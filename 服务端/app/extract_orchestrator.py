# -*- coding: utf-8 -*-
"""规则抽取的智能体编排：驱动 3 个 Agent 走完 S→D→A→F→I。

页面要的是**协同的进展**，不是一排按钮。那就得真有东西在跑、真有状态可读。
这个模块就是那条执行循环——在它之前，这几个 Agent 只有 YAML 配置，
契约校验能过，但运行时根本不会调度它们。

进度/失败/信号这些**公共语义在 app/agent_job.py**（两条链共用一套，
不留两份会各自走偏的拷贝）。这里只剩这条链自己的东西：
哪几个 Agent、每个 Agent 的 S→D→A→F→I 具体干什么。
"""
import time

from . import agent_job, doclib, doclib_extract as dx, rule_detail
from .agent_job import PHASES, blank_agent, phase as _phase, phase_done as _phase_done  # noqa: F401

# ── 编排图（没开跑时也要能画）──────────────────────────────────────────────
# 和 config/agents/*.yaml 一一对应。这里是**给界面看的编排元数据**，
# 真正的契约在 YAML 里，由启动时的校验卡住。两边的 agent_id 必须一致。
AGENTS = [
    {"id": "fj_l0_02_doclib_perception", "agent_name": "文件库变更感知", "layer": "L0",
     "kind": "确定性", "emits": "fjhy_extract_requested",
     "pool_keys": ["agent_signal"],
     "desc": "发现该抽取的文件，投信号；不判断、不切、不调模型"},
    {"id": "fj_l1_02_clause_split", "agent_name": "条款切分", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_extract_requested", "emits": "fjhy_clause_ready",
     "pool_keys": ["doclib_clause", "doc_lib_file"],
     "desc": "按文件形态选策略切条款、识特征、比对覆盖；不出网不计费"},
    {"id": "fj_l1_06_rule_detail", "agent_name": "规则明细成形", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_clause_ready", "emits": "fjhy_detail_ready",
     "pool_keys": ["doclib_rule_detail"],
     "desc": "表格行按表头名映射成规则明细（一行一条规则）；不调模型不计费"},
    {"id": "fj_l1_03_rule_draft", "agent_name": "规则草稿", "layer": "L1",
     "kind": "大模型", "listens": "fjhy_clause_ready", "emits": "fjhy_draft_ready",
     "pool_keys": ["doclib_draft"],
     "desc": "对候选条款调模型产八要素草稿 + 数值幻觉核对；真出网真计费"},
]

# **「规则明细」排在「规则草稿」前面，是故意的。**
# 列映射不出网、不计费，而且本身就是有价值的产物（实测 35 条规则明细）。
# 排在后面的话，一旦模型那一步失败，整条作业就停在那儿
# （app/agent_job.py 的 _work：失败之后的 Agent 标「未执行」），
# 于是一次模型失败变成两件事都没产出。
# 代价是：**本次新成稿的草稿要等下一轮才投影进明细**——
# 这一点由「规则草稿」的结论里明说，并且单步调试里有「只跑规则明细」可以立刻补。
# 常驻运行时（orchestrator.serve）没有这个问题：明细 Agent 同时订阅
# fjhy_clause_ready 和 fjhy_draft_ready，草稿一落它自己会醒。

# **L2「抽取协同与冲突仲裁」不在这条作业里。**
# 多源归并、口径冲突裁决是**统一口径**，那是「规则本体」那一栏的事——
# 抽取只负责抽出来，口径一不一致、以谁为准，是本体层的判断。
# 它由 app/onto_orchestrator.py 驱动，订阅 fjhy_draft_ready，
# 正好接在这条链的产出之后。
# 写在这里是因为：下一个读这段代码的人一定会问「第四个 Agent 哪去了」。


# ── 三个 Agent 的执行体 ─────────────────────────────────────────────────────
# ── 增量范围：L0 算一次，全链共用 ───────────────────────────────────────────
# **这是本模块最要紧的一条不变式。**
#
# 2026-10-21 修掉的缺陷：L0 一直在调 doclib.pending_extract() 算增量、
# 投信号 `{"files": N}`，而下游的 _run_split 调的是 `dx.run_split()`——不带参数，
# 等于全量重切。于是整条链**看着像增量**（L0 如实报「待抽取 N 份」），
# 实际每次都全量跑：库里 13 个抽取任务的 scope 全是「全部」，
# only_changed 这条路从来没被执行过。编排图画的是增量，跑的是全量。
#
# 范围只能算一次，然后放在 job 上给所有下游读。两个原因：
#
# 1. **算第二次就是空集。** run_split 每处理完一份就调 doclib.mark_extracted()，
#    所以它跑完之后 pending_extract() 返回 0 份。要是「规则明细」和「AI 草稿」
#    各自再问一次「增量是谁」，它们拿到的都是空集——
#    表现是「条款切了 2 份，明细和草稿一条没出」。
# 2. **不走 prev 链。** prev 是「上一个 Agent 的返回值」，要让第四个 Agent
#    看到第一个 Agent 定的范围，中间两个都得记得把它带上，
#    而任何一个忘了带，范围就静默退化成「全部」——那是会花钱的退化。
def _scope_of(job):
    """本次作业的范围。→ {"only_changed": bool, "file_ids": list | None}

    `file_ids is None` 表示全量；`[]` 表示**确实没有要处理的文件**。
    这两者在下游 run_split / run_ai / rule_detail.run 里是完全相反的意思，
    那三处都已显式区分（`is not None`，不是 `if file_ids:`）。
    """
    return job.get("scope") or {"only_changed": False, "file_ids": None}


def _run_l0(job, a, prev=None):
    """L0 感知：看见哪些文件该抽了，并**把范围定下来**。只看见，不判断。"""
    only_changed = bool((job.get("opts") or {}).get("only_changed", True))

    t0 = time.time(); _phase(a, "S")
    pend = doclib.pending_extract(limit=500)
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    files = [r for r in pend["items"]]
    blocked = pend["blocked"]
    # 范围在这里定死，下游一律读 job["scope"]，不再自己算
    job["scope"] = {
        "only_changed": only_changed,
        "file_ids": [r["file_id"] for r in files] if only_changed else None,
        "files": [{"file_id": r["file_id"], "file_name": r["file_name"],
                   "why": r["why"]} for r in files],
    }
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    _JOB.emit(job, a, "fjhy_extract_requested",
              {"files": len(files) if only_changed else "全部", "增量": only_changed})
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"范围": "仅变化" if only_changed else "全部",
                    "待抽取文件": len(files),
                    "取不到正文": sum(b["n"] for b in blocked)}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    if not only_changed:
        bits = ["**全量**：忽略增量判断，对所有能取到正文的文件重跑一遍"
                "（改过切分策略或正则之后必须这样跑一次，否则文件没动就一份也跑不到）"]
    elif not files:
        bits = ["**增量为 0 份**：没有新文件，也没有哪份文件在上次抽取之后被改过。"
                "后面三步都会空跑并如实报 0，**不会退化成全量重跑**"]
    else:
        why = {}
        for r in files:
            why[r["why"]] = why.get(r["why"], 0) + 1
        bits = ["投递 %d 份（%s）" % (len(files),
                "、".join("%s %d 份" % kv for kv in why.items()))]
    if blocked:
        bits.append("另有 %d 份取不到正文，抽取覆盖不到它们"
                    % sum(b["n"] for b in blocked))
    a["note"] = "；".join(bits)
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"files": len(files), "only_changed": only_changed}


def _run_split(job, a, prev):
    t0 = time.time(); _phase(a, "S")
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    sc = _scope_of(job)
    # 范围来自 job["scope"]（L0 算定），**不在这里重算**——重算拿到的是空集，
    # 因为 run_split 自己会 mark_extracted()。见本文件上方的不变式说明。
    r = dx.run_split(file_ids=sc["file_ids"], only_changed=sc["only_changed"])
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_clause_ready",
              {"clauses": r["clauses"], "candidates": r["candidates"]})

    t0 = time.time(); _phase(a, "F")
    strategies = {}
    for f in r["per_file"]:
        if f.get("clauses"):
            strategies[f.get("strategy") or "?"] = strategies.get(f.get("strategy") or "?", 0) + 1
    a["outputs"] = {"范围": r["scope"], "文件": r["files"],
                    "条款": r["clauses"], "候选": r["candidates"],
                    "策略": "、".join("%s×%d" % kv for kv in strategies.items()) or "—"}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    unsplit = [f["file_name"] for f in r["per_file"] if not f.get("clauses")
               and f.get("strategy") != "跳过"]
    if sc["only_changed"] and not r["files"]:
        a["note"] = ("增量为 0 份，没有切任何东西。**库里现有的条款一条没动**——"
                     "这是增量抽取该有的样子，不是失败。"
                     "改过切分策略要全量重切，用单步调试里的「只切条款（全部）」")
    else:
        a["note"] = ("切出 %d 条，其中候选 %d 条" % (r["clauses"], r["candidates"])
                     + ("；%d 份没切出来（排版特殊）" % len(unsplit) if unsplit else ""))
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return r


def _run_detail(job, a, prev):
    """规则明细成形。确定性列映射 + 把已成稿的草稿投影进同一张表。不出网。"""
    t0 = time.time(); _phase(a, "S")
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    sc = _scope_of(job)
    # 同一个范围。注意 rule_detail.run 的第三路（已采纳草稿投影）**刻意不受限定**：
    # 采纳是人在别的时候点的，和这次增量是哪几份文件无关；限定了会让
    # 「我明明采纳了，明细里没有」。那一路是幂等 UPSERT，重复跑不多产出。
    r = rule_detail.run(file_ids=sc["file_ids"])
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_detail_ready",
              {"mapped": r["table"]["mapped"], "projected": r["draft"]["projected"]})

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"范围": "仅变化" if sc["only_changed"] else "全部",
                    "规则明细": r["total"],
                    "列映射": r["table"]["mapped"] - r["scenario"]["mapped"],
                    "场景拆条": r["scenario"]["mapped"],
                    "列对不齐": r["table"]["flagged"],
                    "草稿投影": r["draft"]["projected"]}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    bits = ["按表头名映射出 %d 条，场景表按要点拆出 %d 条"
            % (r["table"]["mapped"] - r["scenario"]["mapped"], r["scenario"]["mapped"])]
    if sc["only_changed"] and not sc["file_ids"]:
        bits = ["增量为 0 份，列映射与场景拆条都没有可处理的文件"]
    if r["table"]["flagged"]:
        bits.append("%d 条列对不齐（留原文交人看，没有硬映射）" % r["table"]["flagged"])
    if r["draft"]["projected"]:
        bits.append("另投影 %d 条模型草稿（这一路不受增量范围限定，"
                    "采纳是人在别的时候点的）" % r["draft"]["projected"])
    a["note"] = "；".join(bits)
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return r


def _run_draft(job, a, prev):
    """AI 草稿。**这一步真出网、真计费**，所以增量范围在这里最要紧。

    增量为 0 份时**一次调用都不许发出去**。原来的 `dx.run_ai(limit=8)` 不带范围，
    增量 0 份照样会从全库候选里挑 8 条买回来——钱花了，而使用者以为
    自己只是点了一下「增量抽取」且增量是空的。
    """
    sc = _scope_of(job)
    t0 = time.time(); _phase(a, "S")
    # 增量为空就不去读模型配置了：没配 extract 用途的默认模型时 _extract_cfg
    # 会抛 RuntimeError 把整条链判红，而「增量 0 份」本来是正常结果，
    # 不该因为模型没配就变成失败。
    empty = sc["only_changed"] and not sc["file_ids"]
    if not empty:
        try:
            dx._extract_cfg()
        except RuntimeError:
            _phase_done(a, "S", t0)
            raise
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    _phase_done(a, "D", t0)

    if empty:
        for code in ("A", "F", "I"):
            t0 = time.time(); _phase(a, code); _phase_done(a, code, t0)
        a["outputs"] = {"范围": "仅变化", "试抽": 0, "成稿": 0, "失败": 0, "数值存疑": 0}
        a["note"] = ("增量为 0 份，**没有向模型发出任何一次调用**。"
                     "要对库里积压的候选条款跑草稿，用单步调试里的「只跑 AI 草稿」"
                     "——那一步不受增量范围限制")
        a["cost"] = "未调用 · 不消耗"
        _JOB.emit(job, a, "fjhy_draft_ready", {"drafted": 0})
        return {"tried": 0, "drafted": 0, "failed": 0, "number_suspect": 0,
                "model": None, "items": []}

    t0 = time.time(); _phase(a, "A")
    r = dx.run_ai(limit=8, file_ids=sc["file_ids"])
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_draft_ready", {"drafted": r["drafted"]})

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"范围": "仅变化" if sc["only_changed"] else "全部",
                    "试抽": r["tried"], "成稿": r["drafted"], "失败": r["failed"],
                    "数值存疑": r["number_suspect"]}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    bits = ["模型 %s" % r["model"]]
    if r["number_suspect"]:
        bits.append("数值核对不通过 %d 条，必须回查原文" % r["number_suspect"])
    if r["drafted"]:
        # 明细那一步排在前面（它不花钱，不该等这一步），所以本轮新成稿的
        # 还没进明细表。**说出来**，否则页面上的明细条数会被当成漏了。
        bits.append("本轮新成稿 %d 条尚未进明细表（明细那一步排在前面），"
                    "点「只跑规则明细」即可补上" % r["drafted"])
    a["note"] = "；".join(bits)
    _phase_done(a, "I", t0)
    # 费用：本项目没有核对过各家单价，**不编**。「不知道」不是 0。
    a["cost"] = "已调用 %d 次 · 单价待核对" % r["tried"]
    return r


RUNNERS = {
    "fj_l0_02_doclib_perception": _run_l0,
    "fj_l1_02_clause_split": _run_split,
    "fj_l1_06_rule_detail": _run_detail,
    "fj_l1_03_rule_draft": _run_draft,
}

_JOB = agent_job.AgentJob(
    agents=AGENTS, runners=RUNNERS, baseline_name="extract_baseline.json",
    idle_note="还没跑过。点「开始抽取」启动一次智能体协同。",
    bus="数据池（agent_signal / doclib_*）",
    bus_note="Agent 之间**零直接调用**：上游写进池子、发信号，下游从池子里读。"
             "这条总线就是「数据池是唯一介质」的样子。")

# 对外的名字保持不变：接口层和测试都按这几个名字调。
# 换成实例方法之后这几行是唯一的粘合层——**不许让调用方改成 _JOB.xxx**，
# 那样一来「抽取编排器」这个模块就名存实亡了。
_blank_agent = blank_agent
_pct = _JOB.pct
_load_baseline = _JOB.load_baseline
_save_baseline = _JOB.save_baseline
snapshot = _JOB.snapshot
meta = _JOB.meta
start = _JOB.start
