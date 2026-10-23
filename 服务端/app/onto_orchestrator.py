# -*- coding: utf-8 -*-
"""规则本体 · 统一口径的智能体编排：3 个 Agent 走完 S→D→A→F→I。

和抽取那条链的关键差别有两条，页面上要说清楚：

1. **这条链全部是确定性 Agent，不出网、不计费。**
   抽取链里 AI 草稿那一步占 96% 的时长、真计费；统一口径一步都不调模型。
   口径对不对是能复核的硬判断（文件名 + 条款号精确命中），
   让模型来判反而会给出一条「看起来有据可查」的证据链。

2. **这条链产出的是结论，不是规则。**
   采纳入本体是人的动作（onto_coord.adopt），不在这条作业里。
   Agent 连写 rule 表的能力都没有——WRITABLE_TABLES 白名单里不给。

进度/失败/信号的公共语义在 app/agent_job.py，和抽取链共用一套。
"""
import time

from . import agent_job, doclib_extract as dx, onto_coord as oc
from .agent_job import PHASES, blank_agent, phase as _phase, phase_done as _phase_done  # noqa: F401

AGENTS = [
    {"id": "fj_l0_03_onto_perception", "agent_name": "本体口径感知", "layer": "L0",
     "kind": "确定性", "emits": "fjhy_onto_requested",
     "pool_keys": ["agent_signal"],
     "desc": "看见抽取产出与本体之间的差额，投信号；不归并不对账不下结论"},
    {"id": "fj_l2_01_extract_coord", "agent_name": "抽取协同与冲突仲裁", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_onto_requested", "emits": "fjhy_extract_coord_done",
     "pool_keys": ["doclib_rule_source", "doclib_conflict"],
     "desc": "按主题词+控制对象归并多个出处，标出口径不一致；不自动选边"},
    {"id": "fj_l2_02_onto_align", "agent_name": "本体对齐", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_extract_coord_done", "emits": "fjhy_onto_aligned",
     "pool_keys": ["onto_align", "onto_rule_cover"],
     "desc": "归并组与规则本体双向对账；不新增不改规则不裁决冲突"},
]


def _run_perceive(job, a, prev=None):
    """L0：报数。差额就是这一栏要干的活。"""
    t0 = time.time(); _phase(a, "S")
    p = oc.perceive()
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    delta = max(0, (p["条款"] or 0) - (p["已归并来源"] or 0))
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    _JOB.emit(job, a, "fjhy_onto_requested",
              {"clauses": p["条款"], "grouped": p["已归并来源"], "delta": delta})
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"条款": p["条款"], "已归并": p["已归并来源"], "待统口径": delta,
                    "本体规则": p["本体规则"], "待裁决冲突": p["待裁决冲突"]}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("差额 %d 条条款还没进归并组" % delta) if delta else "条款都已归并过"
    if p["待裁决冲突"]:
        a["note"] += "；另有 %d 处口径冲突待裁决，这些组不许采纳" % p["待裁决冲突"]
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return p


def _run_coord(job, a, prev):
    """L2 归并：多个出处归到一起，口径不一致的标冲突。**不自动选边。**"""
    t0 = time.time(); _phase(a, "S")
    clauses = dx.list_clauses(limit=2000)
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    groups, conflicts = dx.group_and_detect(clauses)
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    n_src, n_conf, stale = dx.save_groups(groups, conflicts)
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_extract_coord_done",
              {"groups": len(groups), "sources": n_src, "conflicts": n_conf})

    t0 = time.time(); _phase(a, "F")
    multi = sum(1 for g in groups.values() if len(g) > 1)
    a["outputs"] = {"归并组": len(groups), "多源印证": multi, "来源行": n_src,
                    "口径冲突": n_conf, "清掉的旧待裁决": stale}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("%d 组有两个及以上出处；%d 处口径冲突待裁决（系统只发现，不自动选边）"
                 % (multi, n_conf)) if n_conf else ("%d 组有两个及以上出处" % multi)
    if stale:
        # 这句话要显示出来：**清掉的是上一版检测器留下的待裁决行**，
        # 不是「冲突消失了」。两者在页面上必须分得开。
        a["note"] += "；清掉 %d 条本次未检出的旧「待裁决」（人裁决过的一条不动）" % stale
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"groups": len(groups), "conflicts": n_conf}


def _run_align(job, a, prev):
    """L2 对齐：双向对账。**两向并列，不合成一个覆盖率。**"""
    t0 = time.time(); _phase(a, "S")
    comparable, codes = oc.comparable_files()
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    fwd = oc.align_forward()
    rev = oc.align_reverse()
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    n_f, n_r = oc.save_align(fwd, rev)
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_onto_aligned", {"groups": n_f, "rules": n_r})

    t0 = time.time(); _phase(a, "F")
    fc = {}
    for x in fwd:
        fc[x["verdict"]] = fc.get(x["verdict"], 0) + 1
    rc = {}
    for x in rev:
        rc[x["state"]] = rc.get(x["state"], 0) + 1
    a["outputs"] = {"制度→落地": "已落地 %d / 未落地 %d / 判不出 %d"
                            % (fc.get("已落地", 0), fc.get("未落地", 0), fc.get("判不出", 0)),
                    "规则→依据": "有依据 %d / 编号对不上 %d / 查无依据 %d / 来源未写明 %d"
                            % (rc.get("有制度依据", 0), rc.get("编号对不上", 0),
                               rc.get("查无依据", 0), rc.get("来源未写明", 0)),
                    "可比对文件": "%d / %d" % (len(comparable), len(codes))}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    # 「判不出」占比高不是算法差，是**台账口径缺口**——要说清楚，
    # 否则下一个人会去调匹配算法，而该补的是规则清单的来源字段。
    und = fc.get("判不出", 0)
    unwritten = rc.get("来源未写明", 0)
    a["note"] = ("判不出 %d 组、来源未写明 %d 条规则：规则清单的来源写的是类别代号"
                 "（规则表/要点/制度…）而不是文件名，代号对不到唯一文件。"
                 "**这是台账口径缺口，不是匹配算法的问题**" % (und, unwritten)) \
        if (und or unwritten) else "两向都对齐，没有判不出的"
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"forward": fc, "reverse": rc}


RUNNERS = {
    "fj_l0_03_onto_perception": _run_perceive,
    "fj_l2_01_extract_coord": _run_coord,
    "fj_l2_02_onto_align": _run_align,
}

_JOB = agent_job.AgentJob(
    agents=AGENTS, runners=RUNNERS, baseline_name="onto_baseline.json",
    idle_note="还没统过口径。点「开始统一口径」启动一次智能体协同——"
              "这条链全部是确定性 Agent，不出网、不计费。",
    bus="数据池（doclib_rule_source / doclib_conflict / onto_align / onto_rule_cover）",
    bus_note="Agent 之间**零直接调用**：上游写进池子、发信号，下游从池子里读。"
             "采纳入本体不在这条总线上——那是人的动作，Agent 连写 rule 表的能力都没有。")

_blank_agent = blank_agent
_pct = _JOB.pct
_load_baseline = _JOB.load_baseline
_save_baseline = _JOB.save_baseline
snapshot = _JOB.snapshot
meta = _JOB.meta
start = _JOB.start
