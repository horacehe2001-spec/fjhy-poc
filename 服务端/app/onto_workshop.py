# -*- coding: utf-8 -*-
"""本体工场：客户自己把三元本体从零搭起来的那一页，八步一条流水线。

**它补的是一个产品缺口，不是又一条链。** 在它之前，一次完整搭建被切在三个页面上：
业务本体和流程本体共用一条链却各放一个「建设」页签，规则本体是另一条链，
而两个装载器根本不在页面上。客户看不到「源文件 → 本体 → 规则 → 可执行」
是一条线，也没有任何东西告诉他先后顺序。

所以这个模块不新造 Agent，只做两件事：
  **排序**  把已有的两条链和两个装载器按真实依赖串成一条，顺序由代码保证
  **分组**  把 9 个执行单元归成 8 个「产品步骤」，每步标明**谁干的**

三条纪律，页面和这里都不许破：

1. **八步不是八个智能体。** ②③ 是装载器、⑧ 是人，只有 ①④⑤⑥⑦ 是 Agent。
   每一步都带 actor 字段——不标就会变成「AI 全自动搭本体」的幻觉，
   而这是这个产品最不该给的错觉。

2. **一键只跑到 ⑦。** ⑧ 采纳改的是客户的源文件，永远由人点。
   这不是约定而是能力约束：Agent 连写 rule 表都不行
   （WRITABLE_TABLES 白名单里没有它），本体 YAML 更不在任何链的总线上。

3. **顺序不靠客户记。** ③ 装规则资产之前必须先装本体——
   `rule_concept` 没有指向 `ontology_concept` 的外键，顺序点反了不报错，
   只会留下一批静默的坏引用。闸在 `app/rules/loader.py:precheck()` 里，
   命令行和按钮共用同一道。

⑧ 之后要**回到 ②**：采纳改的是源文件，改完必须重装才生效。
这条回边在页面上要画出来，否则客户采纳完会以为已经进库了。

4. **复用 runner 的代价：跨链并发。** 下面「不复制那两条链的 runner」是对的，
   但它带来一件事——**三条链各有自己的 AgentJob 锁，互不阻塞**，
   而复用的 7 个 runner 做的是整表清空后重建。「工场一键搭建」和
   「本体建设运行」同时点下去，A 删完插到一半 B 又删一次，
   最终台账是两次运行的混合，而 INSERT 带 ON CONFLICT DO NOTHING，
   冲突被吞掉——**整个过程不报错**。
   闸是 `db.table_lock()`，按表粒度，下沉到做破坏性写入的那几个函数里
   （onto_build.save / onto_coord.save_align / rules.loader.load_all /
   ontology.loader.load），所以命令行入口也绕不过。
   不用一把全局锁：那会让「本体建设」页在工场跑的时候完全点不动。
   钉在 tests/test_table_lock.py。
"""
import time

from . import agent_job, onto_build_orchestrator as borch, onto_orchestrator as orch
from . import db, onto_coord as oc, onto_yaml as oy
# 判据放在 onto_coord（onto_rule_cover 的归属模块），这里转引一下：
# 反过来 import 会成环（onto_coord ← onto_orchestrator ← 这里）。
# **两页用同一个判据函数**，不各写一份——各写一份就会一页报警一页不报。
cover_check = oc.cover_check
from .agent_job import blank_agent, phase as _phase, phase_done as _phase_done
from .rules import loader as rules_loader

# ── 执行单元：9 个。②③ 是装载器，其余八个来自两条现有的链 ──────────────────
# **不复制那两条链的 runner**，直接引用：复制一份的下场是可预见的——
# 改了一处忘了另一处，然后「从本体页跑」和「从工场页跑」产出不一样，
# 而没人说得清哪个才是对的。runner 的签名统一成 (job, agent, prev) → prev，
# 并且 emit() 是按传入的 job 记的，所以跨作业复用是安全的。
AGENTS = [
    {"id": "fj_l0_04_material_perception",
     "agent_name": "素材感知（源头、规则资产装载）", "layer": "L0",
     "kind": "确定性", "emits": "fjhy_onto_build_requested",
     "pool_keys": ["agent_signal"],
     "desc": "数清素材与本体现有规模的差额，并摆出源文件与装载现状"},
    {"id": "ws_load_ontology", "agent_name": "装载本体源文件", "layer": "—",
     "kind": "装载器", "pool_keys": ["ontology_*", "process_*"],
     "desc": "本体_v0.1.yaml → 库。同版本重装先清旧投影再重建"},
    {"id": "ws_load_assets", "agent_name": "装载规则资产", "layer": "—",
     "kind": "装载器", "pool_keys": ["rule", "standard_limit", "lookup_*"],
     "desc": "规则清单 + 四张资产表 → 库，并建一个 draft 态 release"},
    {"id": "fj_l1_04_field_candidate", "agent_name": "字段与枚举候选", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_onto_build_requested",
     "emits": "fjhy_onto_cand_ready", "pool_keys": ["onto_candidate"],
     "desc": "单据列头 → 候选概念；真实取值与正文 → 枚举印证"},
    {"id": "fj_l1_05_process_candidate", "agent_name": "流程候选", "layer": "L1",
     "kind": "确定性", "listens": "fjhy_onto_build_requested",
     "emits": "fjhy_onto_cand_ready", "pool_keys": ["onto_candidate"],
     "desc": "审批记录 → 节点与迁移候选。判不出不猜"},
    {"id": "fj_l2_03_onto_cand_align", "agent_name": "本体候选对齐", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_onto_cand_ready",
     "emits": "fjhy_onto_build_done", "pool_keys": ["onto_candidate"],
     "desc": "汇总去重、定三档判定、落候选台账；不写本体不碰 YAML"},
    {"id": "fj_l0_03_onto_perception", "agent_name": "本体口径感知", "layer": "L0",
     "kind": "确定性", "emits": "fjhy_onto_unify_requested",
     "pool_keys": ["agent_signal"],
     "desc": "看见抽取产出与本体之间的差额；不归并不对账不下结论"},
    {"id": "fj_l2_01_extract_coord", "agent_name": "抽取协同与冲突仲裁", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_onto_unify_requested",
     "emits": "fjhy_merge_ready", "pool_keys": ["doclib_rule_source", "doclib_conflict"],
     "desc": "按主题词+控制对象归并多个出处，标出口径不一致；不自动选边"},
    {"id": "fj_l2_02_onto_align", "agent_name": "本体对齐", "layer": "L2",
     "kind": "确定性", "listens": "fjhy_merge_ready",
     "emits": "fjhy_onto_aligned", "pool_keys": ["onto_align", "onto_rule_cover"],
     "desc": "归并组与规则本体双向对账；不新增不改规则不裁决冲突"},
]

# ── 产品步骤：8 步。和上面 9 个执行单元是多对一 ────────────────────────────
# ⑦「统一口径」是三个 Agent 一组：对客户来说那是**一件事**（把多个出处的
# 口径统一起来并双向对账），拆成三张卡片只会让人去数智能体个数，
# 而「几个智能体」从来不是客户要的信息。
# **字段叫 step 不叫 name。** 脱敏按字段名选策略，叫 name 的一律当人名打星——
# 实测八步的名字会变成「素***」「装******」，页面上整张流水线就废了。
# 这个坑今天踩第二次（第一次是装载闸的 check 字段），所以测试里两处一起钉。
STEPS = [
    {"no": 1, "step": "素材感知", "actor": "智能体",
     "covers": ["fj_l0_04_material_perception"],
     "why": "先数清手上有什么：源文件谁写的、多大、sha256、库里的投影还同不同步"},
    {"no": 2, "step": "装载本体源文件", "actor": "装载器",
     "covers": ["ws_load_ontology"],
     "why": "YAML 是源，库里是投影。**单向，永不回写**——回写会把全文注释抹光"},
    {"no": 3, "step": "装载规则资产", "actor": "装载器",
     "covers": ["ws_load_assets"],
     "why": "必须排在 ② 之后：规则引用本体里的概念，本体不在，引用就是空的"},
    {"no": 4, "step": "字段与枚举候选", "actor": "智能体",
     "covers": ["fj_l1_04_field_candidate"],
     "why": "从真实单据的列头和取值里数出候选概念，连别名一起比"},
    {"no": 5, "step": "流程候选", "actor": "智能体",
     "covers": ["fj_l1_05_process_candidate"],
     "why": "节点与迁移全部数自真实审批记录，不是设计出来的"},
    {"no": 6, "step": "候选对齐", "actor": "智能体",
     "covers": ["fj_l2_03_onto_cand_align"],
     "why": "汇总去重、定三档判定。**产出叫候选**，不是本体"},
    {"no": 7, "step": "统一口径", "actor": "智能体×3",
     "covers": ["fj_l0_03_onto_perception", "fj_l2_01_extract_coord",
                "fj_l2_02_onto_align"],
     "why": "多源归并、标出口径冲突、规则与依据双向对账。**不自动选边**"},
    {"no": 8, "step": "采纳入本体", "actor": "人",
     "covers": [],
     "why": "**一键链跑到 ⑦ 就停。** 采纳改的是客户的源文件，先看 YAML diff 再决定；"
            "采纳完要回到 ② 重装才生效"},
]

ONE_CLICK_LAST = 7          # 一键跑到第几步为止。改这个数要连测试一起改。


# ── 两个装载器的 runner ─────────────────────────────────────────────────────
def _run_load_ontology(job, a, prev=None):
    t0 = time.time(); _phase(a, "S")
    before = db.scalar("SELECT count(*) FROM ontology_concept") or 0
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    r = oy.reload(check_only=False)
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    st = r.get("stats") or {}
    a["outputs"] = {"实体": st.get("entities"), "概念": st.get("concepts"),
                    "枚举": st.get("enums"), "流程节点": st.get("process_nodes"),
                    "迁移": st.get("process_transitions"),
                    "装载前概念": before}
    a["cost"] = "规则引擎 · 不消耗"
    # **与源文件是否同步要摆出来。** 装完还不同步说明装的不是这份文件。
    if not r.get("in_sync"):
        a["note"] = "**装完仍与源文件不同步**：库里的 hash 和文件对不上，别往下走"
    else:
        a["note"] = "版本 %s；同版本重装会先清旧投影再重建" % (r.get("version") or "?")
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    _phase_done(a, "I", t0)
    return prev


def _run_load_assets(job, a, prev=None):
    t0 = time.time(); _phase(a, "S")
    g = rules_loader.precheck()
    _phase_done(a, "S", t0)

    t0 = time.time(); _phase(a, "D")
    if not g["ok"]:
        # **拦住就是拦住，不降级继续。** 继续的后果是一批静默的坏引用，
        # 而页面上看起来一切正常。
        raise RuntimeError("装载前检查没过：" + g["reason"])
    _phase_done(a, "D", t0)

    t0 = time.time(); _phase(a, "A")
    before = db.scalar("SELECT count(*) FROM rule WHERE version=1") or 0
    r = rules_loader.load_all(verbose=False)
    _phase_done(a, "A", t0)

    t0 = time.time(); _phase(a, "F")
    a["outputs"] = {"规则": r["rules"], "可编译": r["compiled"],
                    "演示可执行": r["executable"], "标准限额": r["standards"],
                    "装载前规则": before}
    a["cost"] = "规则引擎 · 不消耗"
    kept = db.scalar("SELECT count(*) FROM rule WHERE version=1 "
                     "AND created_by LIKE %s", ("onto_adopt%",)) or 0
    warn = "；".join(w["detail"] for w in r.get("warnings") or [])
    a["note"] = ("人采纳进来的 %d 条保留，当前生效发布未动。草稿 %s"
                 % (kept, r.get("release"))) + (("。警告：" + warn) if warn else "")
    _phase_done(a, "F", t0)

    t0 = time.time(); _phase(a, "I")
    _phase_done(a, "I", t0)
    return prev


RUNNERS = dict(borch.RUNNERS)
RUNNERS.update(orch.RUNNERS)
RUNNERS["ws_load_ontology"] = _run_load_ontology
RUNNERS["ws_load_assets"] = _run_load_assets

_JOB = agent_job.AgentJob(
    agents=AGENTS, runners=RUNNERS, baseline_name="onto_workshop_baseline.json",
    idle_note="还没搭过。点「一键搭建」从 ① 跑到 ⑦——"
              "全部确定性，不出网、不计费。**⑧ 采纳永远由人点**，"
              "而且采纳改的是源文件，改完要回到 ② 重装才生效。",
    bus="数据池（ontology_* / process_* / rule / onto_candidate / onto_align）",
    bus_note="Agent 之间**零直接调用**：上游写进池子、发信号，下游从池子里读。"
             "**本体源文件 本体_v0.1.yaml 不在这条总线上**——"
             "整条链都没有写它的能力，唯一的写入口是人工采纳。")

snapshot = _JOB.snapshot
meta = _JOB.meta
start = _JOB.start
blank = blank_agent


def steps():
    """八步 + 每步覆盖哪些执行单元。页面照这个画卡片，不自己编顺序。"""
    by_id = {a["id"]: a for a in AGENTS}
    out = []
    for s in STEPS:
        out.append(dict(s, one_click=s["no"] <= ONE_CLICK_LAST,
                        units=[{"id": i, "agent_name": by_id[i]["agent_name"],
                                "layer": by_id[i]["layer"], "kind": by_id[i]["kind"]}
                               for i in s["covers"]]))
    return {"steps": out, "one_click_last": ONE_CLICK_LAST,
            "note": "八步不是八个智能体：②③ 是装载器、⑧ 是人。"
                    "一键只跑到 ⑦，⑧ 之后要回到 ② 重装才生效。"}


def state():
    """每一步**现在**是什么状况——不跑也要能看。

    页面第一次打开时作业快照是空的，但库里是有事实的：本体装没装、
    规则有多少、候选多少。只拿作业快照画页面，等于告诉客户「什么都没有」，
    而他上次明明搭过。
    """
    src = oy.source_info()
    gate = rules_loader.precheck()
    v = db.scalar("SELECT version FROM ontology_version WHERE is_current")
    # 这两个数要给 cover_check() 用，所以提出来——它判的是「两个数对不对得上」。
    n_rules = db.scalar("SELECT count(*) FROM rule WHERE version=1")
    n_cover = db.scalar("SELECT count(*) FROM onto_rule_cover")
    out = {
        "source": {"path": src["path"], "lines": src["lines"],
                   "sha256": src["sha256"], "written_by": src["written_by"],
                   "in_sync": bool(src["loaded"]["in_sync"]),
                   "db_version": src["loaded"]["version"]},
        "onto": {"version": v,
                 "实体": db.scalar("SELECT count(*) FROM ontology_entity WHERE version=%s", (v,)),
                 "概念": db.scalar("SELECT count(*) FROM ontology_concept WHERE version=%s", (v,)),
                 "枚举": db.scalar("SELECT count(*) FROM ontology_enum WHERE version=%s", (v,)),
                 "枚举取值": db.scalar("SELECT count(*) FROM ontology_enum_value WHERE version=%s", (v,)),
                 "流程节点": db.scalar("SELECT count(*) FROM process_node WHERE version=%s", (v,)),
                 "流程迁移": db.scalar("SELECT count(*) FROM process_transition WHERE version=%s", (v,))},
        "assets": {"规则": n_rules,
                   "可编译": db.scalar("SELECT count(*) FROM rule WHERE version=1 AND compiled"),
                   # **带 version=1，和本体那侧对称。** 不带版本条件地 count，
                   # 等 rule 表出现第二个版本就会把两版加起来——本体那侧实测
                   # 踩过：17 实体 / 121 概念一度报成 34 / 240，而这个数是要写进汇报的。
                   # 今天 rule 表只有 version=1（实测 82 条、demo 12），所以这一行
                   # **现在一个数都不变**，防的是以后。
                   "演示可执行": db.scalar(
                       "SELECT count(*) FROM rule WHERE version=1 AND demo_executable"),
                   "概念引用": db.scalar("SELECT count(*) FROM rule_concept"),
                   "标准限额": db.scalar("SELECT count(*) FROM standard_limit"),
                   "采纳进来的": db.scalar("SELECT count(*) FROM rule WHERE version=1 "
                                     "AND created_by LIKE %s", ("onto_adopt%",))},
        "candidates": {"候选": db.scalar("SELECT count(*) FROM onto_candidate"),
                       "已处置": db.scalar("SELECT count(*) FROM onto_cand_action")},
        "unify": {"对账": db.scalar("SELECT count(*) FROM onto_align"),
                  "规则覆盖": n_cover,
                  "冲突": db.scalar("SELECT count(*) FROM doclib_conflict")},
        "gate": gate,
    }
    # **库里的数对不对得上，要和「库里有什么」放在一起给。**
    # 分两个接口给的下场是页面只拉了前一个，于是又看不见。
    out["freshness"] = [cover_check(n_rules, n_cover)]
    return out
