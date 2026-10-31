# -*- coding: utf-8 -*-
r"""重新解析 822港航 发票的购销双方，只定点 UPDATE title / issuer 两列。

用法（**不要加 -I**）：

    cd 服务端
    python -m app.demo.fix_822gh_invoice_parties          # 干跑，逐张前后对比
    WRITE2=1 python -m app.demo.fix_822gh_invoice_parties # 真写库

【修的是什么】
  53 张发票里有 3 张的购销双方取反了：
      旧  title=莆田市涵江建成大酒店有限公司   issuer=售
      新  title=福建省港航建设发展有限公司     issuer=莆田市涵江建成大酒店有限公司
  版式原因是**值排在标签前面**（`<购买方> 名称： <销售方>` 一行里只有一个
  「名称：」），落到全文兜底分支就取反了；而 issuer 里那个「售」字是竖排的
  「购销／买售」列标签被按坐标并进行里的产物，不是名称。
  解析侧已修（load_822gh_attachments.parse_invoice 新增第④分支 + _name_like
  长度闸），这个程序把库里存量那 3 张改过来。

  **方向错了的后果不是少一个字段**：发票抬头显示成供应商名字，
  「抬头是否本公司」「供应商是否关联方」两类规则会全反，而且报告上看起来
  有理有据。

【为什么只 UPDATE 两列而不重跑 loader】
  loader 开头 DELETE attachment，而 attachment_vision 是 ON DELETE CASCADE。
  那件事真实发生过一次（198 行视觉台账），见 fix_822gh_trip_fields 的文件头。
  这里连 invoice 都不删——hotel_invoice / transport_voucher 挂在 invoice 上。

【闸】
  · 只允许改 title / issuer，改之前先核对解析出的其余字段与库里一致，
    不一致就中止——说明解析侧还动了别的东西，那不是本次要改的
  · 改完逐张复核：购买方必须是我方公司，否则中止回滚
"""
import os
import sys

_WRITE_WAS = os.environ.pop("WRITE", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app import db  # noqa: E402
from app.demo.load_822gh_attachments import parse_invoice, pdf_lines  # noqa: E402

IID = "822gh"
WRITE = os.environ.get("WRITE2") == "1"
# 我方公司名。**不从库里猜**：50 张的 title 都是它，但「多数就是对的」
# 不是判据；这个名字写在这里，改主体时改这一行。
OUR_COMPANY = "福建省港航建设发展有限公司"
# 这两列之外的字段必须和库里一致，否则说明解析侧动了别的东西。
UNCHANGED = ("inv_type", "issue_date", "total_amount", "tax_amount",
             "tax_rate", "detail_text")


def main():
    if _WRITE_WAS is not None:
        print("注意：环境里有 WRITE=%r，已摘掉（本程序用 WRITE2）" % _WRITE_WAS)
    cur = {r["inv_no"]: r for r in db.query(
        """SELECT inv_no, inv_type, issue_date, total_amount, tax_amount,
                  tax_rate, detail_text, title, issuer
             FROM invoice WHERE instance_id = %s""", (IID,))}
    rows = db.query("""SELECT file_name, stored_path FROM attachment
                        WHERE instance_id = %s AND file_name LIKE %s""",
                    (IID, "发票附件-%"))
    print("候选附件 %d 个；库里发票 %d 张" % (len(rows), len(cur)))

    todo, drift = [], []
    for r in rows:
        try:
            inv = parse_invoice(pdf_lines(open(r["stored_path"], "rb").read()))
        except Exception:                 # noqa: BLE001
            continue
        if not inv or inv.get("inv_no") not in cur:
            continue
        old = cur[inv["inv_no"]]
        for k in UNCHANGED:
            a, b = old[k], inv.get(k)
            if k in ("total_amount", "tax_amount", "tax_rate"):
                a = float(a) if a is not None else None
                b = float(b) if b is not None else None
            elif k == "issue_date":
                a = str(a) if a is not None else None
            if a != b:
                drift.append((inv["inv_no"], k, a, b))
        if (old["title"], old["issuer"]) != (inv["title"], inv["issuer"]):
            todo.append((inv["inv_no"], old["title"], old["issuer"],
                         inv["title"], inv["issuer"]))

    if drift:
        for d in drift[:10]:
            print("  **本次不该变的字段变了**: %s.%s  %r → %r" % d)
        raise SystemExit("中止：解析侧动了 title/issuer 以外的字段，先查清楚")

    print("要改 %d 张（其余 %d 张不动）" % (len(todo), len(cur) - len(todo)))
    for no, ot, oi, nt, ni in todo:
        print("  %s" % no)
        print("     旧 title=%-30s issuer=%s" % (ot, oi))
        print("     新 title=%-30s issuer=%s" % (nt, ni))

    if not WRITE:
        print("\n（干跑，没写库。设 WRITE2=1 才写）")
        return
    if not todo:
        print("\n没有要改的。")
        return

    with db.tx() as conn:
        c = conn.cursor()
        for no, _ot, _oi, nt, ni in todo:
            c.execute("""UPDATE invoice SET title = %s, issuer = %s
                          WHERE instance_id = %s AND inv_no = %s""",
                      (nt, ni, IID, no))
        c.execute("""SELECT count(*) FROM invoice WHERE instance_id = %s
                      AND title IS NOT NULL AND title <> %s""",
                  (IID, OUR_COMPANY))
        n_bad = c.fetchone()[0]
        if n_bad:
            raise SystemExit(
                "中止：改完还有 %d 张的购买方不是「%s」" % (n_bad, OUR_COMPANY))
        c.execute("""SELECT count(*) FROM invoice WHERE instance_id = %s""",
                  (IID,))
        assert c.fetchone()[0] == len(cur), "发票张数变了，本程序不该增删"
    print("\n**已写库** 改了 %d 张" % len(todo))
    print("  购买方是我方公司的: %s / %s"
          % (db.scalar("""SELECT count(*) FROM invoice WHERE instance_id=%s
                            AND title = %s""", (IID, OUR_COMPANY)),
             db.scalar("SELECT count(*) FROM invoice WHERE instance_id=%s",
                       (IID,))))
    print("  销售方取到的: %s"
          % db.scalar("""SELECT count(*) FROM invoice WHERE instance_id=%s
                           AND issuer IS NOT NULL AND issuer <> ''""", (IID,)))
    print("  别家没被动：fjhy 发票 %s"
          % db.scalar("SELECT count(*) FROM invoice WHERE instance_id='fjhy'"))


if __name__ == "__main__":
    main()
