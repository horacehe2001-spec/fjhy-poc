# -*- coding: utf-8 -*-
"""系统 · 智能体工作台。

全站的 Agent 散在三处：契约在 config/agents/*.yaml（12 份）、运行时状态在
agent_state、运行痕迹在 agent_signal_log / cycle_log，而编排分散在四条链上，
每条链的入口挂在它自己那一栏的页面里。
**结果是没有一个地方能回答「现在有几个智能体、它们各自是什么状态」。**
这一栏就是那个地方。

五段：
  ① 总览      12 个 Agent：层级、触发、读写表、所属链、有没有运行时状态、最近痕迹
  ② 契约      单个 Agent 的 S/D/A/F/I 全文 + **YAML 原文**（注释才是口径）
  ③ 协同与信号 信号台账、通道统计、**pending 积压**、周期
  ④ 运行      四条链：三条手工启动的 + 一条常驻编排的审核链
  ⑤ 存储      五张表，以及「重算时什么会消失」

**这一栏不替 Agent 做事。** 它不改参数、不投信号、不停 Agent：
改参数要走 improve 的 bounds 校验与 improve_log，不能从页面上绕过去；
手工投信号会产生一条「没有上游」的协同记录，审计时说不清是谁触发的。
能启动的只有已有页面上已经有的那三条链，入口搬过来一份而已。

口径纪律（页面上必须写出来，不写就是在说谎）：
  · 契约 12 个，但 agent_state 只有 2 行——另外 10 个是手工编排链上的 Agent，
    参数取契约默认值，没有运行时状态行。
  · 心跳是契约的必填项（yaml_loader 强制），但 **POC 里没有常驻调度器在跑**。
    唯一的常驻编排是 app/runtime/orchestrator.py 的 serve()，要单独启动。
    所以 last_beat_at 全是空——**空不等于坏，等于没开**。
"""
import io

from fastapi import APIRouter, HTTPException, Query

from .. import db
from .. import extract_orchestrator as eorch
from .. import onto_build_orchestrator as borch
from .. import onto_orchestrator as oorch
from ..runtime import yaml_loader

router = APIRouter(prefix="/api/agents", tags=["系统 · 智能体工作台"])

REVIEW_AGENT = "fj_l1_01_travel_review"

# 四条链。前三条是手工启动的作业编排，第四条是常驻编排（LISTEN/NOTIFY + 心跳）。
CHAINS = [
    {"chain_id": "extract", "chain_name": "规则抽取", "page": "/asset/extract",
     "kind": "手工启动", "mod": eorch,
     "note": "制度条款 → AI 规则草稿 → 冲突仲裁。**唯一会调模型、会计费的一条链。**"},
    {"chain_id": "onto_align", "chain_name": "统一口径（规则本体）", "page": "/asset/rules",
     "kind": "手工启动", "mod": oorch,
     "note": "条款与规则双向对账。确定性，不出网、不计费。"},
    {"chain_id": "onto_build", "chain_name": "本体构建（业务 / 流程本体）",
     "page": "/asset/biz", "kind": "手工启动", "mod": borch,
     "note": "素材 → 字段/枚举/流程候选 → 候选对齐。确定性，不出网、不计费。"},
    {"chain_id": "review", "chain_name": "报账单审核", "page": "/review/bills",
     "kind": "常驻编排", "mod": None,
     "note": "L0 感知单据 → L1 审核。由 app/runtime/orchestrator.py 的 serve() 驱动"
             "（LISTEN/NOTIFY + 心跳），**不在这一页启动**：它是常驻进程，"
             "启停属于部署动作，不该挂在一个页面按钮上。"},
]


def _chain_of(agent_id):
    """**由哪个编排负责跑它**——不是「它订阅谁的信号」。

    两者会不一样：fj_l2_01_extract_coord 订阅了抽取链的 fjhy_draft_ready，
    但跑它的是统一口径那条链的编排。页面上同时显示订阅通道，两件事分得开。
    链的成员以编排代码里的 AGENTS 为准，不在这里另记一份——
    记两份的下场是改了编排忘了这里，而页面还一本正经地显示旧的归属。
    """
    for c in CHAINS:
        if c["mod"] is not None:
            if agent_id in [a["id"] for a in c["mod"].AGENTS]:
                return c["chain_id"], c["chain_name"]
    if agent_id.startswith("fj_l0_01") or agent_id == REVIEW_AGENT:
        return "review", "报账单审核"
    return None, None


def _load():
    r = yaml_loader.load(verbose=False)
    if not r.agents:
        raise HTTPException(500, "一个 Agent 契约都没载入——配置目录空了或全部解析失败")
    return r


# ── ① 总览 ──────────────────────────────────────────────────────────────────
@router.get("")
def overview():
    r = _load()
    state = {s["agent_id"]: s for s in db.query("SELECT * FROM agent_state")}
    sig = {s["agent_id"]: s for s in db.query(
        """SELECT agent_id, count(*) AS n, max(emitted_at) AS last
             FROM agent_signal_log GROUP BY agent_id""")}
    cyc = {s["agent_id"]: s for s in db.query(
        """SELECT agent_id, count(*) AS n, max(started_at) AS last
             FROM cycle_log GROUP BY agent_id""")}
    items = []
    for aid in sorted(r.agents):
        c = r.agents[aid]
        chain_id, chain_name = _chain_of(aid)
        st = state.get(aid)
        items.append({
            "agent_id": aid,
            # agent_name 不叫 name：叫 name 会被人名脱敏吃成「素***」
            "agent_name": c.name,
            "agent_version": c.agent_version,
            "layer": c.layer,
            "execution_strategy": c.execution_strategy,
            "triggers": c.enabled_triggers,
            "heartbeat_seconds": (c.trigger("heartbeat") or {}).get("interval_seconds"),
            "listens": (c.trigger("message") or {}).get("channels") or [],
            "emits": ((c.act.get("signal") or {}).get("channel")),
            "reads": sorted(c.readable_tables),
            "writes": sorted(c.writable_tables),
            "params": len(c.param_bounds),
            "forbidden": len(c.improve.get("forbidden") or []),
            "no_judgement": bool(c.decide.get("no_judgement")),
            "chain_id": chain_id, "chain_name": chain_name,
            "has_state": bool(st),
            "enabled": st["enabled"] if st else None,
            "last_beat_at": st["last_beat_at"] if st else None,
            "signals": (sig.get(aid) or {}).get("n") or 0,
            "last_signal_at": (sig.get(aid) or {}).get("last"),
            "cycles": (cyc.get(aid) or {}).get("n") or 0,
            "last_cycle_at": (cyc.get(aid) or {}).get("last"),
            "desc": c.raw.get("description"),
        })
    beats = [x for x in items if x["last_beat_at"]]
    return {
        "items": items,
        "totals": {"agents": len(items),
                   "by_layer": {k: sum(1 for x in items if x["layer"] == k)
                                for k in ("L0", "L1", "L2")},
                   "with_state": sum(1 for x in items if x["has_state"]),
                   "with_trace": sum(1 for x in items if x["signals"] or x["cycles"]),
                   "checks": len(r.checks), "failed": len(r.errors)},
        "facts": [
            {"fact": "契约 %d 个，agent_state 里只有 %d 行"
                     % (len(items), sum(1 for x in items if x["has_state"])),
             "why": "有状态行的是常驻编排那条链上的 Agent。另外那些在**手工启动的作业链**上，"
                    "参数取契约默认值，不落 agent_state——"
                    "**这不是缺数据**，是两种编排方式的差别。"},
            {"fact": "心跳全部没跳过（last_beat_at 空 %d 个）" % (len(items) - len(beats)),
             "why": "心跳是契约的必填项（yaml_loader 强制：LISTEN/NOTIFY 是尽力而为的，"
                    "断连期间的通知收不到且不报错）。但 POC 里**没有常驻调度器在跑**，"
                    "唯一的常驻编排 app/runtime/orchestrator.py:serve() 要单独启动。"
                    "**空不等于坏，等于没开。**"},
            {"fact": "有真实运行痕迹的 %d 个"
                     % sum(1 for x in items if x["signals"] or x["cycles"]),
             "why": "痕迹来自 agent_signal_log 与 cycle_log。没有痕迹的是还没跑过的，"
                    "点「运行」那一段可以跑一次。"},
        ],
    }


# ── ② 契约 ──────────────────────────────────────────────────────────────────
@router.get("/checks")
def checks():
    """现跑一遍契约校验。**和启动时跑的是同一套**（app/main.py 的 lifespan）。

    不是另写一份校验：另写一份就会出现「启动能过、页面报错」这种谁也说不清的状态。
    """
    r = _load()
    rows = [{"check": n, "ok": ok, "detail": d} for n, ok, d in r.checks]
    return {"items": rows, "total": len(rows),
            "failed": sum(1 for x in rows if not x["ok"]),
            "note": "启动时这套校验不过就**拒绝启动**（Fail Fast）。"
                    "启动后报警的系统会在报警被忽略时继续给出错误结论，比不启动坏得多。"}


@router.get("/{agent_id}")
def detail(agent_id: str):
    r = _load()
    c = r.agents.get(agent_id)
    if not c:
        raise HTTPException(404, "没有这个 Agent：%s。可用的见 /api/agents" % agent_id)
    chain_id, chain_name = _chain_of(agent_id)
    st = db.query_one("SELECT * FROM agent_state WHERE agent_id=%s", (agent_id,))
    try:
        text = io.open(c.path, encoding="utf-8").read()
    except OSError as e:
        text = "（读不到契约文件：%s）" % e
    return {
        "agent_id": agent_id, "agent_name": c.name, "agent_version": c.agent_version,
        "layer": c.layer, "execution_strategy": c.execution_strategy,
        "desc": c.raw.get("description"),
        "chain_id": chain_id, "chain_name": chain_name,
        "sdafi": {
            "S": {"tables": sorted(c.readable_tables), "select": c.sense.get("select"),
                  "snapshot": c.sense.get("snapshot")},
            "D": {"kind": c.decide.get("kind"),
                  "conclusion_values": c.decide.get("conclusion_values") or [],
                  "pending_counts_as": c.decide.get("pending_counts_as"),
                  "pending_why": c.decide.get("pending_why"),
                  "no_judgement": bool(c.decide.get("no_judgement")),
                  "no_judgement_why": c.decide.get("no_judgement_why")},
            "A": {"writes": sorted(c.writable_tables),
                  "signal": c.act.get("signal") or {},
                  "idempotency": c.act.get("idempotency"),
                  "idempotency_why": c.act.get("idempotency_why")},
            "F": {"metrics": [{"metric_name": m.get("name"), "desc": m.get("desc"),
                               "source": m.get("source")}
                              for m in (c.feedback.get("metrics") or [])]},
            "I": {"params": [{"param_name": k, "default": v.get("default"),
                              "lo": v["lo"], "hi": v["hi"], "why": v.get("why")}
                             for k, v in c.param_bounds.items()],
                  "forbidden": c.improve.get("forbidden") or []},
        },
        "triggers": {k: dict(v, trigger_kind=k) for k, v in (c.triggers or {}).items()},
        "state": {"has_state": bool(st),
                  "enabled": st["enabled"] if st else None,
                  "params": st["params"] if st else c.params,
                  "params_from": "agent_state（运行时）" if st else "契约默认值（没有状态行）",
                  "last_beat_at": st["last_beat_at"] if st else None,
                  "last_cycle_id": st["last_cycle_id"] if st else None},
        "contract_file": {"file": c.path.replace("\\", "/").split("/")[-1],
                          "lines": text.count("\n") + 1,
                          "comment_lines": sum(1 for ln in text.splitlines()
                                               if ln.lstrip().startswith("#")),
                          "text": text},
        "note": "**契约里的注释是口径本身**，所以这里给原文不给解析结果。"
                "「声明的就是执行的」：S 只能读 sense.tables、A 只能写 act.writes、"
                "I 只能调 improve.params 且必须在 bounds 内，"
                "执行点在 app/runtime/pipeline.py 与 app/audit/recorder.py。",
    }


# ── ③ 协同与信号 ────────────────────────────────────────────────────────────
@router.get("/signals/recent")
def signals(limit: int = Query(50, ge=1, le=300)):
    rows = db.query("""SELECT l.id, l.emitted_at, l.signal_id, l.cycle_id, l.agent_id,
                              l.action, l.target, l.payload, l.written_tables,
                              l.idempotency_key, l.masked
                         FROM agent_signal_log l
                        ORDER BY l.emitted_at DESC, l.id DESC LIMIT %s""", (limit,))
    by_agent = db.query("""SELECT agent_id, count(*) AS n, min(emitted_at) AS first,
                                  max(emitted_at) AS last
                             FROM agent_signal_log GROUP BY agent_id ORDER BY n DESC""")
    by_action = db.query("""SELECT action, count(*) AS n FROM agent_signal_log
                             GROUP BY action ORDER BY n DESC""")
    pool = db.query("""SELECT channel, status, count(*) AS n, max(created_at) AS last
                         FROM agent_signal GROUP BY channel, status ORDER BY n DESC""")
    backlog = sum(x["n"] for x in pool if x["status"] == "pending")
    return {
        "items": rows, "by_agent": by_agent, "by_action": by_action, "pool": pool,
        "backlog": backlog,
        "facts": [
            {"fact": "数据池里有 %d 条 pending 信号" % backlog,
             "why": "pending 是「投了还没人消费」。没有常驻调度器在跑的时候，"
                    "信号就会一直堆着——**堆着不是错**，它正是「尽力而为的通知"
                    "丢了也不会丢任务」的那层保险。开起 serve() 它们会被消费掉。"}
            if backlog else
            {"fact": "数据池里没有积压", "why": "所有投出去的信号都已被消费或归档。"},
            {"fact": "三条手工链的信号 2026-10-19 之前一条都没落库",
             "why": "**实测过的 bug**：app/agent_job.py:emit 写的是 channel / kind / "
                    "from_agent 这些库里不存在的列，两个 NOT NULL 列也没给值，"
                    "所以 INSERT 从来没成功过，而 `except: pass` 把它静默了。"
                    "现在列名和 app/audit/recorder.py 对齐，落库失败也会在"
                    "作业快照的 log_errors 里显示出来。"},
        ],
        "note": "**Agent 之间零直接调用**：上游写进数据池、发信号，下游从池子里读。"
                "agent_signal 是在途的信号，agent_signal_log 是不可改的台账。",
    }


@router.get("/cycles/recent")
def cycles(limit: int = Query(50, ge=1, le=300)):
    rows = db.query("""SELECT cycle_id, agent_id, agent_version, rule_set_version,
                              ontology_version, subject_id, trigger_kind, trigger_reason,
                              decision, status, error, phase_timings_ms,
                              prompt_tokens, completion_tokens, started_at, finished_at
                         FROM cycle_log ORDER BY started_at DESC LIMIT %s""", (limit,))
    by_agent = db.query("""SELECT agent_id, count(*) AS n,
                                  count(*) FILTER (WHERE status <> 'ok') AS bad,
                                  max(started_at) AS last
                             FROM cycle_log GROUP BY agent_id ORDER BY n DESC""")
    by_trigger = db.query("""SELECT trigger_kind, count(*) AS n FROM cycle_log
                              GROUP BY trigger_kind ORDER BY n DESC""")
    return {"items": rows, "by_agent": by_agent, "by_trigger": by_trigger,
            "note": "一个周期 = 一次 S→D→A→F→I。phase_timings_ms 是**实测**各阶段毫秒数，"
                    "页面上的进度权重就是按它折算的，不按「已完成个数/总数」。"}


# ── ④ 运行 ──────────────────────────────────────────────────────────────────
@router.get("/chains/list")
def chains():
    out = []
    for c in CHAINS:
        row = {"chain_id": c["chain_id"], "chain_name": c["chain_name"],
               "kind": c["kind"], "page": c["page"], "note": c["note"]}
        if c["mod"] is not None:
            snap = c["mod"].snapshot()
            row["agents"] = [{"agent_id": a["id"], "agent_name": a["agent_name"],
                              "layer": a["layer"], "kind": a["kind"],
                              "desc": a["desc"]} for a in c["mod"].AGENTS]
            row["status"] = snap.get("status")
            row["pct"] = snap.get("pct")
            row["job_id"] = snap.get("job_id")
            row["elapsed"] = snap.get("elapsed")
            # **两个数必须分开。** snapshot 里的 signals 是「本次作业内存里投了几条」，
            # 重启或没跑过就是 0；台账是库里累计的。只给前者会让页面显示
            # 「信号 0 条」而库里明明有行——那种自相矛盾最难查。
            row["signals_in_job"] = len(snap.get("signals") or [])
            ids = [a["id"] for a in c["mod"].AGENTS]
            row["signals"] = db.scalar(
                "SELECT count(*) FROM agent_signal_log WHERE agent_id = ANY(%s)", (ids,))
            row["log_errors"] = snap.get("log_errors") or []
            row["error"] = snap.get("error")
        else:
            row["agents"] = [{"agent_id": a, "agent_name": n, "layer": lay,
                              "kind": "常驻", "desc": d}
                             for a, n, lay, d in (
                                 ("fj_l0_01_bill_perception", "单据感知", "L0",
                                  "扫新单据，投信号；由数据库触发器通知"),
                                 (REVIEW_AGENT, "差旅审核", "L1",
                                  "跑执行集里的规则，出审核结论与发现项"))]
            row["status"] = "常驻（本页不启停）"
            row["signals"] = db.scalar(
                "SELECT count(*) FROM agent_signal_log WHERE agent_id=%s", (REVIEW_AGENT,))
            row["cycles"] = db.scalar(
                "SELECT count(*) FROM cycle_log WHERE agent_id=%s", (REVIEW_AGENT,))
        out.append(row)
    return {"items": out,
            "note": "**本页只搬入口，不新造能力。** 三条手工链在各自那一栏也能启动，"
                    "启动的是同一个编排、同一份进度语义（app/agent_job.py）。"
                    "审核链是常驻进程，启停属于部署动作，不挂页面按钮。"}


@router.post("/chains/{chain_id}/run")
def run_chain(chain_id: str):
    c = next((x for x in CHAINS if x["chain_id"] == chain_id), None)
    if not c:
        raise HTTPException(404, "没有这条链：%s" % chain_id)
    if c["mod"] is None:
        raise HTTPException(400, "审核链是**常驻编排**（app/runtime/orchestrator.py 的 "
                                 "serve()），不从页面启动：它是常驻进程，"
                                 "启停属于部署动作。单据来了由数据库触发器通知它。")
    return c["mod"].start()


@router.get("/chains/{chain_id}/progress")
def chain_progress(chain_id: str):
    c = next((x for x in CHAINS if x["chain_id"] == chain_id), None)
    if not c or c["mod"] is None:
        raise HTTPException(404, "这条链没有可轮询的作业进度：%s" % chain_id)
    return c["mod"].snapshot()


# ── ⑤ 存储 ──────────────────────────────────────────────────────────────────
STORAGE = [
    ("config/agents/*.yaml", "**契约源文件**：12 份，声明 S/D/A/F/I 与可读可写表",
     "人工编写", "不清",
     "「声明的就是执行的」。启动时校验不过**拒绝启动**；注释是口径本身，不许用 dump 重写"),
    ("agent_state", "运行时状态：参数、bounds、是否启用、上次心跳",
     "常驻编排（serve）/ 装载脚本", "不清",
     "只有常驻链上的 Agent 有行。手工作业链的参数取契约默认值，不落这张表"),
    ("agent_signal", "**在途**信号：pending / claimed / done", "A 阶段",
     "停机时 claimed 放回 pending",
     "停机不放回就会留下孤儿信号，永远没人处理而且看不出来"),
    ("agent_signal_log", "**信号台账**，不可改。每次写库动作都要有对应的一行",
     "A 阶段（recorder / agent_job）", "永不清",
     "「A 是唯一允许写库的阶段」这条纪律的可验证形式。幂等键唯一索引钉住重放"),
    ("cycle_log", "周期台账：一行一次 S→D→A→F→I，含各阶段实测毫秒与版本号",
     "常驻编排", "永不清",
     "审计追溯靠它串起「哪一版规则、哪一版本体、哪个模型判的」"),
    ("data_pool_snapshot", "S 阶段读到的数据快照（**已脱敏**）", "S 阶段", "按保留期清",
     "masked=False 的行不许出现在生产：快照会被审计接口与导出读到，那都是旁路出口"),
]


@router.get("/storage/tables")
def storage():
    import os

    from .. import config
    rows = []
    for name, what, who, on_recalc, why in STORAGE:
        if name.endswith("*.yaml"):
            try:
                n = len([f for f in os.listdir(config.AGENT_CONFIG_DIR)
                         if f.endswith((".yaml", ".yml"))])
            except OSError:
                n = None
        else:
            try:
                n = db.scalar("SELECT count(*) FROM " + name)
            except Exception:                      # noqa: BLE001 表不存在要显式写出来
                n = None
        rows.append({"table_name": name, "what": what, "written_by": who,
                     "on_recalc": on_recalc, "why": why, "rows": n})
    return {"items": rows,
            "note": "契约在**文件**里，状态与痕迹在**库**里。两边的一致性由启动校验保证："
                    "校验不过就不启动，而不是启动后报警。"}
