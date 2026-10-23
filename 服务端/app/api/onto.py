# -*- coding: utf-8 -*-
"""栏目三 规则本体 / 栏目四 流程本体 的接口。

页面按四段 + 存储组织（2026-10-19 用户口径）：
  ① 本体建设  智能体协同跑统一口径（规则）/ 解析器数真实记录（流程）
  ② 本体展示  本体里现在有什么
  ③ 本体关系  实体↔实体、规则↔规则、规则↔概念、归并组↔规则
  ④ 本体图谱  关系的可视形态：哪个是枢纽、哪条边悬着、哪个点是孤的
  ⑤ 存储      哪张表、谁写的、**重算时什么会消失**

**归并组与口径冲突的接口从 /api/extract 挪到这里**：
统一口径是本体要做的事，规则抽取只负责抽取（用户口径）。
接口路径跟着归属走，不然下一个人会在抽取那一栏找这些数。
"""
from fastapi import APIRouter, Body, HTTPException, Query

from .. import db
from .. import doclib_extract as dx, onto_build as ob, onto_coord as oc
from .. import onto_build_orchestrator as borch, onto_orchestrator as orch
from .. import onto_process as op, onto_yaml as oy
from .. import onto_workshop as wsp
from .common import must_exist

router = APIRouter(prefix="/api/onto", tags=["规则资产 · 规则本体"])


# ── 业务本体 · 建立：把藏在命令行与对话里的步骤搬到明面上 ────────────────────
# 「所有东西都摆在明面上操作」（2026-10-19 用户）。在这组接口之前：
#   · 924 行的 本体_v0.1.yaml 是人读客户材料**手写**的，页面上看不到
#   · 装进库要在命令行跑 `python -m app.ontology.loader`
#   · 客户没法自己再跑一遍，也查不到证据链
@router.post("/build/run")
def build_run():
    """手工启动一次本体构建。四个确定性 Agent 一路跑到底，**不出网、不计费**。

    产出是**候选**，不是本体：采纳是人的动作，而且采纳改的是源文件不是库。
    """
    return borch.start()


@router.get("/build/progress")
def build_progress():
    return borch.snapshot()


@router.get("/build/agents")
def build_agents():
    return borch.meta()


@router.get("/build/summary")
def build_summary():
    return ob.summary()


@router.get("/candidates")
def candidates(kind: str = Query(None, description="实体/概念/枚举取值/流程节点/流程迁移"),
               verdict: str = Query(None, description="建议新增/层间不一致/已在本体"),
               limit: int = Query(300, ge=1, le=500)):
    return {"items": ob.list_candidates(kind, verdict, limit),
            "note": "每条候选都带**出处与出现次数**——没有出处的候选不进这张表，"
                    "这和本体 YAML 的第一条口径纪律是同一条。"
                    "「层间不一致」不指定修哪一层：该补节点层、还是枚举层当初写多了，"
                    "方向要人判。"}


@router.get("/candidates/{cand_id}/preview")
def candidate_preview(cand_id: str, yaml_target: str = Query(None)):
    """只算不写：把将要插入源文件的那几行摆出来。

    **采纳是改用户手写的源文件，必须先看清要插什么。**
    """
    try:
        return ob.preview(cand_id, yaml_target)
    except ob.BuildError as e:
        raise HTTPException(400, str(e))


@router.post("/candidates/{cand_id}/adopt")
def candidate_adopt(cand_id: str, body: dict = Body(...)):
    """采纳：**定点追加进 本体_v0.1.yaml，再自动重装**。

    顺序是 备份 → 按行插入 → safe_load 验（不过就从备份回滚）→ 重装 → 记台账。
    不走 yaml.dump：dump 会把 924 行里的口径注释一次性抹光。
    """
    try:
        return ob.adopt(cand_id, body.get("by"), body.get("yaml_target"),
                        reload=body.get("reload", True))
    except ob.BuildError as e:
        raise HTTPException(400, str(e))


@router.post("/candidates/{cand_id}/dispose")
def candidate_dispose(cand_id: str, body: dict = Body(...)):
    try:
        return ob.dispose(cand_id, body.get("action"), body.get("by"), body.get("reason"))
    except ob.BuildError as e:
        raise HTTPException(400, str(e))


@router.get("/build/storage")
def build_storage():
    return ob.biz_storage()


@router.get("/build/actions")
def build_actions(limit: int = Query(200, ge=1, le=500)):
    return {"items": ob.list_actions(limit),
            "note": "采纳会改源文件，所以台账里记的不只是「谁采纳了什么」，"
                    "还有**改了哪一行、插了什么、备份在哪、重装成没成**。"}


# ── 本体工场：八步一条流水线 ────────────────────────────────────────────────
@router.get("/workshop/steps")
def workshop_steps():
    """八步 + 每步覆盖哪些执行单元 + 谁干的。**页面照这个画，不自己编顺序。**

    顺序写在后端的理由和闸一样：顺序本身是口径（③ 必须在 ② 之后），
    前端排一遍就是第二份口径，而两份迟早不一样。
    """
    return wsp.steps()


@router.get("/workshop/state")
def workshop_state():
    """每一步**现在**是什么状况——没跑过也要能看。

    第一次打开时作业快照是空的，但库里是有事实的（本体装没装、规则多少条）。
    只拿作业快照画页面，等于告诉客户「什么都没有」，而他上次明明搭过。
    """
    return wsp.state()


@router.post("/workshop/run")
def workshop_run():
    """一键搭建 ①→⑦。**⑧ 不在里面**，它是人的动作。"""
    return wsp.start()


@router.get("/workshop/progress")
def workshop_progress():
    return wsp.snapshot()


@router.get("/workshop/agents")
def workshop_agents():
    return wsp.meta()


# ── 源文件：看得见 ──────────────────────────────────────────────────────────
@router.get("/source")
def source():
    return oy.source_info()


@router.get("/source/section")
def source_section(name: str = Query(..., description="meta/business/entities/enums/…"),
                   max_lines: int = Query(400, ge=20, le=2000)):
    """**原文照抄，含注释。** 注释才是这份文件最贵的部分，摘要掉等于没给。"""
    try:
        return oy.read_section(name, max_lines)
    except oy.PatchError as e:
        raise HTTPException(404, str(e))


@router.post("/reload")
def reload_onto(check_only: bool = Query(False, description="只比对指纹，不写库")):
    """把命令行的 `python -m app.ontology.loader` 搬到明面上。

    **两件事分开**：check_only 只比对，不写库；不带它是真重装
    （先清掉同版本的旧投影再重建）。合成一个按钮迟早有人误点。
    """
    try:
        return oy.reload(check_only=check_only)
    except db.TableBusy as e:
        # 409 不是 500：另一条链正在装载不是服务器出错，是「现在不能做」。
        # 500 会让人去翻日志找异常，而这里要的是「等它跑完再点」。
        raise HTTPException(409, str(e))


@router.get("/load-assets/ready")
def load_assets_ready():
    """第③步的闸：能不能装规则资产，以及不能装的理由。

    页面拿它来决定按钮灰不灰。**理由要能显示出来**——
    一个灰着不给理由的按钮，和没有这个按钮一样挡路。
    """
    from ..rules import loader as rules_loader
    return rules_loader.precheck()


@router.post("/load-assets")
def load_assets():
    """装载规则资产（第③步）。原来只有命令行，2026-10-20 按产品要求做成按钮。

    **做成按钮的前提是闸先存在**：闸在装载器自己身上（`precheck()`），
    命令行和这个接口共用同一道，所以「从页面点」和「从命令行跑」安全性一致。

    这里不传 force：**接口永远不许越过闸**。要越过得上机器敲命令行，
    那时人至少知道自己在干什么。
    """
    from ..rules import loader as rules_loader
    g = rules_loader.precheck()
    if not g["ok"]:
        raise HTTPException(409, g["reason"])
    before = db.scalar("SELECT count(*) FROM rule WHERE version=1")
    try:
        out = rules_loader.load_all(verbose=False)
    except db.TableBusy as e:
        # 和上面的 precheck 一样用 409：两道都是「现在不能装」，
        # 区别只在一个是顺序没到、一个是别人正在装。
        raise HTTPException(409, str(e))
    out["before_rules"] = before
    out["adopted_kept"] = db.scalar("SELECT count(*) FROM rule WHERE version=1 "
                                    "AND created_by LIKE 'onto_adopt%%'")
    out["release_current_untouched"] = db.scalar("SELECT count(*) FROM release_current")
    return out


@router.get("/load-assets-note")
def load_assets_note():
    """装载规则资产会动什么、绝不动什么。

    这一条以前写的是「为什么不做按钮：它会清空 release_current」。
    **那句话在代码改掉之后就不成立了**，而它正好是「客户不能自助搭本体」
    的理由，所以留着它的代价不是文案不准，是一个功能被一句假话挡住。
    """
    return {
        "command": "python -I app/rules/loader.py",
        "api": "POST /api/onto/load-assets（页面第③步的按钮）",
        "touches":
            "只重建装载脚本自己的地盘：created_by='asset_loader' 的规则，"
            "以及它自己建的那份 status='draft' 的 release。",
        "never_touches":
            "**已发布/审阅中/已回滚的 release 一个不碰，release_current 也不清**"
            "——那是栏目五「发布管理」的事。"
            "人采纳进来的规则（created_by=onto_adopt:谁）同样保留；"
            "原来是无条件 DELETE FROM rule，重跑一次装载会把它们静默删掉。",
        "gate":
            "装载前先过 precheck()：本体已装、源文件与库同步、"
            "规则引用的概念与实体都在库里。**闸在装载器身上，命令行也绕不过。**"
            "理由是 rule_concept 没有指向 ontology_concept 的外键，"
            "顺序点反了不会报错，只会留下一批静默的坏引用。",
        "current": {"rules": db.scalar("SELECT count(*) FROM rule WHERE version=1"),
                    "adopted": db.scalar("SELECT count(*) FROM rule WHERE version=1 "
                                         "AND created_by LIKE 'onto_adopt%%'"),
                    "releases": db.scalar("SELECT count(*) FROM release"),
                    "release_current": db.scalar("SELECT count(*) FROM release_current")},
    }


# ── ① 本体建设：智能体协同 ──────────────────────────────────────────────────
@router.post("/run")
def run_job():
    """手工启动一次统一口径。三个确定性 Agent 一路跑到底，**不出网、不计费**。"""
    return orch.start()


@router.get("/progress")
def progress():
    return orch.snapshot()


@router.get("/agents")
def agents_meta():
    return orch.meta()


@router.get("/summary")
def summary():
    return oc.summary()


# ── ② 本体展示：双向对账的两张清单 ──────────────────────────────────────────
@router.get("/groups")
def groups(verdict: str = Query(None, description="已落地 / 未落地 / 判不出"),
           limit: int = Query(300, ge=1, le=500)):
    return {"items": oc.list_align(verdict, limit),
            "note": "多源不是冗余，是证据强度：制度条款 + 规则表都写了同一件事，"
                    "这条规则比单一出处可信得多。"
                    "「判不出」不是「未落地」——规则清单的来源写的是类别代号，"
                    "对不到唯一文件就不能下断言。"}


@router.get("/cover")
def cover(state: str = Query(None, description="有制度依据/仅规则表/编号对不上/查无依据/来源未写明"),
          limit: int = Query(300, ge=1, le=500)):
    return {"items": oc.list_cover(state, limit),
            "note": "「编号对不上」不是「查无依据」：来源写的是款级、表内行或问答序号，"
                    "和条款级编号对不上。标成查无依据会让人去改规则，"
                    "而该改的是切分粒度。"}


@router.get("/options")
def options():
    """采纳表单的可选值。**风险类别取现有值，不新造**——
    新造一个会让按风险类别统计的报表多出一档。"""
    return {"dimensions": oc.DIMS, "rule_classes": list(oc.CLASSES),
            "control_types": list(oc.CONTROLS), "risk_classes": oc.risk_classes()}


# ── 口径冲突：只发现，不自动选边 ────────────────────────────────────────────
@router.get("/conflicts")
def conflicts(state: str = Query(None), limit: int = Query(200, ge=1, le=500)):
    return {"items": dx.list_conflicts(state, limit),
            "note": "系统只发现冲突，不自动选边。裁决必须记 resolved_by——"
                    "「谁拍的板」在制度冲突这种事情上，和结论本身一样重要。"}


@router.post("/conflicts/{conflict_id}/resolve")
def resolve(conflict_id: str, body: dict = Body(...)):
    try:
        ok = dx.resolve_conflict(conflict_id, body.get("state"), body.get("who"),
                                 body.get("resolution"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    must_exist(ok or None, "冲突 " + conflict_id)
    return {"conflict_id": conflict_id, "state": body.get("state"), "ok": True}


# ── 采纳入本体：人的动作 ────────────────────────────────────────────────────
@router.get("/pending")
def pending(limit: int = Query(100, ge=1, le=300)):
    return {"items": oc.pending_adoption(limit),
            "note": "**建议不等于结论**：控制类型、规则类型、维度、风险类别都要人选。"
                    "给了默认值人就会直接点确认，而控制类型选错，"
                    "就是把「提醒」变成「拦单」，或者反过来让一道闸静默失效。"}


@router.post("/adopt")
def adopt(body: dict = Body(...)):
    """采纳成一条规则（status=draft、未编译、不在执行集）。

    **采纳入本体 ≠ 发布生效。** 发布是栏目五的事。
    """
    try:
        return oc.adopt(body.get("group_key"), body.get("by"), body)
    except oc.AdoptError as e:
        raise HTTPException(400, str(e))


@router.post("/dispose")
def dispose(body: dict = Body(...)):
    """驳回 / 暂缓。两者都必须写理由。"""
    try:
        return oc.dispose(body.get("group_key"), body.get("action"),
                          body.get("by"), body.get("reason"))
    except oc.AdoptError as e:
        raise HTTPException(400, str(e))


@router.get("/adoptions")
def adoptions(limit: int = Query(200, ge=1, le=500)):
    return {"items": oc.list_adoptions(limit),
            "note": "采纳 / 驳回 / 暂缓的全部动作台账。**重算不动它**——"
                    "算出来的结论整表重算，人拍的永不清。"}


# ── ③④ 本体关系与图谱 ──────────────────────────────────────────────────────
@router.get("/relations")
def relations():
    return oc.relations()


@router.get("/graph")
def graph():
    return oc.graph()


# ── ⑤ 存储 ──────────────────────────────────────────────────────────────────
@router.get("/storage")
def storage():
    return oc.storage()


# ── 栏目四 · 流程本体 ───────────────────────────────────────────────────────
@router.get("/process/build")
def process_build():
    return op.build()


@router.get("/process/graph")
def process_graph():
    return op.graph()


@router.get("/process/storage")
def process_storage():
    return op.storage()
