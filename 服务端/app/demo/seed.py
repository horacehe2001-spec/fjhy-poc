# -*- coding: utf-8 -*-
"""把客户给的 40 张真实样本装进业务表。

这不是「演示数据」，是**真实单据**，所以 expense_bill.is_demo = FALSE。
合成的演示数据另有生成器（app/demo/generator.py），单据号带 FJHY-DEMO- 前缀、
is_demo = TRUE，一眼可辨、事后好清理。两者不能混，否则演示时说不清
「这个结论是跑真实单据得出的还是跑编的数据得出的」。

装载纪律：
  1. **引用完整性对不上的行进隔离区，不丢弃也不造父行。**
     实测：FJPG-BX2609041860 有 8 条费用明细但主表里没有这张单（G-30）。
  2. 枚举取值由数据库外键把关。本体里不存在的取值会被拒，
     装载直接失败而不是写进去——「多一个表单上不存在的取值」会让规则
     去判一个永远为假的条件。
  3. 装完自检：行数、关键列非空、与源文件对账。对不上就回滚。

用法（在 服务端/ 下）：
    python -I app/demo/seed.py            # 装载
    python -I app/demo/seed.py --status   # 只看状态
    python -I app/demo/seed.py --clear    # 清掉业务数据（规则资产不动）
"""
import argparse
import json
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import config, db  # noqa: E402

MATERIAL = os.path.join(config.PROJECT_ROOT, "材料", "单据明细表与审批意见")
SINGLE_XLSX = os.path.join(MATERIAL, "差旅报账单明细表.xlsx")
MULTI_XLSX = os.path.join(MATERIAL, "多人差旅报账单明细表.xlsx")

# **真实样本只属于福建海运集团，实例硬编码不做参数**（理由同 rules/loader.py）：
# 这个脚本读的是客户给的那两份 xlsx 导出（单人单/多人单），是海运集团的真实单据。
# 822港航 要有自己的业务数据，得有它自己的导出文件，那是另一件事。
INSTANCE = "fjhy"

ORG_ID = "ORG-CZFGS"
ORG_NAME = "福建省港口集团有限责任公司财智分公司"


def _rows(path, sheet):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    return [r for r in wb[sheet].iter_rows(values_only=True) if r and any(x is not None for x in r)]


def _hdr(rows):
    return [str(c) if c is not None else "" for c in rows[0]]


def _fill_down(rows, key_idx):
    """费用明细表的单据号只在每张单的首行出现，需要向下填充。

    **不填充的后果**：108 行明细里只有 33 行有单据号，其余 75 行会被当成
    没有归属而丢掉——而丢掉的恰恰是金额明细，所有按金额判的规则全部失效。
    """
    cur = None
    out = []
    for r in rows[1:]:
        v = r[key_idx]
        if v:
            cur = str(v)
        out.append((cur, r))
    return out


def clear(conn):
    cur = conn.cursor()
    # 顺序要紧：有外键的先删
    # 注意 attachment：清掉它之后，**从 xlsx 抽出来的 219 张附件登记会一起消失**，
    # 而 var/attachments/ 下的图片文件不会跟着删。那不是故障，但页面上会显示
    # 「40 张单全都没有附件」——和已被推翻的缺口 G-29 长得一模一样。
    # 重跑一次 app/perception/attachment_extract.py 即可（它是幂等的），
    # 而 /api/attachments/status 会把「磁盘 219 / 库里 0」这个差额直接报出来。
    #
    # **每一句都限定在本实例（0040 之后）。** 这个函数原来是 19 句无条件
    # `DELETE FROM <表>`，在只有一个实例的年代等价于"清掉我自己导入的那批"。
    # 审核领域有了第二个实例之后，那个写法会把 **822港航 的全部业务数据
    # 一次清空**——而装载脚本的输出里只会打印它自己导入了多少行，
    # 对面消失了多少行一个字都不会说。这是本文件里最危险的一个函数。
    #
    # 只有 expense_bill / org 自己带 instance_id（业务主键按用户决策维持全局
    # 唯一，所以子表不加这一列），子表一律经它们的主键回溯：
    # 这样"作用域"写在 SQL 里，而不是写在"反正库里只有我这一批数据"的假设里。
    bills = "SELECT bill_no FROM expense_bill WHERE instance_id = %s"
    orgs = "SELECT org_id FROM org WHERE instance_id = %s"
    plan = [
        ("review_finding", "WHERE run_id IN (SELECT run_id FROM review_run "
                           "WHERE bill_no IN (%s))" % bills),
        ("manual_review", "WHERE bill_no IN (%s)" % bills),
        ("review_run", "WHERE bill_no IN (%s)" % bills),
        ("expense_allocation", "WHERE bill_no IN (%s)" % bills),
        ("meeting_notice", "WHERE bill_no IN (%s)" % bills),
        ("authority_approval", "WHERE bill_no IN (%s)" % bills),
        ("attachment", "WHERE bill_no IN (%s)" % bills),
        ("approval_record", "WHERE bill_no IN (%s)" % bills),
        ("transport_voucher", "WHERE bill_no IN (%s)" % bills),
        ("hotel_invoice", "WHERE invoice_id IN (SELECT invoice_id FROM invoice "
                          "WHERE bill_no IN (%s))" % bills),
        ("invoice", "WHERE bill_no IN (%s)" % bills),
        ("trip_calendar", "WHERE app_no IN (SELECT app_no FROM trip_application "
                          "WHERE bill_no IN (%s))" % bills),
        ("trip_application", "WHERE bill_no IN (%s)" % bills),
        ("expense_detail", "WHERE bill_no IN (%s)" % bills),
        ("expense_bill", "WHERE instance_id = %s"),
        ("budget", "WHERE org_id IN (%s)" % orgs),
        ("employee", "WHERE org_id IN (%s)" % orgs),
        ("org", "WHERE instance_id = %s"),
    ]
    for t, where in plan:
        cur.execute("DELETE FROM %s %s" % (t, where), (INSTANCE,))
    # load_quarantine 没有任何能回溯到实例的列（它装的是外键对不上的孤行，
    # target/source 是自由文本）。**如实整表清**：这张表只在本脚本导入时写入，
    # 留着上一轮的孤行会让"这次导入干净不干净"判不出来。
    # 口径写在这里，不然下一个人会以为这是漏掉了一句。
    cur.execute("DELETE FROM load_quarantine")


def load(verbose=True):
    import openpyxl  # noqa: F401  提前失败好过装到一半失败
    stats = {}
    with db.tx() as conn:
        cur = conn.cursor()
        clear(conn)

        # ── 核算主体与出差人 ────────────────────────────────────────────────
        cur.execute("""INSERT INTO org (instance_id, org_id, name, city, is_internal)
                       VALUES (%s,%s,%s,NULL,TRUE)""", (INSTANCE, ORG_ID, ORG_NAME))
        # city 留空：财智分公司的办公地在材料里没有明确出现，不猜。
        # 住宿费标准按市定额，猜错一位就选错档。

        # ── 先建出差人，再插单据 ────────────────────────────────────────────
        # 顺序不能反：expense_bill.submitter_no / claimant_no 外键指向 employee，
        # 边读边插会在第一张单就撞外键。两趟扫：先收齐姓名，再插单据。
        srows = _rows(SINGLE_XLSX, "1差旅费报账单")
        sh = _hdr(srows)
        mrows = _rows(MULTI_XLSX, "2多人差旅报账单")
        mh = _hdr(mrows)

        names = set()
        for r in srows[1:]:
            for k in ("提单人", "报账人"):
                if k in sh and r[sh.index(k)]:
                    names.add(str(r[sh.index(k)]).strip())
        for r in mrows[1:]:
            if "提单人" in mh and r[mh.index("提单人")]:
                names.add(str(r[mh.index("提单人")]).strip())
        for n in sorted(names):
            # 工号样本里没给，用姓名派生占位工号。职级（C0804）与常驻地（C0805）
            # 留空：前者待业务侧明确，后者 40 张样本一列都没有（G-31，阻塞）。
            cur.execute("""INSERT INTO employee (emp_no, name, org_id)
                           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (_emp(n), n, ORG_ID))
        stats["employees"] = len(names)

        # ── 单人单 ──────────────────────────────────────────────────────────
        bills = {}
        for r in srows[1:]:
            g = lambda k: r[sh.index(k)] if k in sh else None            # noqa: E731
            bill_no = str(g("单据编号"))
            sub, cla = g("提单人"), g("报账人")
            bills[bill_no] = True
            cur.execute("""INSERT INTO expense_bill
                (instance_id, bill_no, doc_type, doc_date, submitter_no, claimant_no,
                 claim_dept, org_id, bill_status, bearing_dept, biz_type, amount,
                 pay_amount, currency, tax, summary, is_multi_person, is_demo)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE)""",
                (INSTANCE, bill_no, str(g("单据类型")), g("单据日期"),
                 _emp(str(sub).strip()) if sub else None,
                 _emp(str(cla).strip()) if cla else None,
                 g("报账部门"), ORG_ID, str(g("单据状态")) if g("单据状态") else None,
                 g("费用承担部门"), str(g("业务类型")), g("报账金额"), g("付款金额"),
                 g("币种"), g("税金"), g("摘要")))
        stats["single_bills"] = len(srows) - 1

        # ── 多人单 ──────────────────────────────────────────────────────────
        for r in mrows[1:]:
            g = lambda k: r[mh.index(k)] if k in mh else None            # noqa: E731
            bill_no = str(g("单据编号"))
            sub = g("提单人")
            bills[bill_no] = True
            # 多人导出缺 报账人 / 核算主体 / 单据状态 / 费用承担部门（G-32）。
            # **这四列写 NULL，不拿单人单的值去填**——
            # 「导出时没带上」和「业务上没有」是两件事，填了就分不清了。
            cur.execute("""INSERT INTO expense_bill
                (instance_id, bill_no, doc_type, doc_date, submitter_no, claimant_no,
                 claim_dept, org_id, bill_status, bearing_dept, biz_type, amount,
                 pay_amount, currency, tax, summary, is_multi_person, is_demo)
                VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,NULL,NULL,%s,%s,%s,%s,%s,%s,TRUE,FALSE)""",
                (INSTANCE, bill_no, str(g("单据类型")), g("单据日期"),
                 _emp(str(sub).strip()) if sub else None, g("报账部门"), ORG_ID,
                 str(g("业务类型")), g("报账金额"), g("付款金额"),
                 g("币种"), g("税金"), g("摘要")))
        stats["multi_bills"] = len(mrows) - 1

        # ── 费用明细 ────────────────────────────────────────────────────────
        n_detail, quarantined = 0, 0
        for path, sheet, has_dept in ((SINGLE_XLSX, "1费用明细", False),
                                      (MULTI_XLSX, "2多人差旅-费用明细", True)):
            rows = _rows(path, sheet)
            h = _hdr(rows)
            key = "单据号" if "单据号" in h else h[0]
            item_i = h.index("费用明细")
            amt_i = h.index("费用明细金额")
            dept_i = h.index("部门") if has_dept and "部门" in h else None
            line = 0
            for bill_no, r in _fill_down(rows, h.index(key)):
                if not r[item_i]:
                    continue
                line += 1
                if bill_no not in bills:
                    # 游离明细：不丢弃、不造父行，进隔离区
                    cur.execute("""INSERT INTO load_quarantine
                        (source, target, reason, gap_id, row_data)
                        VALUES (%s,%s,%s,%s,%s)""",
                        ("%s / %s" % (os.path.basename(path), sheet), "expense_detail",
                         "单据号 %s 在报账单主表里不存在（游离明细）" % bill_no, "G-30",
                         json.dumps({"bill_no": bill_no, "item": str(r[item_i]),
                                     "amount": float(r[amt_i]) if r[amt_i] is not None else None,
                                     "dept": str(r[dept_i]) if dept_i is not None and r[dept_i] else None},
                                    ensure_ascii=False)))
                    quarantined += 1
                    continue
                cur.execute("""INSERT INTO expense_detail
                    (bill_no, expense_item, amount, alloc_dept, line_no)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (bill_no, str(r[item_i]).strip(), r[amt_i],
                     str(r[dept_i]) if dept_i is not None and r[dept_i] else None, line))
                n_detail += 1
        stats["details"] = n_detail
        stats["quarantined"] = quarantined

        # ── 审批意见 PDF ────────────────────────────────────────────────────
        # 这一步不是可选的。补充说明（C0116）是 12 条规则的例外出口，
        # 不装它，那些规则会对所有有例外情形的正常单据报违规——
        # 第一次空跑就出现了 18 条这样的命中，全部是误报。
        from app.perception import approval_pdf as ap
        parsed = ap.parse_all(MATERIAL)
        n_appr = n_trav = n_rank = n_remark = 0
        for bn, rec in parsed.items():
            if bn not in bills:
                cur.execute("""INSERT INTO load_quarantine
                    (source, target, reason, gap_id, row_data)
                    VALUES (%s,%s,%s,NULL,%s)""",
                    (rec.get("source_pdf") or "审批意见PDF", "expense_bill",
                     "审批意见 PDF 里的单据 %s 在明细表里不存在" % bn,
                     json.dumps({"bill_no": bn}, ensure_ascii=False)))
                continue
            # 单据上的字段
            cur.execute("""UPDATE expense_bill SET
                    remark = %s, attach_pages = %s, voucher_no = %s,
                    travel_settlement = %s, writeoff_amount = %s, payable_amount = %s,
                    standard_basis_person = %s, applicant_rank = %s,
                    pay_method = %s, payee = %s, payee_account = %s,
                    payee_bank = %s, pay_purpose = %s
                 WHERE bill_no = %s""",
                (rec["remark"], rec["attach_pages"], rec["voucher_no"],
                 rec["travel_settlement"], rec["writeoff_amount"], rec["payable_amount"],
                 rec["standard_basis_person"], rec["applicant_rank"],
                 (rec["payments"][0]["pay_method"] if rec["payments"] else None),
                 (rec["payments"][0]["payee"] if rec["payments"] else None),
                 (rec["payments"][0]["payee_account"] if rec["payments"] else None),
                 (rec["payments"][0]["payee_bank"] if rec["payments"] else None),
                 (rec["payments"][0]["pay_purpose"] if rec["payments"] else None),
                 bn))
            if rec["remark"]:
                n_remark += 1
            if rec["applicant_rank"]:
                n_rank += 1
            # 申请人栏的职级回写到 employee。**单据上那一份也留着**
            # （expense_bill.applicant_rank）：人的职级会变，而单据的判定要用当时的职级。
            if rec["applicant_name"] and rec["applicant_rank"]:
                cur.execute("UPDATE employee SET rank_level = %s WHERE name = %s",
                            (rec["applicant_rank"], rec["applicant_name"]))
            # 出行人员：PDF 里带真实工号。先补建 employee 再挂关系。
            for k, t in enumerate(rec["travelers"]):
                cur.execute("""INSERT INTO employee (emp_no, name, org_id)
                               VALUES (%s,%s,%s) ON CONFLICT (emp_no) DO NOTHING""",
                            (t["emp_no"], t["name"], ORG_ID))
                cur.execute("""INSERT INTO bill_traveler (bill_no, emp_no, name, ord)
                               VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (bn, t["emp_no"], t["name"], k))
                n_trav += 1
            # 审批记录
            for a in rec["approvals"]:
                cur.execute("""INSERT INTO approval_record
                    (bill_no, seq, node, approver, opinion, approved_at)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (bn, a["seq"], a["node"], a["approver"], a["opinion"], a["approved_at"]))
                n_appr += 1
        stats["approvals"] = n_appr
        stats["travelers"] = n_trav
        stats["with_rank"] = n_rank
        stats["with_remark"] = n_remark

        # ── 自检 ────────────────────────────────────────────────────────────
        cur.execute("SELECT count(*) FROM expense_bill")
        n_bills = cur.fetchone()[0]
        assert n_bills == 40, "报账单装载 %d 张，应为 40 张" % n_bills
        cur.execute("SELECT count(*) FROM expense_detail")
        n_d = cur.fetchone()[0]
        assert n_d == 174, ("费用明细装载 %d 行。源表共 182 行，其中 8 行属游离单据"
                            "FJPG-BX2609041860，应进隔离区，故期望 174 行" % n_d)
        cur.execute("SELECT count(*) FROM load_quarantine")
        assert cur.fetchone()[0] == 8, "隔离区应有 8 行游离明细"
        cur.execute("SELECT count(*) FROM expense_bill WHERE biz_type IS NULL")
        assert cur.fetchone()[0] == 0, "有报账单的业务类型为空"
        cur.execute("""SELECT count(*) FROM expense_bill b
                       WHERE NOT EXISTS (SELECT 1 FROM expense_detail d WHERE d.bill_no=b.bill_no)""")
        stats["bills_without_detail"] = cur.fetchone()[0]

        # 审批记录对账：330 条是独立数 PDF 原文得出的真值。
        # 第一版解析器因为页归属方向写反，只得 255 条（漏 24%），
        # **而且不报错**。所以这个数字必须钉成断言。
        cur.execute("SELECT count(*) FROM approval_record")
        n_a = cur.fetchone()[0]
        assert n_a == 330, ("审批记录装载 %d 条，真值 330 条。"
                            "真值是独立数 PDF 原文里「整行等于节点名」的次数得出的，"
                            "不是解析器自己报的数。" % n_a)
        cur.execute("SELECT count(*) FROM expense_bill WHERE applicant_rank IS NOT NULL")
        assert cur.fetchone()[0] == 40, "应当 40 张单都有职级（申请人栏「姓名/职级/部门」）"
        cur.execute("SELECT count(*) FROM expense_bill WHERE remark IS NOT NULL")
        n_r = cur.fetchone()[0]
        assert n_r == 17, "有补充说明的单据应为 17 张，实际 %d 张" % n_r
        cur.execute("SELECT count(*) FROM bill_traveler")
        stats["travelers_db"] = cur.fetchone()[0]

    if verbose:
        print("样本装载完成（**真实单据，is_demo=FALSE**）")
        for k, label in (("single_bills", "单人报账单"), ("multi_bills", "多人报账单"),
                         ("employees", "出差人"), ("details", "费用明细"),
                         ("approvals", "审批记录"), ("with_remark", "有补充说明的单据"),
                         ("with_rank", "有职级的单据"), ("travelers_db", "出行人员关系"),
                         ("quarantined", "进隔离区的游离明细"),
                         ("bills_without_detail", "没有明细的单据")):
            print("  %-18s %s" % (label, stats.get(k)))
        if stats.get("quarantined"):
            print("  注意：隔离区有 %d 行。这 %d 笔费用当前不被任何规则审核，"
                  "覆盖面声明里要算进去（G-30）。" % (stats["quarantined"], stats["quarantined"]))
    return stats


def _emp(name):
    """由姓名派生占位工号。样本里没有工号列，但 employee 需要主键。

    **不能把姓名直接拼进工号。** 第一版是 `"SMP-" + name`，结果
    `SMP-岑柏` 这种值在脱敏时按「保留后 2 位」处理，保留的恰好是那两个字的姓名
    ——脱敏自检把它抓了出来（16/40 张单的快照被判定泄露）。
    改成姓名的哈希前 8 位：稳定、可复现、不含姓名。
    `SMP-` 前缀保留，让人一眼看出这不是真工号——真工号出现在报告里
    会被当成系统里的真实编号去查，查不到就成了疑点。
    """
    import hashlib
    return "SMP-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8].upper()


def status():
    print("业务数据：")
    for q, label in (
        ("SELECT count(*) FROM expense_bill", "报账单"),
        ("SELECT count(*) FROM expense_bill WHERE is_demo", "  其中演示数据"),
        ("SELECT count(*) FROM expense_detail", "费用明细"),
        ("SELECT count(*) FROM employee", "出差人"),
        ("SELECT count(*) FROM approval_record", "审批记录"),
        ("SELECT count(*) FROM attachment", "附件"),
        ("SELECT count(*) FROM load_quarantine WHERE NOT resolved", "隔离区未处理"),
    ):
        print("  %-16s %s" % (label, db.scalar(q)))
    print("按业务类型：")
    for r in db.query("""SELECT biz_type, doc_type, count(*) n FROM expense_bill
                         GROUP BY biz_type, doc_type ORDER BY n DESC"""):
        print("  %-34s %-16s %d" % (r["biz_type"], r["doc_type"], r["n"]))
    print("费用小类分布：")
    for r in db.query("""SELECT expense_item, count(*) n, sum(amount) s
                         FROM expense_detail GROUP BY expense_item ORDER BY n DESC"""):
        print("  %-22s %3d 行  合计 %s" % (r["expense_item"], r["n"], r["s"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    if args.status:
        return status()
    if args.clear:
        with db.tx() as conn:
            clear(conn)
        print("业务数据已清空（规则资产与本体未动）")
        return 0
    load()
    return 0


if __name__ == "__main__":
    sys.exit(main())
