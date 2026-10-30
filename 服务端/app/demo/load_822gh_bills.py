# -*- coding: utf-8 -*-
r"""822港航 的 29 张真实报账单 → 库（expense_bill / expense_detail / manual_review）。

用法（**不要加 -I**：隔离模式会把工作目录从 sys.path 摘掉，
找不到 app 包。项目里别处文档写的 `python -I -m ...` 同样跑不起来）：

    cd 服务端
    python -m app.demo.load_822gh_bills          # 干跑，只解析不写库
    WRITE=1 python -m app.demo.load_822gh_bills  # 真写库（可重入）

源：材料\822港航、单据、数据\*.zip 里的两份 .xls 导出
    · 822港航公司差旅报账单-含驳回意见(2).xls  （20 张）
    · 822港航公司招待费报账单-含驳回意见.xls   （9 张）
日常费用那个包里**只有附件 zip、没有明细表**，所以那 53 张进不来，不编。

几条口径：

1. **「驳回原因」是人工复核的真实结论，入 manual_review。**
   29 张全部非空。这是本实例目前唯一的人工判定来源。
   decision 一律 'return'（驳回），理由原样抄进 return_reason。

2. **29 张全是驳回，没有一张通过。**
   所以这批数据**算不出准确率**——没有负样本，任何比率都没有分母意义。
   而且人工驳回的维度（大量是「入账期间填错」这类录入规范问题）
   和那 74 条规则覆盖的维度本来就不是一套。
   能做的是「AI 报出的问题」与「人工驳回的问题」**分开列、看重合**。

3. **日期用导出表里的 Excel 序列号还原**，还原不出就留空，不猜。

4. 单据状态一律「已驳回」：导出表里没有状态列，这是从驳回原因非空推出来的，
   本体注释里写明了是推出来的，不是读出来的。
"""
import datetime
import io
import os
import sys
import zipfile

import xlrd

if __name__ == "__main__" and __package__ is None:      # 允许直接 python 路径执行
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))

from app import config, db  # noqa: E402

IID = "822gh"
ROOT = os.path.join(config.PROJECT_ROOT, "材料", "822港航、单据、数据")
# 从压缩包里解出来的 .xls 落在 var/ 下，**不写回客户的材料目录**——
# 那是文件库的「引用根」，0015 的只读纪律管着它，一个字节都不许写。
WORK = os.path.join(config.SERVER_DIR, "var", "822gh_import")
XLS_DIR = os.path.join(WORK, "xls")

# (压缩包, 主表工作表名, 分摊工作表名, 单据类型)
SRC = [
    ("822港航差旅费用报销单+附件.zip", "主表区", "分摊区", "差旅费报账单"),
    ("822港航业务接待费单据+附件.zip", "头表区", "费用分摊区", "业务招待费报账单"),
]
out = []


def p(s):
    out.append(str(s))


def excel_date(v):
    """Excel 序列号 → date。还原不出就 None，**不猜**。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n < 20000 or n > 80000:          # 明显不是日期序列号
        return None
    return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(n)))


def sheet_rows(wb, name):
    if name not in wb.sheet_names():
        return [], []
    sh = wb.sheet_by_name(name)
    if not sh.nrows:
        return [], []
    hdr = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    rows = []
    for r in range(1, sh.nrows):
        rows.append({hdr[c]: sh.cell_value(r, c) for c in range(sh.ncols)})
    return hdr, rows


def ensure_xls():
    os.makedirs(XLS_DIR, exist_ok=True)
    found = {}
    for zf, _, _, _ in SRC:
        z = zipfile.ZipFile(os.path.join(ROOT, zf))
        for n in z.namelist():
            if n.lower().endswith(".xls"):
                try:
                    nn = n.encode("cp437").decode("gbk")
                except Exception:                              # noqa: BLE001
                    nn = n
                dest = os.path.join(XLS_DIR, os.path.basename(nn))
                if not os.path.exists(dest):
                    io.open(dest, "wb").write(z.read(n))
                found[zf] = dest
    return found


xls = ensure_xls()
bills, details, places, reviews = [], [], [], []

for zf, main_sheet, alloc_sheet, doc_type in SRC:
    path = xls.get(zf)
    if not path:
        p("!! %s 里没找到明细表，跳过" % zf)
        continue
    wb = xlrd.open_workbook(path)
    _, mains = sheet_rows(wb, main_sheet)
    _, allocs = sheet_rows(wb, alloc_sheet)
    _, pls = sheet_rows(wb, "出差地点")
    p("=== %s ===" % os.path.basename(path))
    p("  主表 %d 行 / 分摊 %d 行 / 出差地点 %d 行" % (len(mains), len(allocs), len(pls)))

    for m in mains:
        bn = str(m.get("单据号") or "").strip()
        if not bn:
            continue
        rej = str(m.get("驳回原因") or "").strip()
        bills.append({
            "bill_no": bn,
            "doc_type": doc_type,
            "biz_type": str(m.get("业务大类") or "").strip() or None,
            "bill_status": "已驳回" if rej else None,
            "claim_dept": str(m.get("经办部门") or "").strip() or None,
            "submitter": str(m.get("姓名") or "").strip() or None,
            "amount": float(m.get("报销金额") or 0),
            "currency": str(m.get("报账币种") or "").strip() or None,
            "summary": str(m.get("事由") or "").strip() or None,
            "period": excel_date(m.get("入账期间")),
        })
        if rej:
            reviews.append({"bill_no": bn, "reason": rej})
    for a in allocs:
        bn = str(a.get("单据号") or "").strip()
        if not bn:
            continue
        details.append({
            "bill_no": bn,
            "expense_item": str(a.get("业务小类") or "").strip() or None,
            "amount": float(a.get("金额") or 0),
            "tax": float(a.get("税额") or 0),
            "dept": str(a.get("责任部门") or "").strip() or None,
            "budget": str(a.get("预算科目") or "").strip() or None,
        })
    for x in pls:
        bn = str(x.get("单据号") or "").strip()
        if bn:
            places.append({"bill_no": bn, "place": str(x.get("出差地点") or "").strip()})
    p("")

p("=== 解析结果 ===")
p("  单据 %d 张 / 明细 %d 行 / 出差地点 %d 条 / 人工驳回 %d 条"
  % (len(bills), len(details), len(places), len(reviews)))
p("")
p("  单据类型: %s" % {b["doc_type"]: sum(1 for x in bills if x["doc_type"] == b["doc_type"])
                     for b in bills})
amt = sum(b["amount"] for b in bills)
p("  金额合计 %.2f 元，均单 %.2f 元" % (amt, amt / max(1, len(bills))))
nod = [b for b in bills if not b["period"]]
p("  入账期间还原不出的: %d 张" % len(nod))
p("")
p("=== 前 6 张 ===")
for b in bills[:6]:
    p("  %-20s %-12s %-28s %8.2f  %s" % (b["bill_no"], b["doc_type"][:12],
                                         (b["biz_type"] or "")[:28], b["amount"],
                                         b["period"] or "期间?"))
p("")
p("=== 明细按费用小类 ===")
agg = {}
for d in details:
    agg[d["expense_item"]] = agg.get(d["expense_item"], 0) + 1
for k in sorted(agg, key=lambda x: -agg[x]):
    p("  %-16s %s 行" % (k, agg[k]))

# ── 入库前的闸：枚举值必须都在本实例本体里 ────────────────────────────────
p("")
p("=== 枚举对照（不在本体里的会被外键拒）===")
for eid, field in (("EN01", "doc_type"), ("EN02", "bill_status"), ("EN03", "biz_type")):
    have = {r["value"] for r in db.query(
        "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id=%s",
        (IID, eid))}
    used = {b[field] for b in bills if b[field]}
    miss = used - have
    p("  %s %-12s 用到 %d 种，缺 %s" % (eid, field, len(used), sorted(miss) or "无"))
have4 = {r["value"] for r in db.query(
    "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN04'", (IID,))}
used4 = {d["expense_item"] for d in details if d["expense_item"]}
p("  EN04 expense_item 用到 %d 种，缺 %s" % (len(used4), sorted(used4 - have4) or "无"))

ORG_ID = "ORG-822GH"


def emp_no(name):
    """由姓名派生占位工号。导出表里**没有工号列**，而 employee 要主键。

    规则照抄 demo/seed.py 的 _emp()，连同它那条用代价换来的纪律：
    **不能把姓名直接拼进工号**。第一版是 "SMP-" + 姓名，结果 `SMP-岑柏`
    这种值在脱敏时按「保留后 2 位」处理，保留下来的恰好就是那两个字的姓名，
    脱敏自检把 16/40 张单判成了泄露。所以取姓名哈希前 8 位：
    稳定、可复现、不含姓名。
    `SMP-` 前缀保留——让人一眼看出这不是真工号；真工号出现在报告里
    会被当成系统里的真实编号去查，查不到就成了疑点。
    """
    import hashlib
    return "SMP-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8].upper()


# ── 入库 ───────────────────────────────────────────────────────────────────
if os.environ.get("WRITE") == "1":
    place_of = {x["bill_no"]: x["place"] for x in places}
    with db.tx() as conn:
        cur = conn.cursor()
        # 组织：**只建一个，就是这个实例本身那家公司**。
        # 导出表里的「经办部门」是部门不是法人主体，不能当 org。
        # 公司名取自本体 meta.entity，也是 12 份制度文件的抬头，不是编的。
        cur.execute("""INSERT INTO org(instance_id, org_id, name, is_internal)
                       VALUES (%s,%s,%s,TRUE)
                       ON CONFLICT (org_id) DO NOTHING""",
                    (IID, ORG_ID, "福建省港航建设发展有限公司"))
        # 员工：姓名来自导出表，**部门也一起记**（fjhy 那批 dept 是空的，
        # 这里有就存，别浪费一个现成的字段）。职级留空——导出表没给，不猜。
        seen_emp = {}
        for b in bills:
            if b["submitter"] and b["submitter"] not in seen_emp:
                seen_emp[b["submitter"]] = b["claim_dept"]
        for nm, dept in seen_emp.items():
            cur.execute("""INSERT INTO employee(instance_id, emp_no, name, dept, org_id)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (emp_no) DO NOTHING""",
                        (IID, emp_no(nm), nm, dept, ORG_ID))
        # 可重入：先清掉本实例这批。**只清 822gh**，别家一行不动。
        cur.execute("DELETE FROM manual_review WHERE bill_no IN "
                    "(SELECT bill_no FROM expense_bill WHERE instance_id=%s)", (IID,))
        cur.execute("DELETE FROM expense_bill WHERE instance_id=%s", (IID,))
        for b in bills:
            cur.execute("""
                INSERT INTO expense_bill(instance_id, bill_no, doc_type, biz_type,
                    bill_status, claim_dept, submitter_no, amount, currency, summary,
                    booking_period, remark, org_id, is_demo, is_multi_person)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE)""",
                (IID, b["bill_no"], b["doc_type"], b["biz_type"], b["bill_status"],
                 b["claim_dept"], emp_no(b["submitter"]) if b["submitter"] else None,
                 b["amount"], b["currency"],
                 b["summary"], str(b["period"] or "") or None,
                 # 出差地点没有能落的字段（trip_application 要审批单号，这批没有），
                 # 如实放进 remark 并注明来源，**不塞进一个语义不对的字段**。
                 ("出差地点（导出表「出差地点」工作表）：%s" % place_of[b["bill_no"]])
                 if place_of.get(b["bill_no"]) else None,
                 ORG_ID))
        # 明细的 instance_id 由迁移 0046 的触发器从单据继承，这里不传
        for i, d in enumerate(details, 1):
            cur.execute("""INSERT INTO expense_detail(bill_no, expense_item, amount,
                               alloc_dept, line_no) VALUES (%s,%s,%s,%s,%s)""",
                        (d["bill_no"], d["expense_item"], d["amount"], d["dept"], i))
        # 人工驳回意见 → manual_review。**这是人的结论，必须留痕。**
        for r in reviews:
            cur.execute("""INSERT INTO manual_review(bill_no, reviewer, decision,
                               return_reason) VALUES (%s,%s,'return',%s)""",
                        (r["bill_no"], "822港航共享审核岗（导出表「驳回原因」）",
                         r["reason"]))
    p("")
    p("**已入库**")
    for q, lab in (
            ("SELECT count(*) FROM expense_bill WHERE instance_id=%s", "单据"),
            ("""SELECT count(*) FROM expense_detail d JOIN expense_bill b
                  ON b.bill_no=d.bill_no WHERE b.instance_id=%s""", "明细"),
            ("""SELECT count(*) FROM manual_review m JOIN expense_bill b
                  ON b.bill_no=m.bill_no WHERE b.instance_id=%s""", "人工驳回")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  明细 instance_id（应由触发器自动填）: %s"
      % {r["instance_id"]: r["n"] for r in db.query(
          "SELECT instance_id, count(*) n FROM expense_detail GROUP BY 1")})
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "import822.txt"), "w", encoding="utf-8").write("\n".join(out))
print("ok: 单据 %d / 明细 %d / 驳回 %d" % (len(bills), len(details), len(reviews)))
