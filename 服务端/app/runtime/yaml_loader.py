# -*- coding: utf-8 -*-
"""YAMLAgentLoader：从 config/agents/*.yaml 载入 Agent 配置，并在启动时校验。

**配置驱动的意义在于「声明的就是执行的」。** 如果 YAML 里写 `act.writes: [X]`
而代码实际写了 Y，那这份 YAML 就只是注释，审计时毫无价值。
所以这里不只是「读 YAML」，还要把声明变成**可执行的约束**：

  · S 阶段只允许读 sense.tables 里列的表
  · A 阶段只允许写 act.writes 里列的表
  · I 阶段只允许调 improve.params 里列的参数，且必须在 bounds 内
  · D/F/I 阶段一个表都不许写

约束的执行点在 app/runtime/pipeline.py 与 app/audit/recorder.py，
本模块负责把约束读出来并校验配置自身的完整性。

启动时校验不过就不启动（Fail Fast）。
"""
import io
import os

import yaml

if __name__ == "__main__" and __package__ is None:      # 允许 python app/runtime/yaml_loader.py
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from app import config
else:
    from .. import config

REQUIRED_TOP = ["agent_id", "agent_version", "layer", "name",
                "triggers", "sense", "decide", "act", "feedback", "improve",
                "execution_strategy"]
VALID_LAYERS = {"L0", "L1", "L2"}
VALID_STRATEGY = {"per_station", "batch"}
VALID_TRIGGERS = {"heartbeat", "message", "subscribe"}

# A 阶段允许写的表白名单。不在这里面的表，任何 Agent 都不许写——
# 业务表的变更必须经由明确声明的路径，否则审计记录与实际发生的事会对不上。
WRITABLE_TABLES = {
    "agent_signal", "agent_signal_log", "review_run", "review_finding",
    "data_pool_snapshot", "cycle_log", "feedback_log", "improve_log",
    "agent_state", "pool_maintenance_log", "manual_review",
    # ── 规则抽取链路（2026-10-18）──────────────────────────────────────────
    # 这几张表让抽取 Agent 可写，但**写规则本体的 rule 表仍然不在名单里**：
    # 抽取产出的是草稿，采纳是人的动作。Agent 能直接写 rule，
    # 就等于有一条「模型抽出来的东西自动变成生效规则」的路——
    # 这正是整个设计里最不该存在的那条路。
    "doclib_clause", "doclib_draft", "doclib_extract_task",
    "doclib_rule_source", "doclib_conflict",
    # 规则明细（2026-10-19）：抽取的**产物台账**，一行一条规则明细。
    # 它仍然是草稿性质——doclib_rule_detail.review_state 默认「待审」，
    # 采纳只是标记，adopted_rule_id 要等「规则本体」那一栏回填。
    # 明细表进名单、rule 表不进，这条界线和上面一样：
    # **抽取能产出规则明细，不能让规则生效。**
    "doclib_rule_detail", "doclib_detail_tag",
    # ── 规则本体 · 统一口径（2026-10-19）────────────────────────────────────
    # 双向对账的结论可以由 Agent 写：它是**算出来的**，每次重算整表覆盖。
    # 但 onto_adoption 与 rule 都**不在**名单里：
    # 采纳入本体是人的动作，必须有 adopted_by。
    # 「对账发现缺口就自动补一条规则」这条路一旦存在，迟早有人走上去，
    # 而自动补出来的规则带着「有制度依据」的外观，复核的人未必回查原文。
    "onto_align", "onto_rule_cover",
    # ── 本体构建（2026-10-19）────────────────────────────────────────────────
    # 候选可以由 Agent 写：它是算出来的，整表重算。
    # 但 **ontology_* 一张都不在名单里，onto_cand_action 也不在**：
    # 采纳要改 本体_v0.1.yaml（人手写的源文件），而本体一改，
    # 所有引用这些字段的规则口径就跟着变。
    # 「系统自己悄悄改了本体」这条路不能存在——不是约定不走，是能力上不给。
    "onto_candidate",
    # 抽取完成后要在文件上钉指纹（extracted_at / extract_fingerprint），
    # 否则「这份文件抽过没有」永远算不出来。
    "doc_lib_file",
}


class AgentConfig:
    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
        self.agent_id = raw["agent_id"]
        self.agent_version = raw["agent_version"]
        self.layer = raw["layer"]
        self.name = raw["name"]
        self.triggers = raw["triggers"] or {}
        self.sense = raw["sense"] or {}
        self.decide = raw["decide"] or {}
        self.act = raw["act"] or {}
        self.feedback = raw["feedback"] or {}
        self.improve = raw["improve"] or {}
        self.execution_strategy = raw["execution_strategy"]

    # ── 把声明变成约束 ──────────────────────────────────────────────────────
    @property
    def readable_tables(self):
        return set(self.sense.get("tables") or [])

    @property
    def writable_tables(self):
        return set(self.act.get("writes") or [])

    @property
    def param_bounds(self):
        out = {}
        for k, v in (self.improve.get("params") or {}).items():
            b = v.get("bounds")
            if b and len(b) == 2:
                out[k] = {"lo": b[0], "hi": b[1], "default": v.get("default"),
                          "why": v.get("why")}
        return out

    @property
    def params(self):
        return {k: v.get("default") for k, v in (self.improve.get("params") or {}).items()}

    def trigger(self, kind):
        t = self.triggers.get(kind) or {}
        return t if t.get("enabled") else None

    @property
    def enabled_triggers(self):
        return [k for k in VALID_TRIGGERS if self.trigger(k)]

    def __repr__(self):
        return "<AgentConfig %s v%s %s>" % (self.agent_id, self.agent_version, self.layer)


class LoaderResult:
    def __init__(self):
        self.agents = {}
        self.errors = []
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), detail))
        if not ok:
            self.errors.append("%s：%s" % (name, detail))
        return ok

    @property
    def ok(self):
        return not self.errors


def load(verbose=True, agent_dir=None):
    d = agent_dir or config.AGENT_CONFIG_DIR
    r = LoaderResult()
    if not os.path.isdir(d):
        r.check("Agent 配置目录存在", False, d)
        return r
    files = sorted(f for f in os.listdir(d) if f.endswith((".yaml", ".yml")))
    r.check("至少有一个 Agent 配置", bool(files), "目录 %s 里没有 yaml" % d)

    for fn in files:
        path = os.path.join(d, fn)
        try:
            raw = yaml.safe_load(io.open(path, encoding="utf-8"))
        except Exception as exc:
            r.check("%s 可解析" % fn, False, str(exc).split("\n")[0])
            continue
        missing = [k for k in REQUIRED_TOP if k not in (raw or {})]
        if not r.check("%s 必填段完整" % fn, not missing, "缺 %s" % missing):
            continue
        cfg = AgentConfig(raw, path)

        # 文件名要能对上 agent_id。对不上会让「改了哪个 Agent」在 code review 里看不出来
        r.check("%s 文件名与 agent_id 一致" % fn,
                fn.rsplit(".", 1)[0] == cfg.agent_id,
                "文件名 %s vs agent_id %s" % (fn.rsplit(".", 1)[0], cfg.agent_id))
        r.check("%s layer 合法" % cfg.agent_id, cfg.layer in VALID_LAYERS,
                "%s 不在 %s 内" % (cfg.layer, sorted(VALID_LAYERS)))
        r.check("%s execution_strategy 合法" % cfg.agent_id,
                cfg.execution_strategy in VALID_STRATEGY,
                "%s 不在 %s 内" % (cfg.execution_strategy, sorted(VALID_STRATEGY)))

        # 触发器：至少一个，且必须有心跳
        r.check("%s 至少启用一种触发" % cfg.agent_id, bool(cfg.enabled_triggers), "一个都没启用")
        r.check("%s 必须启用心跳" % cfg.agent_id, bool(cfg.trigger("heartbeat")),
                "LISTEN/NOTIFY 是尽力而为的，连接断开期间的通知收不到且不报错。"
                "只靠消息/订阅会在重连窗口静默丢任务，所以心跳不是可选项")
        for k in cfg.triggers:
            r.check("%s 触发类型 %s 合法" % (cfg.agent_id, k), k in VALID_TRIGGERS,
                    "只允许 %s" % sorted(VALID_TRIGGERS))
        hb = cfg.trigger("heartbeat")
        if hb:
            iv = hb.get("interval_seconds")
            r.check("%s 心跳间隔在 10~300 秒内" % cfg.agent_id,
                    isinstance(iv, int) and 10 <= iv <= 300,
                    "interval_seconds=%s，白皮书给的量级是 30 秒~5 分" % iv)

        # A 阶段写表白名单
        bad_w = cfg.writable_tables - WRITABLE_TABLES
        r.check("%s 的 act.writes 都在白名单内" % cfg.agent_id, not bad_w,
                "不允许写 %s；业务表的变更必须经由明确声明的路径，"
                "否则审计记录与实际发生的事会对不上" % sorted(bad_w))
        r.check("%s 声明了 act.writes" % cfg.agent_id, bool(cfg.writable_tables),
                "A 是唯一允许写库的阶段，写什么必须声明")
        r.check("%s 声明了幂等键" % cfg.agent_id, bool(cfg.act.get("idempotency")),
                "心跳与消息可能对同一主体同时醒来，没有幂等键就会产出两份结论")

        # S 阶段读表必须声明
        r.check("%s 声明了 sense.tables" % cfg.agent_id, bool(cfg.readable_tables),
                "S 阶段只允许读声明过的表")

        # I 阶段参数必须有区间，且默认值在区间内
        bounds = cfg.param_bounds
        declared = set((cfg.improve.get("params") or {}).keys())
        r.check("%s 的每个可调参数都有 bounds" % cfg.agent_id,
                declared == set(bounds),
                "缺 bounds 的参数 %s；没有区间就等于允许无限自优化" % sorted(declared - set(bounds)))
        for k, b in bounds.items():
            dv = b["default"]
            r.check("%s 参数 %s 的默认值在区间内" % (cfg.agent_id, k),
                    dv is not None and b["lo"] <= dv <= b["hi"],
                    "默认 %s 不在 [%s, %s]" % (dv, b["lo"], b["hi"]))
        r.check("%s 声明了 improve.forbidden" % cfg.agent_id,
                bool(cfg.improve.get("forbidden")),
                "必须写明哪些东西 Agent 不许动——规则、阈值、控制类型都不许")
        fb = " ".join(str(x) for x in (cfg.improve.get("forbidden") or []))
        for must in ("规则", "阈值", "控制类型"):
            r.check("%s 的 forbidden 覆盖「%s」" % (cfg.agent_id, must), must in fb,
                    "规则是制度的投影，改规则是业务决策，不是 Agent 能动的")

        # F 阶段必须分别计误报与漏报
        if cfg.layer != "L0":
            names = {m.get("name") for m in (cfg.feedback.get("metrics") or [])}
            r.check("%s 的 feedback 分别计误报与漏报" % cfg.agent_id,
                    {"false_positive", "false_negative"} <= names,
                    "只有 %s；合成一个「准确率」会把误报的代价藏起来" % sorted(names))

        if cfg.agent_id in r.agents:
            r.check("agent_id 唯一", False, "%s 重复" % cfg.agent_id)
        r.agents[cfg.agent_id] = cfg

    # 信号通道要闭合：有人发就得有人收，反之亦然
    emits, listens, db_emitted = {}, {}, set()
    for a in r.agents.values():
        ch = (a.act.get("signal") or {}).get("channel")
        if ch:
            emits.setdefault(ch, []).append(a.agent_id)
        for k in ("message", "subscribe"):
            t = a.trigger(k) or {}
            srcs = t.get("channel_sources") or {}
            for c in t.get("channels") or []:
                # 有些通道由**数据库触发器**发送，不是 Agent 发的
                # （比如 fjhy_bill_changed 由 expense_bill 上的触发器发）。
                # 这类通道要在 YAML 里显式声明来源，否则就当成「没人发」报错——
                # 默认报错是对的：订阅了一个没人发的通道，这条路就白配了，
                # 而心跳会把它掩盖成「好像在工作」。
                if srcs.get(c) == "db_trigger":
                    db_emitted.add(c)
                    continue
                listens.setdefault(c, []).append(a.agent_id)
    orphan_emit = {c: v for c, v in emits.items() if c not in listens}
    orphan_listen = {c: v for c, v in listens.items() if c not in emits}
    # 发出去没人收：允许，但要知道（终端信号，比如 review_done 给前端用）
    if db_emitted:
        r.checks.append(("信号通道：由数据库触发器发送", True,
                         "%s —— 已在 YAML 里显式声明 emitted_by: db_trigger" % sorted(db_emitted)))
    if orphan_emit:
        r.checks.append(("信号通道：发出但无 Agent 订阅", True,
                         "%s —— 终端信号，由前端或人工消费，不算错" % sorted(orphan_emit)))
    # 订阅了但没人发：这是真问题，这个 Agent 永远等不到消息
    r.check("订阅的通道都有 Agent 发送", not orphan_listen,
            "%s 订阅了没人发送的通道，会永远等不到消息（心跳还能兜住，"
            "但那意味着订阅这条路白配了）" % orphan_listen)

    if verbose:
        print("Agent 配置校验（%d 个文件）" % len(files))
        for a in r.agents.values():
            print("  %-28s v%-7s %s  触发=%s  读 %d 表 / 写 %d 表 / 可调 %d 参数"
                  % (a.agent_id, a.agent_version, a.layer,
                     "+".join(a.enabled_triggers), len(a.readable_tables),
                     len(a.writable_tables), len(a.param_bounds)))
        bad = [(n, d) for n, ok, d in r.checks if not ok]
        print("  检查项 %d，失败 %d" % (len(r.checks), len(bad)))
        for n, d in bad:
            print("    !! %s：%s" % (n, d))
    return r


def load_or_die(verbose=True):
    r = load(verbose=verbose)
    if not r.ok:
        raise SystemExit("** Agent 配置校验失败 %d 项，拒绝启动（Fail Fast）：\n%s"
                         % (len(r.errors), "\n".join("   - " + e for e in r.errors)))
    return r


if __name__ == "__main__":
    import sys
    res = load()
    sys.exit(0 if res.ok else 1)
