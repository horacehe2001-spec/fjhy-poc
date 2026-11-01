# -*- coding: utf-8 -*-
r"""822港航 读图批：业务接待审批单 / 会议培训通知 / 出差审批单里**没有文字层**的那些。

用法（**不要加 -I**，理由同 load_822gh_bills）：

    cd 服务端
    python -m app.demo.read_822gh_vision_batch              # 干跑：只算范围、不出网
    VISION=1 python -m app.demo.read_822gh_vision_batch     # 真出网、真计费
    VISION=1 ONLY=3 python -m app.demo.read_822gh_vision_batch   # 先试跑 3 份
    VISION=1 TYPE=A16 ONLY=2 python -m app.demo.read_822gh_vision_batch  # 挑类型试跑

  TYPE/ONLY 存在的理由是**先试跑再全量**：提示词是新写的，42 份一次全发出去，
  万一标签没对上票面（比如 _LINE_RE 只认纯汉字标签那件事），
  就是 42 次调用全废。先 2 份看回答原文，再放量。

【这个程序存在的唯一理由：把「该读图的」和「已经免费解出来的」分开】
  attachment_extract_fields.pending() 的口径是「已定类、类型在抽取范围内、
  且 attachment_extract 里没有记录」。按它跑会**把 18 份有文字层的 PDF 一起
  发出去**——那 18 份（出差审批单 15 / 业务接待审批单 2 / 培训通知 1）的字段
  早就由 load_822gh_attachments 和 load_822gh_entertain_approval 零成本解出来
  并入库了，再读一遍是纯浪费，而且读图的结果还会覆盖掉解析得更准的那一份。
  所以这里**逐个文件探一次文字层**，有文字层的一律排除，并把排除清单打出来。

  「先看文件再估工作量」这件事在本项目上已经量过两次：
  388 个 PDF 有文字层，零模型调用解出发票 53、出差审批单 15。

【范围（2026-11-01 实测）】
  A16 业务接待审批单  27 份：26 份 JPG + 1 份扫描 PDF（另 2 份 PDF 有文字层）
  A06 会议/培训通知    7 份：全是扫描 PDF（另 1 份有文字层）
  A01 出差审批单       8 份：2 份 JPG + 6 份扫描 PDF（另 15 份有文字层）
  合计 42 次调用。PDF 由 attachment_extract_fields 里的 _as_image 渲成 PNG
  再发（渲不出来就抛，一次调用都不花）。

【为什么不把 A17/A18/A19/A02/A14 也读了】
  A17 业务接待结算单 5 份、A19 酒水领用单 2 份、A02 差旅费明细表 2 份、
  A14 情况说明 1 份——这几类票面上**主要是金额**，而本模块的提示词按
  2026-10-22 用户决策写死了「不要回答任何金额」，读回来也落不了库
  （库里也没有对应的列）。要不要放开金额是**口径决策，决策权在用户**，
  不在这一批里自己定。这 10 份原样留着，台账上也不会出现它们。
"""
import io
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

# 见 fix_822gh_trip_fields 文件头：load_822gh_attachments 是**顶层就执行**的
# 脚本，import 它等于跑一遍全量解析；带着 WRITE=1 import 它会真写库并
# 删附件。本程序压根不 import 它（文字层探测自己用 fitz 做），
# 这一句是双保险。
os.environ.pop("WRITE", None)

import fitz                                                      # noqa: E402

from app import config, db                                        # noqa: E402
from app.perception import attachment_extract_fields as F         # noqa: E402

IID = "822gh"
TYPES = ["A16", "A06", "A01"]
GO = os.environ.get("VISION") == "1"
ONLY = int(os.environ.get("ONLY") or 0)
PICK = [t for t in (os.environ.get("TYPE") or "").split(",") if t]
# IDS=6495,6504 —— 点名挑附件号。ONLY 只能取「前 N 份」，而试跑要挑的是
# **有代表性的那几份**（比如一份 OA 打印件 + 一份手写勾选表），
# 顺序碰巧排在前面的不一定是它们。
IDS = [int(x) for x in (os.environ.get("IDS") or "").replace("，", ",").split(",")
       if x.strip().isdigit()]
REAPPLY = os.environ.get("REAPPLY") == "1"
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")

out = []


def p(s=""):
    out.append(str(s))
    print(s)


def has_text_layer(path):
    """这份文件有没有可直接提取的文字层。

    只对 PDF 有意义；图片一律当没有。**探不到就当没有文字层**（宁可多读一张
    图，也不要因为探测本身报错就把一份该读的漏掉）——漏掉的后果是规则
    pending，而 pending 看起来和「判过了」很像，很难被发现。
    """
    if os.path.splitext(path or "")[1].lower() != ".pdf":
        return False
    try:
        doc = fitz.open(path)
    except Exception:                                   # noqa: BLE001
        return False
    try:
        for pg in doc:
            if pg.get_text("words"):
                return True
        return False
    finally:
        doc.close()


def scope():
    rows = db.query("""SELECT a.id, a.type_code, a.bill_no, a.file_name,
                              a.content_type, a.stored_path
                         FROM attachment a
                        WHERE a.instance_id = %s AND a.type_code = ANY(%s)
                          AND a.source_kind = ANY(%s)
                        ORDER BY a.type_code, a.bill_no, a.id""",
                    (IID, TYPES, list(F.REAL_SOURCES)))
    # 已经成功读过的不再发。**这是计费链路，可重入必须是「不重复付费」**，
    # 而 attachment_extract 对同一张图重读是记成新 attempt 的（永不覆盖），
    # 所以不在这里拦，重跑一次就是重新付一次全款。
    # REDO=1 才把它们放回来（改了提示词想重读时用）。
    done = set() if os.environ.get("REDO") == "1" else {
        r["attachment_id"] for r in db.query(
            """SELECT DISTINCT e.attachment_id FROM attachment_extract e
                 JOIN attachment a ON a.id = e.attachment_id
                WHERE a.instance_id = %s AND e.ok""", (IID,))}
    need, free, broken, already = [], [], [], []
    for r in rows:
        if not r["stored_path"] or not os.path.exists(r["stored_path"]):
            broken.append(r)
        elif has_text_layer(r["stored_path"]):
            free.append(r)
        elif r["id"] in done:
            already.append(r)
        else:
            need.append(r)
    return need, free, broken, already


def reapply():
    """**不花一分钱**，拿台账里已经存着的模型回答重放一遍回写。

    改了回写闸（比如「没有接待时间就不建行」）之后要让存量跟着变，
    靠重读图是最蠢的做法——图发出去一次 2700 个 prompt token，
    而回答原文和解析结果 attachment_extract 里一个字都没丢。

    先把**读图写的**业务行清掉再重放，三张表都按
    extracted_from='审批单读图' 删——文字层那一批一行都不碰。

    **meeting_notice 清不掉，这是已知的缺口，不要当它干净了。**
    那张表是 UNIQUE(bill_no)，读图和文字层写进**同一行**（按字段 COALESCE
    合并），没有来源列可以区分，所以这里不动它：对 A06 来说重放是叠加、
    不是重建。改了 A06 的闸要让存量跟着变，得 REDO=1 重读（要花钱），
    或者先手工把 notice_start/notice_end/notice_place 三列清掉再重放。
    """
    db.execute("""DELETE FROM approval_record
                   WHERE instance_id=%s AND extracted_from=%s""",
               (IID, "审批单读图"))
    db.execute("""DELETE FROM entertain_approval
                   WHERE instance_id=%s AND extracted_from=%s""",
               (IID, "审批单读图"))
    # trip_calendar 是 ON DELETE CASCADE 挂在 trip_application 上。
    # **第四轮起读图自己也写逐日行了**（A01 的提示词补了那两张小表），
    # 所以「挂着逐日行就中止」这道闸的原始写法已经不适用——
    # 它会把每一次正常重放都挡死。闸要换个问法，但不能撤：
    # 真正的危险是**删掉重放补不回来的行**，也就是挂在「这一批台账里
    # 没有对应记录」的审批单下面的逐日行。2026-11-01 第三轮差点删掉的
    # 90 行就属于这一类（它们是文字层写的，台账里一条都没有）。
    orphan = db.scalar("""
        SELECT count(*) FROM trip_calendar c
          JOIN trip_application t ON t.app_no = c.app_no
         WHERE t.instance_id=%s AND t.extracted_from=%s
           AND t.app_no NOT IN (SELECT %s || e.attachment_id
                                  FROM attachment_extract e
                                  JOIN attachment a ON a.id = e.attachment_id
                                 WHERE a.instance_id=%s AND e.ok
                                   AND e.fields IS NOT NULL)""",
        (IID, "审批单读图", "ATT-", IID)) or 0
    if orphan:
        raise SystemExit("中止：读图来源的出差审批单下面挂着 %d 行逐日记录，"
                         "而它们对应的台账记录找不到——重放补不回这些行，"
                         "删了就是真没了" % orphan)
    db.execute("""DELETE FROM trip_application
                   WHERE instance_id=%s AND extracted_from=%s""",
               (IID, "审批单读图"))
    # 每张图只重放**最后一次成功的** attempt。同一张图重读是新 attempt、
    # 永不覆盖，拿旧 attempt 重放等于把后来读得更准的那次盖掉。
    rows = db.query("""
        SELECT DISTINCT ON (e.attachment_id)
               e.id, e.attachment_id, a.bill_no, a.file_name, a.instance_id,
               e.type_code, e.fields, e.confidence, e.model_name
          FROM attachment_extract e
          JOIN attachment a ON a.id = e.attachment_id
         WHERE a.instance_id = %s AND e.ok AND e.fields IS NOT NULL
         ORDER BY e.attachment_id, e.attempt DESC""", (IID,))
    import json as _json
    n_ok, n_skip = 0, 0
    for r in rows:
        if r["confidence"] not in ("高", "中"):
            n_skip += 1
            continue
        f = r["fields"]
        if isinstance(f, str):
            f = _json.loads(f)
        to = F._apply(r["type_code"], r["bill_no"], r["attachment_id"], f,
                      r["model_name"], r["confidence"], r["instance_id"])
        # **只改这一条台账行，不改这张图的所有 attempt。**
        # 台账是「永不覆盖」的（同一张图重读记成新 attempt），
        # 按 attachment_id 更新会把历史 attempt 的回写结论一起改掉，
        # 那就等于篡改了「当时那次读图回写了什么」。
        db.execute("""UPDATE attachment_extract SET applied=%s, applied_to=%s
                       WHERE id=%s""", (bool(to), to, r["id"]))
        if to:
            n_ok += 1
            p("  回写 %s %s → %s" % (r["bill_no"], r["file_name"][:40], to))
        else:
            n_skip += 1
    p("")
    p("=== 重放完成：台账 %d 条，回写 %d，按闸挡下 %d（0 次模型调用）==="
      % (len(rows), n_ok, n_skip))
    return 0


def main():
    need, free, broken, already = scope()
    p("=== 有文字层、已零成本解出，**不读图** %d 份 ===" % len(free))
    for r in free:
        p("  %s %s  %s" % (r["type_code"], r["bill_no"], r["file_name"][:60]))
    if already:
        p("")
        p("=== 已经读过且成功 %d 份，不重复付费（REDO=1 可重读）===" % len(already))
        for r in already:
            p("  %s %s  %s" % (r["type_code"], r["bill_no"], r["file_name"][:60]))
    if broken:
        p("")
        p("=== 原图不在盘上 %d 份（不发，也不算进范围）===" % len(broken))
        for r in broken:
            p("  %s %s  %s" % (r["type_code"], r["bill_no"], r["file_name"][:60]))

    from collections import Counter
    p("")
    p("=== 要读图 %d 份：%s ===" % (len(need),
      " / ".join("%s %d" % kv for kv in sorted(Counter(
          r["type_code"] for r in need).items()))))
    for r in need:
        p("  %s %s  %-8s %s"
          % (r["type_code"], r["bill_no"],
             "PDF渲页" if (r["content_type"] or "").endswith("pdf") else "图片",
             r["file_name"][:60]))

    ids = [r["id"] for r in need if not PICK or r["type_code"] in PICK]
    if PICK:
        p("")
        p("（TYPE=%s：只挑这些类型，%d 份）" % (",".join(PICK), len(ids)))
    if IDS:
        miss = [i for i in IDS if i not in ids]
        ids = [i for i in ids if i in IDS]
        p("")
        p("（IDS=%s：点名 %d 份%s）"
          % (",".join(str(i) for i in IDS), len(ids),
             ("；不在待读范围里的 %s" % miss) if miss else ""))
    if ONLY:
        ids = ids[:ONLY]
        p("")
        p("（ONLY=%d：只跑前 %d 份）" % (ONLY, len(ids)))

    if REAPPLY:
        return reapply()

    if not GO:
        p("")
        p("（干跑，没出网。设 VISION=1 才真调用、真计费）")
        return 0

    p("")
    p("=== 开始读图：%d 次调用，真出网、真计费 ===" % len(ids))
    res = F.run(attachment_ids=ids, limit=len(ids), instance_id=IID)
    p("  调用 %d / 回写 %d / 失败 %d"
      % (res["tried"], res["applied"], res["failed"]))
    p("")
    for it in res["items"]:
        p("  %s %s" % (it["bill_no"], (it.get("file_name") or "")[:46]))
        p("     ok=%s 把握=%s 回写=%s %s"
          % (it.get("ok"), it.get("confidence"), it.get("applied_to"),
             ("· %s" % it.get("message")) if not it.get("ok") else ""))
        f = it.get("fields") or {}
        # **接待对象/签批人姓名一个都不问**，所以这里打印全字段是安全的
        # （A16_FIELDS 里没有任何人名字段，见 attachment_extract_fields）。
        if f:
            p("     %s" % {k: v for k, v in f.items() if v is not None})

    p("")
    p("=== 台账与业务表 ===")
    st = F.status(instance_id=IID)
    p("  可抽取 %s / 已抽 %s / 已回写 %s / 待抽 %s"
      % (st["eligible"], st["extracted"], st["applied"], st["pending"]))
    p("  调用 %s 次（失败 %s）· 提示 token %s · 补全 token %s"
      % (st["calls"], st["calls_failed"], st["prompt_tokens"],
         st["completion_tokens"]))
    p("  把握分布：%s" % st["by_confidence"])
    for lab, q in (
            ("业务接待审批单", "SELECT count(*) FROM entertain_approval "
                          "WHERE instance_id=%s"),
            ("其中读图来的", "SELECT count(*) FROM entertain_approval "
                        "WHERE instance_id=%s AND extracted_from='审批单读图'"),
            ("业务接待审批流水", "SELECT count(*) FROM approval_record "
                          "WHERE instance_id=%s AND flow='业务接待审批'"),
            ("会议/培训通知", "SELECT count(*) FROM meeting_notice "
                        "WHERE instance_id=%s"),
            ("其中有活动起止", "SELECT count(*) FROM meeting_notice "
                         "WHERE instance_id=%s AND notice_start IS NOT NULL"),
            ("出差审批单", "SELECT count(*) FROM trip_application "
                       "WHERE instance_id=%s")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  别家没被动：fjhy 审批流水 %s / 附件 %s"
      % (db.scalar("SELECT count(*) FROM approval_record "
                   "WHERE instance_id='fjhy'"),
         db.scalar("SELECT count(*) FROM attachment WHERE instance_id='fjhy'")))
    return 0


rc = main()
os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "vision822.txt"), "w",
        encoding="utf-8").write("\n".join(out))
sys.exit(rc)
