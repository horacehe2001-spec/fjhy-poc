# -*- coding: utf-8 -*-
"""报账单 AI 审核端 + 审计接口 + 调试接口。

后两个是**脱敏的旁路出口**，专门放在同一个文件里，提醒它们和审核报告一样
要过同一套脱敏规则。做安全的那个模块往往只守住主输出路径（报告正文），
而审计接口、调试查询这些根本不经过它。
"""
import os

from fastapi import APIRouter, Body, HTTPException, Query

from .. import db
from ..audit import recorder as rec
from .common import demo_banner, must_exist, paginate

router = APIRouter(prefix="/api", tags=["报账单审核"])


# ─────────────────────────────────────────────────────────────────────────────
# 审核岗工作台
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/bills")
def list_bills(conclusion: str = None, is_demo: bool = None,
               q: str = None, page: int = 1, size: int = 20):
    where, params = [], []
    if is_demo is not None:
        where.append("b.is_demo = %s")
        params.append(is_demo)
    if q:
        where.append("(b.bill_no ILIKE %s OR b.summary ILIKE %s)")
        params += ["%%%s%%" % q, "%%%s%%" % q]
    if conclusion:
        where.append("r.conclusion = %s")
        params.append(conclusion)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    base = """FROM expense_bill b
              LEFT JOIN LATERAL (SELECT * FROM review_run rr WHERE rr.bill_no=b.bill_no
                                 ORDER BY rr.started_at DESC LIMIT 1) r ON TRUE""" + w
    out = paginate(
        "SELECT count(*) " + base,
        """SELECT b.bill_no, b.doc_type, b.doc_date, b.biz_type, b.amount, b.summary,
                  b.is_demo, b.applicant_rank, b.remark IS NOT NULL AS has_remark,
                  r.run_id, r.conclusion, r.hit_count, r.pending_count,
                  r.rule_executed, r.rule_candidates, r.finished_at
           """ + base + " ORDER BY b.doc_date DESC NULLS LAST, b.bill_no", tuple(params),
        page, size)
    out["banner"] = demo_banner()
    return out


@router.get("/bills/{bill_no}")
def get_bill(bill_no: str):
    b = must_exist(db.query_one("""SELECT b.*, o.name AS org_name FROM expense_bill b
                                   LEFT JOIN org o ON o.org_id=b.org_id
                                   WHERE b.bill_no=%s""", (bill_no,)), "报账单 " + bill_no)
    b["details"] = db.query("SELECT expense_item, amount, alloc_dept FROM expense_detail "
                            "WHERE bill_no=%s ORDER BY id", (bill_no,))
    b["approvals"] = db.query("""SELECT seq, node, approver, opinion, approved_at
                                 FROM approval_record WHERE bill_no=%s ORDER BY seq""",
                              (bill_no,))
    b["travelers"] = db.query("SELECT emp_no, name FROM bill_traveler WHERE bill_no=%s "
                              "ORDER BY ord", (bill_no,))
    # 附件。**不回 stored_path**：绝对路径是服务器的内部结构，前端用 id 取图就够了
    # （/api/attachments/{id}/file）。source_ref 要回——那是「这张图出自哪份 xlsx
    # 的哪一行哪个 media」，是事后回查原件的唯一入口。
    b["attachments"] = db.query(
        """SELECT id, attach_type, file_name, pages, seq, byte_size, content_type,
                  source_kind, source_ref, type_code, type_source, type_conf,
                  -- 有没有图可取。**前端要按这个判，不要按 source_kind 判**：
                  -- 原来缩略图写的是 `source_kind === 'xlsx内嵌'`，于是演示单配了
                  -- 合成示意图之后，页面照旧显示「无原图」——条件写死在来源上，
                  -- 就跟不上数据的变化。stored_path 本身不回（那是服务器绝对路径）。
                  (stored_path IS NOT NULL) AS has_file
             FROM attachment WHERE bill_no=%s
            ORDER BY (source_kind <> 'xlsx内嵌'), seq NULLS LAST, id""", (bill_no,))
    # 附件数**两个口径并列**，页面分两行显示，谁也不覆盖谁。
    #   attach_count      真实原图数（xlsx 内嵌，点得开）。**语义不变**——
    #                     _attach_metrics_note 与「申报张数」的对比都指它。
    #   attach_demo_count 演示合成数（类型是生成器给的，图是带水印的示意图）
    # 实测踩过：只摆一个口径且标签写「实际附件数」，演示单于是出现
    # 「头部写『附件（2 个）』、指标卡写『实际附件数 0』」同页两个数对不上。
    b["attach_count"] = sum(1 for a in b["attachments"] if a["source_kind"] == "xlsx内嵌")
    b["attach_demo_count"] = sum(1 for a in b["attachments"]
                                 if a["source_kind"] == "演示合成")
    # 「申报张数」与「实际附件数」是**两个指标**，页面并列显示，谁也不覆盖谁。
    # 实测 34/40 张单两者不等，而那不等于「申报不实」（见迁移 0035 的列注释）。
    b["_attach_metrics_note"] = (
        "「附件张数」(%s) 来自**审批意见 PDF** 的申报值；「实际附件数」(%d) 是从 xlsx "
        "内嵌图片里抽出来、真的落了盘的那几张。**两者是两个指标，不是一个**——"
        "实测 40 张真实单里 34 张不等（申报多 32、申报少 2）。"
        "「张数」可能按纸质页数算、一张图可能是两页合并、申报数也可能含未扫描的原件，"
        "所以**没有**把不等做成规则：在口径搞清楚之前，那会是一条 32/40 命中的误报机。"
        % (b.get("attach_pages") if b.get("attach_pages") is not None else "未填",
           b["attach_count"]))
    # **这段文案必须跟 attach_type 的实测值走，一个字都不许写死。**
    # 实测踩过（2026-10-23 修）：原文在 `if attachments and attach_count` 下
    # **无条件**断言「附件类型全部为空、TR-D6-01 仍然不能开」——那是 0/219 已判时
    # 写的真话；等 attachment_classify.py 把 219 条全判完之后，它变成了每张真实单上
    # 都加粗显示的**假告警**，使用者读到的结论是「附件没弄好」，于是来问
    # 「怎么真实数据和模拟数据都没有附件」。**假话比没有这句话更危险。**
    # 同一次还修掉了演示单落进 if/elif 空档（attachments 非空而 attach_count=0，
    # 两个分支都不进，_attachment_note 压根没赋值）导致的「零解释」。
    real_atts = [a for a in b["attachments"] if a["source_kind"] == "xlsx内嵌"]
    demo_atts = [a for a in b["attachments"] if a["source_kind"] == "演示合成"]
    untyped = [a for a in real_atts if not a["attach_type"]]
    # 合成图是否已生成，也按实测判：库里 content_type 有值才算有图。
    # 不写死「演示单有图」——seed.py 的 clear() 会清表重建，重建后又会变成没图。
    demo_with_img = [a for a in demo_atts if a["content_type"]]
    if not b["attachments"]:
        b["_attachment_note"] = (
            "本单没有附件记录。注意区分两件事：真实单的附件已于 2026-10-21 从 xlsx "
            "内嵌图片抽取入库（219 张，覆盖 40 张真实单）；**演示单（is_demo）的附件是合成的**。"
            "这一单两者都没有。")
    elif real_atts and untyped:
        b["_attachment_note"] = (
            "本单 %d 张真实附件里**还有 %d 张没判类型**——「没判」不是「没有类型」。"
            "从一张 PNG 判它是出差审批单还是住宿费发票必须读图"
            "（必要附件决策表 15 种细类里 7 种标着 ocr_needed）。未判完之前"
            "**TR-D6-01（必要附件完整性）不能开**：缺的那几张到底是「真的没附件」"
            "还是「还没判」，分不清。" % (len(real_atts), len(untyped)))
    elif real_atts:
        b["_attachment_note"] = (
            "本单 %d 张真实附件的类型**已全部判出**（读图判定，原图点得开）。"
            "**TR-D6-01（必要附件完整性）目前仍未开启**——这是待定的项目决策，"
            "**不是数据不够**：全库 219 张真实附件已全部判完。"
            "开它就会开始对真实单报「缺件」，口径要先定。" % len(real_atts))
    elif demo_with_img and len(demo_with_img) == len(demo_atts):
        b["_attachment_note"] = (
            "本单 %d 条附件**全部是演示合成的**：类型和文件名由生成器给定，"
            "图是带「演示数据」水印的合成示意图，**不是真实凭证**——"
            "不要拿它验证识别效果。真实单的附件是从客户 xlsx 内嵌图片抽出来的真图。"
            % len(demo_atts))
    else:
        b["_attachment_note"] = (
            "本单 %d 条附件是**演示合成**的，而且其中 %d 条连合成示意图都还没生成"
            "（只有类型和文件名，点开会是 404）。真实单的附件不受影响。"
            % (len(demo_atts), len(demo_atts) - len(demo_with_img)))
    # 最近一次审核结论 + 违规规则（供单据详情页展示「违规了哪些制度」）
    # rule_name 不叫 name，避免脱敏模块按字段名把规则名打成星号（踩坑三次）
    latest_run = db.query_one("""
        SELECT run_id, conclusion, hit_count, pending_count, rule_executed, finished_at
        FROM review_run WHERE bill_no=%s ORDER BY started_at DESC LIMIT 1
    """, (bill_no,))
    if latest_run:
        if (latest_run.get("hit_count") or 0) > 0:
            latest_run["hits"] = db.query("""
                SELECT rf.rule_id, rl.name AS rule_name,
                       rf.control_type, rf.message, rf.needs_human
                FROM review_finding rf
                LEFT JOIN rule rl ON rl.rule_id = rf.rule_id AND rl.version = rf.rule_version
                WHERE rf.run_id = %s AND rf.outcome = 'hit'
                ORDER BY rf.rule_id
            """, (latest_run["run_id"],))
        else:
            latest_run["hits"] = []
        if (latest_run.get("pending_count") or 0) > 0:
            latest_run["pending"] = db.query("""
                SELECT rf.rule_id, rl.name AS rule_name, rf.message, rf.pending_reason
                FROM review_finding rf
                LEFT JOIN rule rl ON rl.rule_id = rf.rule_id AND rl.version = rf.rule_version
                WHERE rf.run_id = %s AND rf.outcome = 'pending'
                ORDER BY rf.rule_id
            """, (latest_run["run_id"],))
        else:
            latest_run["pending"] = []
    b["latest_review"] = latest_run
    return b


@router.get("/reviews/{run_id}")
def get_review(run_id: str):
    """一份审核报告。**三个数必须并列出现。**

    只报「命中 N 条」会让人以为其余都查过了。
    执行多少、命中多少、判不了多少、被剔除多少——四个数缺一不可。
    """
    r = must_exist(db.query_one("SELECT * FROM review_run WHERE run_id=%s", (run_id,)),
                   "审核记录 " + run_id)
    findings = db.query("""SELECT rule_id, rule_class, control_type, outcome, message,
                           evidence, clause_ref, matched_text, pending_reason,
                           llm_involved, llm_rationale, needs_human
                           FROM review_finding WHERE run_id=%s
                           ORDER BY CASE outcome WHEN 'hit' THEN 0 WHEN 'pending' THEN 1
                                                 ELSE 2 END, rule_id""", (run_id,))
    hit = [f for f in findings if f["outcome"] == "hit"]
    pending = [f for f in findings if f["outcome"] == "pending"]
    passed = [f for f in findings if f["outcome"] == "pass"
              and not (f["evidence"] or {}).get("not_applicable")]
    na = [f for f in findings if f["outcome"] == "pass"
          and (f["evidence"] or {}).get("not_applicable")]
    return {
        "run": r,
        "coverage": {
            "candidates": r["rule_candidates"], "executed": r["rule_executed"],
            "excluded": len(r["rule_excluded"] or []),
            "hit": len(hit), "pending": len(pending),
            "passed": len(passed), "not_applicable": len(na),
            "note": "「不适用」是 scope 不覆盖本单，不等于「已查且合规」。"
                    "「待人工」更不等于「通过」——把它算进通过，"
                    "就等于声称复核了没复核的东西。",
        },
        "findings": {"hit": hit, "pending": pending, "passed": passed,
                     "not_applicable": na},
        # 被剔除的规则单列一节。审核岗看到「执行 12 条」时必须同时看到这个。
        # 这是**存库的 JSON**（老行里键就叫 name），所以在出口处转键：
        # 叫 name 会被人名脱敏吃掉，报告上「没跑的规则」整列变星号。
        "excluded_rules": [dict(x, rule_name=x.pop("name", None))
                           if isinstance(x, dict) and "name" in x else x
                           for x in (r["rule_excluded"] or [])],
        "excluded_note": "这些规则本次**没有执行**。不是通过，是没跑。"
                         "让 AI 声称复核了它读不到的数据，比少复核更危险。",
        "ai_is_advisory": {
            "value": True,
            "why": "AI 结论不阻断流程。实测 FJPG-BX2610130821 在 2026-10-13 17:25:03 "
                   "得到「AI审核不通过」，17:35:41 仍进入共享审核并由人工给出退回理由。"
                   "闸门在人工复核，不在这份报告。",
        },
    }


@router.get("/bills/{bill_no}/reviews")
def bill_reviews(bill_no: str):
    return {"items": db.query("""SELECT run_id, conclusion, hit_count, pending_count,
                                 rule_executed, rule_candidates, started_at, finished_at,
                                 rule_set_version, ontology_version
                                 FROM review_run WHERE bill_no=%s
                                 ORDER BY started_at DESC""", (bill_no,))}


# ─────────────────────────────────────────────────────────────────────────────
# 人工复核闸门 —— **真正的闸在这里**
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/reviews/{run_id}/manual")
def manual_review(run_id: str, body: dict):
    r = must_exist(db.query_one("SELECT bill_no FROM review_run WHERE run_id=%s", (run_id,)),
                   "审核记录 " + run_id)
    decision = body.get("decision")
    if decision not in ("agree", "return", "agree_with_note"):
        raise HTTPException(400, "decision 只能是 agree / return / agree_with_note")
    if decision == "return" and not body.get("return_reason"):
        raise HTTPException(400, "退回必须给理由——退回理由是误报/漏报回流的起点")
    import json
    with rec.phase("A"):
        with db.tx() as conn:
            rec.guard_write("manual_review")
            conn.cursor().execute("""INSERT INTO manual_review
                (bill_no, run_id, reviewer, decision, return_reason,
                 disagree_with, agree_with)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (r["bill_no"], run_id, body.get("reviewer") or "unknown", decision,
                 body.get("return_reason"),
                 json.dumps(body.get("disagree_with") or [], ensure_ascii=False),
                 json.dumps(body.get("agree_with") or [], ensure_ascii=False)))
    return {"ok": True,
            "note": "人工结论已记录。disagree_with 里的每一条都会回流成 "
                    "feedback_log(deviation_kind=false_positive)。"}


# ── 单据附件 ────────────────────────────────────────────────────────────────
# 附件是**真实发票与出差申请单的扫描图**，里面有姓名、身份证号片段、金额。
# 两条安全口径：
#
# 1. **只按 id 取图，不接受外来路径。** 第一版很容易写成
#    `GET /file?path=...`，那等于把整个服务器磁盘开给前端。
#    这里拿 id 查库取 stored_path，再强制校验它确实落在 ATTACH_DIR 之下——
#    库里的值理论上是我们自己写的，但「理论上可信的输入」正是最容易出事的那种。
#
# 2. **图片出口不过脱敏中间件。** MaskMiddleware 只处理 JSON 响应，二进制直接穿过去。
#    这是**有意的**：审核岗必须看到发票原件才能复核，把发票打上马赛克等于
#    让这套系统没法用。但它意味着**这个接口会把客户的个人信息原样发给浏览器**，
#    所以它只在内网 POC 里成立；对外提供必须先有登录与权限。这一条写在这里，
#    不是写在某份文档里——下一个人改这个接口时看得到。
from fastapi.responses import FileResponse   # noqa: E402
from .. import config                        # noqa: E402


def _under(path, base):
    """path 是否落在 base 之下。→ bool

    **刻意不用 os.path.commonpath。** 实测（2026-10-21）：Windows 上两个路径
    在不同盘符时它抛 `ValueError: Paths don't have the same drive`，
    于是这道闸是以 500 崩掉的方式「拦住」的——文件确实没发出去，
    但接口回的是「服务器故障」而不是「这个路径不许读」，
    而一个会抛异常的安全检查，下一个人很容易用 try/except 包起来然后 pass 掉。

    normcase 也是必须的：Windows 路径大小写不敏感，
    `d:\\...\\var\\attachments` 和 `D:\\...\\var\\Attachments` 是同一个目录，
    用区分大小写的比较会把合法路径判成越界。
    """
    p = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(base)).rstrip("\\/")
    return p == b or p.startswith(b + os.sep)


@router.get("/attachments/{att_id}/file")
def attachment_file(att_id: int):
    """取一张附件原图。"""
    row = must_exist(db.query_one(
        "SELECT bill_no, file_name, stored_path, content_type, source_kind "
        "FROM attachment WHERE id=%s", (att_id,)), "附件 %d" % att_id)
    path = row["stored_path"]
    if not path:
        raise HTTPException(404, detail=(
            "这条附件记录没有原图。source_kind=%s —— **演示单的附件是合成的**，"
            "只有类型和文件名，没有图片。" % row["source_kind"]))
    # 规范化之后必须仍在 ATTACH_DIR 之下，否则一律拒绝
    if not _under(path, config.ATTACH_DIR):
        raise HTTPException(400, detail=(
            "这条附件记录的路径不在附件存储区（%s）内，拒绝读取。"
            "附件只能从附件区读——库里的值理论上是我们自己写的，"
            "但「理论上可信的输入」正是最容易出事的那种" % config.ATTACH_DIR))
    full = os.path.abspath(path)
    if not os.path.isfile(full):
        raise HTTPException(404, detail=(
            "登记里有这条附件，但磁盘上找不到文件：%s。"
            "**不自动删登记**——登记在而文件不在，是需要人去查的状态，"
            "悄悄清掉等于把问题藏起来" % row["file_name"]))
    return FileResponse(full, media_type=row["content_type"] or "image/png",
                        filename=row["file_name"])


@router.get("/attachments/status")
def attachment_status():
    """抽了多少、和申报张数差多少、还差什么才能开 TR-D6-01。"""
    from ..perception import attachment_extract as ax
    return ax.status()


# ── 附件读图判类型 ───────────────────────────────────────────────────────────
# **真出网、真计费**，而且发出去的是真实发票与出差申请单（含姓名、身份证号片段、
# 金额）。单独一组接口、单独一个按钮——点「附件分类」之前，使用者必须先看到
# 「这一下要读几张图、花多少钱」，不能和不计费的抽取动作混在一个按钮里。
from ..perception import attachment_classify as acx   # noqa: E402


@router.get("/attachments/classify/pending")
def attachment_classify_pending(limit: int = Query(500, ge=1, le=2000)):
    """还没判类型的真实附件。**点「开始分类」之前先看这份清单。**"""
    rows = acx.pending(limit)
    return {"items": rows, "total": len(rows),
            "note": "只列从 xlsx 抽出来的真实附件（source_kind=xlsx内嵌）；"
                    "演示数据的类型是生成器直接给的，不进这份清单。"}


@router.post("/attachments/classify")
def attachment_classify(body: dict = Body(None)):
    """批量读图判类型。**这一下会真出网、真计费。**

    一次最多 limit 条（上限 100），不传 attachment_ids 就按「还没判类型的」顺序挑。
    传 attachment_ids=[] 等于明确说「这次不处理任何一条」，不是「全部」。
    """
    b = body or {}
    ids = b.get("attachment_ids")
    try:
        return acx.run(attachment_ids=ids, limit=int(b.get("limit") or 20),
                       config_id=b.get("config_id"))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/attachments/classify/status")
def attachment_classify_status():
    """分类进度、费用留痕（累计 token）、类型分布。"""
    return acx.status()


# ── 附件字段抽取 ────────────────────────────────────────────────────────────
# **真出网、真计费**，是判类型之后的第二步。单独一组接口——和分类一样，
# 点「开始抽取」之前使用者要先看到「这一下要读几张图」。
# 2026-10-22 用户决策：这一次只抽文字/日期/布尔字段，金额税额不落明文。
from ..perception import attachment_extract_fields as aex  # noqa: E402


@router.get("/attachments/extract/pending")
def attachment_extract_pending(limit: int = Query(500, ge=1, le=2000)):
    """已判过类型、在抽取范围内（A01/A03/A04/A05）、但还没抽字段的附件。"""
    rows = aex.pending(limit)
    return {"items": rows, "total": len(rows)}


@router.post("/attachments/extract")
def attachment_extract(body: dict = Body(None)):
    """批量抽字段。**这一下会真出网、真计费。** 只问文字/日期/布尔字段，不问金额税额。

    一次最多 limit 条（上限 100），不传 attachment_ids 就按「还没抽的」顺序挑。
    """
    b = body or {}
    ids = b.get("attachment_ids")
    try:
        return aex.run(attachment_ids=ids, limit=int(b.get("limit") or 20),
                       config_id=b.get("config_id"))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/attachments/extract/status")
def attachment_extract_status():
    """抽取进度、费用留痕。"""
    return aex.status()


@router.get("/attachments/perception")
def attachment_perception(failures: int = Query(50, ge=0, le=500)):
    """附件感知链路一次看全：判类型 + 抽字段的进度、成本、失败。

    **做成一个合并接口而不是让前端打四次**：这四个数必须同时出现在一屏上
    才有意义。分四次拉，中间任何一次失败都会让页面显示一个「进度 219/219
    但成本空白」的半截状态，而那看起来像「没花钱」。

    失败明细**默认带上**（最近 50 条）。读图是真出网真计费的动作，
    把失败藏在另一个要点一下才展开的接口里，等于默认不给看——
    实测 49 次失败在改这一版之前页面上一个字都没有。
    """
    from .. import perception
    clf = acx.status()
    ext = aex.status()
    clf_f = perception.ledger_failures("attachment_vision", failures)
    ext_f = perception.ledger_failures("attachment_extract", failures)
    return {
        "classify": clf,
        "extract": ext,
        "classify_failures": clf_f,
        "extract_failures": ext_f,
        # 链路口径：两步是**串行依赖**，而且第二步的范围比第一步小。
        # 不写这一句，看到「判类型 219 / 抽字段 197」会以为少了 22 张。
        "chain_note": (
            "两步串行：先判类型（全部 %d 张真实附件），再按类型抽字段"
            "（只有 A01/A03/A04/A05 四类进抽取范围，共 %d 张）。"
            "**抽字段的分母比判类型小不是漏了**——出差审批单、交通费凭证、"
            "住宿费发票、其他差旅费发票之外的类型这一轮不抽字段。"
            % (clf.get("total") or 0, ext.get("eligible") or 0)),
        "cost_note": (
            "token 是累计口径（两张台账全量，含失败的调用）。"
            "**失败的调用也花钱**，所以成本要算在一起看；"
            "但失败次数不等于待办，见各步的失败说明。"),
    }


@router.get("/deviation")
def deviation():
    """误报与漏报**分开**统计，不合成准确率。"""
    rows = db.query("""SELECT deviation_kind, count(*) n FROM feedback_log
                       WHERE metric IN ('conclusion_vs_human','finding_vs_human')
                       GROUP BY 1 ORDER BY 2 DESC""")
    by_rule = db.query("""SELECT actual->>'rule_id' AS rule_id, count(*) n
                          FROM feedback_log
                          WHERE metric='finding_vs_human' AND deviation_kind='false_positive'
                            AND actual->>'rule_id' IS NOT NULL
                          GROUP BY 1 ORDER BY 2 DESC LIMIT 20""")
    return {"by_kind": rows, "false_positive_by_rule": by_rule,
            "kinds": {
                "false_positive": "AI 命中了规则，人工却放行 —— 真误报，代价最高",
                "false_negative": "AI 放行，人工退回 —— 漏报",
                "needs_review_only": "AI 只是有待人工项，人工放行 —— **不是误报**，是没把握",
                "null": "一致",
            },
            "note": "漏报放过风险；误报把正常单据判成问题件，审核岗很快就不再信任这套系统。"
                    "合成一个「准确率」会把误报的代价藏起来，所以这里不给那个数。"
                    "「判不了」单列，不混进误报——混进去会虚报误报率。"}


# ─────────────────────────────────────────────────────────────────────────────
# 审计接口 —— **旁路出口，必须过同一套脱敏**
# ─────────────────────────────────────────────────────────────────────────────
audit_router = APIRouter(prefix="/api/audit", tags=["审计"])


@audit_router.get("/snapshots")
def snapshots(subject_id: str = None, page: int = 1, size: int = 20):
    w, p = ("WHERE subject_id=%s", (subject_id,)) if subject_id else ("", ())
    return paginate("SELECT count(*) FROM data_pool_snapshot " + w,
                    """SELECT snapshot_id, agent_id, subject_id, taken_at, payload_hash,
                       masked, source_tables, row_counts, payload
                       FROM data_pool_snapshot """ + w + " ORDER BY taken_at DESC", p,
                    page, size)


@audit_router.get("/cycles")
def cycles(subject_id: str = None, status: str = None, page: int = 1, size: int = 20):
    where, params = [], []
    if subject_id:
        where.append("subject_id=%s")
        params.append(subject_id)
    if status:
        where.append("status=%s")
        params.append(status)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    return paginate("SELECT count(*) FROM cycle_log" + w,
                    """SELECT cycle_id, agent_id, agent_version, rule_set_version,
                       ontology_version, subject_id, snapshot_id, trigger_kind,
                       trigger_reason, rule_candidates, rule_executed, rule_excluded,
                       rule_hits, decision, phase_timings_ms, status, error,
                       started_at, finished_at
                       FROM cycle_log""" + w + " ORDER BY started_at DESC",
                    tuple(params), page, size)


@audit_router.get("/trace/{subject_id}")
def trace(subject_id: str):
    """一张单的全过程：S 看到什么 → D 判了什么 → A 写了什么 → F 偏差多少 → I 调了什么。

    五张表分开存的价值就在这个接口上：合成一张「日志表」之后，
    「模型看到的输入」和「模型造成的写入」就混在同一批行里，这里没法分开追。
    """
    return {
        "subject_id": subject_id,
        "S_snapshots": db.query("""SELECT snapshot_id, taken_at, payload_hash, masked,
                                   source_tables, row_counts FROM data_pool_snapshot
                                   WHERE subject_id=%s ORDER BY taken_at""", (subject_id,)),
        "D_cycles": db.query("""SELECT cycle_id, trigger_kind, rule_candidates, rule_executed,
                                decision, phase_timings_ms, status, started_at
                                FROM cycle_log WHERE subject_id=%s ORDER BY started_at""",
                             (subject_id,)),
        "A_actions": db.query("""SELECT l.emitted_at, l.action, l.target, l.written_tables,
                                 l.idempotency_key FROM agent_signal_log l
                                 JOIN cycle_log c ON c.cycle_id=l.cycle_id
                                 WHERE c.subject_id=%s ORDER BY l.emitted_at""", (subject_id,)),
        "F_feedback": db.query("""SELECT observed_at, metric, expected, actual,
                                  deviation, deviation_kind, source FROM feedback_log
                                  WHERE subject_id=%s ORDER BY observed_at""", (subject_id,)),
        # I 阶段按**批次**调参，不是按单。所以 improve_log.cycle_id 通常为空，
        # 按 cycle_id 关联永远查不到——第一版就是这么写的，结果这一节永远是空的，
        # 看着像「从来没调过参」。
        # 正确做法：取这张单所涉 Agent 的近期调参，并标明它是批次级的。
        "I_improvements": db.query("""SELECT i.applied_at, i.agent_id, i.param,
                                      i.old_value, i.new_value, i.rejected,
                                      i.rejected_why, i.reason, i.bounded_by,
                                      (i.cycle_id IS NOT NULL) AS per_cycle
                                      FROM improve_log i
                                      WHERE i.agent_id IN (
                                        SELECT DISTINCT agent_id FROM cycle_log
                                        WHERE subject_id = %s)
                                      ORDER BY i.applied_at DESC LIMIT 20""",
                                   (subject_id,)),
        "I_scope_note": "I 阶段按**批次**调参，不是按单——单个周期的数字噪声太大，"
                        "按它调会让参数抖来抖去。所以这一节列的是本单所涉 Agent 的"
                        "近期调参记录，per_cycle=false 表示该条是批次级的。",
    }


@audit_router.get("/consistency")
def consistency():
    ok, info = rec.audit_consistency()
    return {"ok": ok, **info}


@audit_router.get("/quarantine")
def quarantine():
    """隔离区不为空就是一个**要处理的事实**，不是日志噪音。

    里面每一行都代表一笔业务数据当前不被任何规则审核。
    """
    rows = db.query("""SELECT id, loaded_at, source, target, reason, gap_id, row_data,
                       resolved FROM load_quarantine ORDER BY loaded_at DESC""")
    return {"items": rows, "unresolved": sum(1 for r in rows if not r["resolved"]),
            "note": "覆盖面声明里必须把它算进去——"
                    "「本批 40 张单全部审核完毕」和「40 张单 + 8 行游离明细未审」是两句话。"}


# ─────────────────────────────────────────────────────────────────────────────
# 调试接口 —— **另一个旁路出口**
# ─────────────────────────────────────────────────────────────────────────────
debug_router = APIRouter(prefix="/api/debug", tags=["调试"])


@debug_router.get("/signals")
def signals():
    return {"items": db.query("""SELECT signal_id, channel, kind, subject_id, status,
                                 attempts, max_attempts, last_error, payload,
                                 claimed_by, created_at FROM agent_signal
                                 ORDER BY id DESC LIMIT 100"""),
            "by_status": db.query("SELECT status, count(*) n FROM agent_signal GROUP BY 1")}


@debug_router.get("/agent-state")
def agent_state():
    return {"items": db.query("SELECT * FROM agent_state ORDER BY agent_id")}


@debug_router.get("/fact/{bill_no}")
def debug_fact(bill_no: str):
    """直接看 S 阶段会捞到什么。**这是最容易漏脱敏的一条路**——
    它返回的是原始事实对象，不经过报告生成。"""
    from ..perception import reader
    fact, meta = reader.snapshot_fact(bill_no)
    if fact is None:
        raise HTTPException(404, "单据 %s 不存在" % bill_no)
    payload, masked, leaks = reader.masked_payload(fact)
    return {"meta": meta, "masked": masked, "leaks": leaks, "payload": payload}
