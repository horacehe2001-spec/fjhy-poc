# -*- coding: utf-8 -*-
r"""从单据号回填 822港航 的 expense_bill.doc_date（提单日期）。

用法（**不要加 -I**）：

    cd 服务端
    python -m app.demo.fix_822gh_doc_date          # 干跑，逐张打印并跑全部闸
    WRITE2=1 python -m app.demo.fix_822gh_doc_date # 真写库（可重入）

【这是算出来的值，不是客户给的——所以闸要先过】
  单据号形如 `FJPG-BX` + YYMMDD + 4 位流水：
      FJPG-BX**260728**0255  →  2026-07-28
  判据不是「看着像日期」，是下面三条实测：
    ① 29/29 张都解得出一个合法日期
    ② **提单日 ≥ 业务发生日，29/29 张，0 条反例**
       （业务发生日取：招待单 entertain_detail.happen_date、
         差旅单 trip_application.end_date、其余 invoice.issue_date）
    ③ 解出的日期落在 2026 年内，且不晚于入账期间的次月末
  ② 是关键那条：单据号前六位如果不是提单日期，没有理由在 29 张单上
  一次不差地晚于业务发生日。三条闸任何一条不过就整个事务中止。

【为什么值得回填】
  doc_date 本实例 29 张全空，而它是下面这几条的输入：
      TR-通用-03 审批流程时效性（要「单据提交时间」）
      TR-通用-01 入账期间准确性（要「业务发生日期」那一侧的参照）
      TR-D10-06 那类拆单检测（reader 的 peer_bills_2d 按 doc_date 取邻单）
  可行性复核里这一项原来写的是「**库里没有这些字段，要先改表**」——
  那句话错了：字段一直在，只是空着，而值在单据号里。
  归因写错的后果是实打实的：「要改表」会被排进我方的开发活。

【写进去的值要标明来源 —— 但不许写进业务列】
  来源记在 `expense_bill.doc_date_source`（migration 0058 新建）。

  **第一版把标记追加进了 remark，当场把 17 条规则判坏了。**
  remark 是业务上的「补充说明区」，原来 19/29 有值，追加之后 29/29 非空，
  其中 10 张里只有我写的标记。而 TR-差旅-06/07/25/31/33、TR-接待-15、
  TR-通用-04 判的正是「补充说明区是否填写了必要信息」——
  可行性复核里「部分字段不全」当场从 17 条掉到 2 条。
  审计痕迹写进被审计的字段，判出来的「已说明」全是假的。
  业务列只装业务值，这条没有例外。
"""
import datetime
import os
import re
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

os.environ.pop("WRITE", None)

from app import db  # noqa: E402

IID = "822gh"
WRITE = os.environ.get("WRITE2") == "1"
RE_BILL = re.compile(r"^FJPG-BX(\d{2})(\d{2})(\d{2})\d{4}$")
SOURCE = "单据号推出"
# 第一版写进 remark 的那个标记。**本程序现在负责把它清掉**：
# 清不干净的话，「补充说明区填没填」那十几条规则永远判成「填了」。
BAD_MARK = "［提单日期由单据号前六位推出，非客户字段］"


def main():
    rows = db.query("""
        SELECT b.bill_no, b.doc_type, b.booking_period, b.doc_date, b.remark,
               (SELECT max(e.happen_date) FROM entertain_detail e
                 WHERE e.bill_no = b.bill_no) ent_d,
               (SELECT max(t.end_date) FROM trip_application t
                 WHERE t.bill_no = b.bill_no) trip_d,
               (SELECT max(i.issue_date) FROM invoice i
                 WHERE i.bill_no = b.bill_no) inv_d
          FROM expense_bill b
         WHERE b.instance_id = %s
         ORDER BY b.bill_no""", (IID,))
    print("本实例报账单 %d 张；doc_date 现在有值的 %d 张"
          % (len(rows), sum(1 for r in rows if r["doc_date"])))

    plan, fail = [], []
    for r in rows:
        m = RE_BILL.match(r["bill_no"])
        if not m:
            fail.append((r["bill_no"], "单据号不是 FJPG-BX + YYMMDD + 4 位流水"))
            continue
        try:
            d = datetime.date(2000 + int(m.group(1)), int(m.group(2)),
                              int(m.group(3)))
        except ValueError as e:
            fail.append((r["bill_no"], "解出的不是合法日期：%s" % e))
            continue
        biz = r["ent_d"] or r["trip_d"] or r["inv_d"]
        src = ("招待发生日期" if r["ent_d"] else
               "出差结束日期" if r["trip_d"] else
               "发票开票日期" if r["inv_d"] else None)
        # 闸②：提单日必须不早于业务发生日
        if biz and d < biz:
            fail.append((r["bill_no"],
                         "提单日 %s 早于业务发生日 %s（%s）——"
                         "单据号前六位不是提单日期" % (d, biz, src)))
            continue
        # 闸③：落在 2026 年内，且不晚于入账期间的次月末
        bp = str(r["booking_period"] or "")[:7]
        if d.year != 2026:
            fail.append((r["bill_no"], "解出的年份是 %d，不在 2026 年内" % d.year))
            continue
        if re.match(r"^\d{4}-\d{2}$", bp):
            by, bm = int(bp[:4]), int(bp[5:7])
            # 入账期间的次月末
            ny, nm = (by + 1, 1) if bm == 12 else (by, bm + 1)
            last = (datetime.date(ny + (1 if nm == 12 else 0),
                                  1 if nm == 12 else nm + 1, 1)
                    - datetime.timedelta(days=1))
            if d > last:
                fail.append((r["bill_no"],
                             "提单日 %s 晚于入账期间 %s 的次月末 %s"
                             % (d, bp, last)))
                continue
        plan.append((r["bill_no"], d, biz, src, bp, r["remark"]))
        print("  %-22s 提单=%s  业务=%s(%s)  入账=%s"
              % (r["bill_no"], d, biz, src or "-", bp))

    print()
    print("能回填 %d / %d 张" % (len(plan), len(rows)))
    if fail:
        print("**过不了闸的 %d 张**：" % len(fail))
        for bn, why in fail:
            print("  · %s —— %s" % (bn, why))
        raise SystemExit("中止：有单据过不了闸，单据号前六位不是提单日期的假设"
                         "在这批数据上不成立——不回填")

    if not WRITE:
        print("\n（干跑，没写库。设 WRITE2=1 才写）")
        return

    with db.tx() as conn:
        c = conn.cursor()
        for bn, d, _biz, _src, _bp, remark in plan:
            # remark 只做一件事：把第一版误写进去的标记清掉，
            # 清完是空串就还原成 NULL——「空」和「只有一句注释」
            # 对「补充说明填没填」那十几条规则是相反的结论。
            fixed = (remark or "").replace(BAD_MARK, "").strip() or None
            c.execute("""UPDATE expense_bill
                            SET doc_date = %s::date, doc_date_source = %s,
                                remark = %s
                          WHERE instance_id = %s AND bill_no = %s""",
                      (d.isoformat(), SOURCE, fixed, IID, bn))
        c.execute("""SELECT count(*) FROM expense_bill WHERE instance_id = %s
                      AND doc_date IS NULL""", (IID,))
        if c.fetchone()[0]:
            raise SystemExit("中止：还有单据的 doc_date 是空的")
        # 闸：remark 里一个标记都不许剩，而且非空的张数要回到 19
        c.execute("""SELECT count(*) FROM expense_bill WHERE instance_id = %s
                      AND remark LIKE %s""", (IID, "%" + BAD_MARK + "%"))
        if c.fetchone()[0]:
            raise SystemExit("中止：remark 里还有误写的来源标记")
        c.execute("""SELECT count(*) FROM expense_bill WHERE instance_id = %s
                      AND remark IS NOT NULL AND remark <> ''""", (IID,))
        n_remark = c.fetchone()[0]
        if n_remark != 19:
            raise SystemExit(
                "中止：补充说明非空的张数是 %d，应该回到 19——"
                "这一列是业务的补充说明区，有十几条规则判它填没填" % n_remark)
        c.execute("""SELECT count(*) FROM expense_bill
                      WHERE instance_id <> %s AND doc_date IS NOT NULL""", (IID,))
        n_other = c.fetchone()[0]
        if n_other != 69:
            raise SystemExit("中止：别家的 doc_date 行数从 69 变成 %d" % n_other)
    print("\n**已写库**")
    print("  822gh doc_date 有值: %s / %s"
          % (db.scalar("""SELECT count(doc_date) FROM expense_bill
                           WHERE instance_id=%s""", (IID,)),
             db.scalar("SELECT count(*) FROM expense_bill WHERE instance_id=%s",
                       (IID,))))
    print("  来源标成「%s」的: %s" % (
        SOURCE,
        db.scalar("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                       AND doc_date_source=%s""", (IID, SOURCE))))
    print("  补充说明（remark）非空: %s / 29（回填前是 19，不该被动）"
          % db.scalar("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                           AND remark IS NOT NULL AND remark <> ''""", (IID,)))
    print("  别家没被动：fjhy doc_date 有值 %s 张"
          % db.scalar("""SELECT count(doc_date) FROM expense_bill
                          WHERE instance_id='fjhy'"""))


if __name__ == "__main__":
    main()
