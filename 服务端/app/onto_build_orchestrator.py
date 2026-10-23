# -*- coding: utf-8 -*-
"""本体构建的智能体编排：4 个 Agent 走完 S→D→A→F→I。

这条链补的是**整个项目里唯一一个完全在系统之外发生过的步骤**：
本体骨架是人读客户材料手写进 本体_v0.1.yaml 的，页面上看不到来源、
没有入口、客户没法自己再跑一遍，也查不到任何证据链。

**边界必须说清，不然就是夸大：**
  能：从素材里数出有硬证据的东西（单据列头 39 个、枚举取值的真实使用次数、
      审批记录 368 条里的节点与迁移），每条候选都带「出自哪、出现几次」。
  不能：替代人读制度划实体、定概念语义与 source 归属。
  所以产出叫**候选**；采纳是人的动作，而且采纳改的是**源文件**不是库。

全部确定性，**不出网不计费**（2026-10-19 用户选的）。

进度/失败/信号的公共语义在 app/agent_job.py，三条链共用一套。
"""
import time

from . import agent_job, onto_build as ob
from .agent_job import PHASES, blank_agent, phase as _phase, phase_done as _phase_done  # noqa: F401

AGENTS = [
    # 名字带括号是用户 2026-10-19 定的：源头与规则资产装载原来是「建立」页上
    # 两个独立面板，并进这个 Agent 后，页面上要看得出它现在管着这两件事。
    {"id": "fj_l0_04_material_perception",
     "agent_name": "素材感知（源头、规则资产装载）", "layer": "L0",
     "kind": "确定性", "emits": "fjhy_onto_build_requested",
     "pool_keys": ["agent_signal"],
     "desc": "数清素材与本体现有规模的差额，并摆出源文件与装载现状；不抽候选不判断"},
    {"id": "fj_l1_04_field_candidate", "agent_name": "字段与枚举候选", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_onto_build_requested",
     "emits": "fjhy_onto_cand_ready", "pool_keys": ["onto_candidate"],
     "desc": "单据列头 → 候选概念；真实取值与正文 → 枚举印证。连别名一起比"},
    {"id": "fj_l1_05_process_candidate", "agent_name": "流程候选", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_onto_build_requested",
     "emits": "fjhy_onto_cand_ready", "pool_keys": ["onto_candidate"],
     "desc": "审批记录 → 节点与迁移候选。别名与唯一前缀都算对上，判不出不猜"},
    {"id": "fj_l2_03_onto_cand_align", "agent_name": "本体候选对齐", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_onto_cand_ready",
     "emits": "fjhy_onto_build_done", "pool_keys": ["onto_candidate"],
     "desc": "汇总去重、定三档判定、落候选台账；不写本体不碰 YAML"},
]


def _run_perceive(job, a, prev=None):
    t0 = time.time(); _phase(a, "S")
    p = ob.perceive()
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    _JOB.emit(job, a, "fjhy_onto_build_requested", {"sheets": p["单据明细表"],
                                                    "approvals": p["审批记录"]})
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    # 前六个是素材与本体的规模差额；后四个是搬进来的源头与装载现状。
    # 布尔值不往页面上送：「true」在中文页面上不是结论，写成一致/不一致。
    a["outputs"] = {"单据明细表": p["单据明细表"], "条款": p["条款"],
                    "审批记录": p["审批记录"], "本体概念": p["本体概念"],
                    "本体枚举取值": p["本体枚举取值"], "流程节点": p["流程节点"],
                    "源文件": "%s · %d 行 · sha %s…"
                              % (p["源文件"], p["源文件行数"], p["源文件sha256"][:12]),
                    "库里的投影": "%s · %s" % (p["库里版本"] or "未装载",
                                         "与源文件一致" if p["与库同步"]
                                         else "不一致：文件改了没重装"),
                    "规则资产": "%d 条（采纳进来的 %d 条）"
                            % (p["规则"], p["规则里采纳进来的"]),
                    "发布": "release %d 个 · 当前发布 %d 个"
                          % (p["release"], p["当前发布"])}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("素材：%d 份单据明细表、%d 条条款、%d 条审批记录；"
                 "本体现有 %d 概念 / %d 枚举取值 / %d 流程节点。"
                 "源头：%s —— %s "
                 "装载：%s —— %s"
                 % (p["单据明细表"], p["条款"], p["审批记录"],
                    p["本体概念"], p["本体枚举取值"], p["流程节点"],
                    p["源文件谁写的"], p["源文件怎么写的"],
                    p["装载命令"], p["装载会动什么"]))
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return p


def _run_field(job, a, prev):
    t0 = time.time(); _phase(a, "S")
    sheets = ob.bill_sheets()
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    h, s1 = ob.header_candidates()
    e, s2 = ob.enum_candidates()
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    _JOB.emit(job, a, "fjhy_onto_cand_ready", {"from": "field", "n": len(h) + len(e)})
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"单据工作表": len(sheets), "列头": s1["列头"], "列头对上": s1["对上"],
                    "候选概念": s1["候选"], "枚举取值": s2["枚举取值"],
                    "数据里用到": s2["数据里用到"], "未检索到": s2["候选"]}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("列头 %d 个，%d 个对上本体（**含别名**），%d 个没有对应概念；"
                 "枚举取值 %d 个里有 %d 个样本与正文都没检索到——"
                 "那不等于写错了，出处可能落在读不出的表格页里"
                 % (s1["列头"], s1["对上"], s1["候选"], s2["枚举取值"], s2["候选"]))
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"cands": h + e, "stats": {"列头": s1, "枚举": s2}}


def _run_process(job, a, prev):
    t0 = time.time(); _phase(a, "S")
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    p, s3 = ob.process_candidates()
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    _JOB.emit(job, a, "fjhy_onto_cand_ready", {"from": "process", "n": len(p)})
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"实测节点名": s3["实测节点名"], "节点对上": s3["节点对上"],
                    "实测迁移": s3["实测迁移"], "迁移对上": s3["迁移对上"],
                    "候选": s3["候选"]}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("节点 %d 个对上 %d 个（别名与唯一前缀都算）；迁移 %d 条只有 %d 条"
                 "在本体的迁移表里。**对不上的大多源自同几个节点问题**，"
                 "不是 %d 条独立缺口"
                 % (s3["实测节点名"], s3["节点对上"], s3["实测迁移"],
                    s3["迁移对上"], s3["实测迁移"] - s3["迁移对上"]))
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"cands": (prev or {}).get("cands", []) + p,
            "stats": dict((prev or {}).get("stats", {}), 流程=s3)}


def _run_align(job, a, prev):
    t0 = time.time(); _phase(a, "S")
    cands = (prev or {}).get("cands", [])
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    # 去重：同一个名字可能被两条路同时抽到（「总经理」既是节点名又是 EN16 取值）。
    # 不去重，页面上同一件事出现两行，人处置完一行以为完事了。
    seen, uniq = set(), []
    for c in cands:
        k = (c["kind"], c["cand_key"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    merged = len(cands) - len(uniq)
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    n = ob.save(uniq)
    _phase_done(a, "A", t0)
    _JOB.emit(job, a, "fjhy_onto_build_done", {"candidates": n})

    t0 = time.time(); _phase(a, "F")
    by = {}
    for c in uniq:
        by[c["verdict"]] = by.get(c["verdict"], 0) + 1
    a["outputs"] = {"候选": n, "建议新增": by.get("建议新增", 0),
                    "层间不一致": by.get("层间不一致", 0),
                    "已在本体": by.get("已在本体", 0), "去重合并": merged}
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    a["note"] = ("落候选 %d 条（建议新增 %d / 层间不一致 %d / 已在本体 %d）。"
                 "**到这里就停**：采纳是人的动作，而且采纳改的是源文件 "
                 "本体_v0.1.yaml，不是库"
                 % (n, by.get("建议新增", 0), by.get("层间不一致", 0),
                    by.get("已在本体", 0)))
    _phase_done(a, "I", t0)
    a["cost"] = "规则引擎 · 不消耗"
    return {"candidates": n, "by_verdict": by}


RUNNERS = {
    "fj_l0_04_material_perception": _run_perceive,
    "fj_l1_04_field_candidate": _run_field,
    "fj_l1_05_process_candidate": _run_process,
    "fj_l2_03_onto_cand_align": _run_align,
}

_JOB = agent_job.AgentJob(
    agents=AGENTS, runners=RUNNERS, baseline_name="onto_build_baseline.json",
    idle_note="还没跑过。点「开始本体构建」启动一次智能体协同——"
              "全部确定性 Agent，不出网、不计费，产出的是**候选**，采纳是人的动作。",
    bus="数据池（onto_candidate / agent_signal）",
    bus_note="Agent 之间**零直接调用**：上游写进池子、发信号，下游从池子里读。"
             "**本体源文件 本体_v0.1.yaml 不在这条总线上**——"
             "整条链都没有写它的能力，唯一的写入口是人工采纳。")

_blank_agent = blank_agent
_pct = _JOB.pct
_load_baseline = _JOB.load_baseline
_save_baseline = _JOB.save_baseline
snapshot = _JOB.snapshot
meta = _JOB.meta
start = _JOB.start
