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
  外加同目录的 员工信息导出(2026-10-27).xlsx （49 人，真工号）
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

5. **工号用员工导出里的真工号**（2026-10-31 补）。原来 29 张单上的 14 个
   报账人全是姓名哈希派生的 `SMP-xxxxxxxx` 占位号——那是在「导出表里没有
   工号列」的前提下的权宜做法，而这个前提是错的：工号在**员工信息导出**
   那份文件里，一直都在手上。顺带把员工从 14 人补到 49 人全量，
   职级（主岗）也一起入库。
   占位号规则保留，给**导出里没有的人**兜底：实测报账人「雒朔」
   在 49 人全量导出里查不到（离职或外部人员，待客户确认），
   这一条仍然走 SMP- 占位号，不编一个工号给她。

6. **收款账户名称不入 payee_account**。那一列在 fjhy 里装的是银行卡号
   （实测 `622900000000000000` 这种），822港航 导出里给的是**账户名**
   （人名或公司名）。同一列塞两种语义，就是这个项目里刚踩过的
   `rule.feasibility` 那个坑的翻版。822港航 的导出里**没有银行账号**
   （在附件的银行回单里），所以这一列留空。
   「收款客商 ≠ 收款账户名称」这件事本身是审核抓手，实测 28 行里有 1 行
   不一致，如实记进 remark，不丢掉。
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
# 员工全量导出。**不在压缩包里**，是单独一份 xlsx，和报账单导出同级目录。
EMP_XLSX = os.path.join(ROOT, "员工信息导出(2026-10-27).xlsx")

# 招待明细区只在招待费那份导出里有（差旅那份没有这张工作表）。
# 工作表名两份都不冲突，所以直接按名字取，取不到就是 0 行。
ENT_SHEET = "招待明细区"

# (压缩包, 主表工作表名, 分摊工作表名, 单据类型, 费用归属工作表名)
# 两份导出的工作表名不一样（一份叫「归属区」，一份叫「费用归属区」），
# 所以归属表名跟着源文件走，不要图省事写死一个。
# 「支付区」两份同名，在下面直接写死。
SRC = [
    ("822港航差旅费用报销单+附件.zip", "主表区", "分摊区", "差旅费报账单", "归属区"),
    ("822港航业务接待费单据+附件.zip", "头表区", "费用分摊区", "业务招待费报账单",
     "费用归属区"),
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


def s_(v):
    """单元格 → 文本，空串归 None。"""
    t = str(v or "").strip()
    return t or None


def num(v):
    """单元格 → 小数，读不出就 None，**不补 0**。
    0 和「没填」在金额上不是一回事：补成 0 会让「酒水金额超标」这类规则
    对着一个假的 0 判通过。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def intg(v):
    n = num(v)
    return int(n) if n is not None else None


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


def emp_master():
    """读 员工信息导出(2026-10-27).xlsx 的「主表区」→ 49 人。

    只取四列：员工工号 / 员工姓名 / 部门 / 主岗。

    **电话号码一列故意不取。** employee 表没有这个字段，而它是真实手机号；
    为了存它去加一列，等于多开一个对外出口要守——这个项目里脱敏的教训
    就是出口越多越守不住。需要联系人时查原始导出文件。

    **差旅级别一列也不取。** 49 人全是「二级」，而住宿标准表有三档，
    对不上；这是要客户先解释的口径问题，映射关系没定之前入库就是在猜。
    职级存的是「主岗」（董事长/副总经理/部门负责人/员工/其他员工），
    对应本体 EN12，见 本体_822gh_v0.1.yaml 里 EN12 的注释。

    「兼岗信息2」那张表（34 条、16 个人有兼岗）暂不入库：employee 是一人
    一行的结构，装不下一人多岗，要装得先加表。先记在这里，别当没看见。
    """
    if not os.path.exists(EMP_XLSX):
        p("!! 找不到 %s，员工退回到「只装单据上出现过的报账人」" % EMP_XLSX)
        return []
    import openpyxl
    wb = openpyxl.load_workbook(EMP_XLSX, read_only=True, data_only=True)
    try:
        ws = wb["主表区"]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    hdr = ["" if c is None else str(c).strip() for c in rows[0]]
    need = ("员工工号", "员工姓名", "部门", "主岗")
    assert all(k in hdr for k in need), "员工导出表头变了：%s" % hdr
    recs = []
    for r in rows[1:]:
        if not r or r[0] is None:
            continue
        d = {hdr[i]: ("" if c is None else str(c).strip())
             for i, c in enumerate(r) if i < len(hdr)}
        if not d.get("员工工号") or not d.get("员工姓名"):
            continue
        recs.append({"emp_no": d["员工工号"], "name": d["员工姓名"],
                     "dept": d["部门"] or None, "rank_level": d["主岗"] or None})
    nos = [e["emp_no"] for e in recs]
    assert len(set(nos)) == len(nos), "员工导出里有重复工号"
    return recs


def pay_fields(rs):
    """支付区的若干行 → 落到 expense_bill 单列字段上的取值。

    一张单可能有多行（实测 FJPG-BX2607280255 有 2 行：两个收款人各一笔）。
    **某一列只在所有行取值一致时才落字段**；不一致的留空，并把原始取值
    如实记一句进 remark。挑其中一行当代表就是在编数据，而且编得看起来
    很合理——这种错下游查不出来。

    付款金额是例外：多行求和就是「该单付款合计」，语义本来就是加总，
    不是从几个候选里挑一个。
    """
    if not rs:
        return {}, []

    def one(col):
        vals = {str(r.get(col) or "").strip() for r in rs}
        vals.discard("")
        return vals.pop() if len(vals) == 1 else None

    notes = []
    payee, acct = one("收款客商"), one("收款账户名称")
    if payee is None or acct is None:
        notes.append("支付区有 %d 行、收款人不止一个：%s" % (
            len(rs), "；".join(
                "%s→%s %.2f" % (str(r.get("收款客商") or "").strip(),
                                str(r.get("收款账户名称") or "").strip(),
                                float(r.get("付款金额") or 0)) for r in rs)))
    elif payee != acct:
        notes.append("支付区的收款客商「%s」与收款账户名称「%s」不一致"
                     % (payee, acct))
    tot = sum(float(r.get("付款金额") or 0) for r in rs)
    return {
        "payee": payee,
        "pay_method": one("支付方式"),
        "pay_purpose": one("付款用途"),
        "pay_amount": tot or None,
    }, notes


def ensure_xls():
    os.makedirs(XLS_DIR, exist_ok=True)
    found = {}
    for zf in [s[0] for s in SRC]:
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
emps = emp_master()
bills, details, places, reviews, ents = [], [], [], [], []

for zf, main_sheet, alloc_sheet, doc_type, own_sheet in SRC:
    path = xls.get(zf)
    if not path:
        p("!! %s 里没找到明细表，跳过" % zf)
        continue
    wb = xlrd.open_workbook(path)
    _, mains = sheet_rows(wb, main_sheet)
    _, allocs = sheet_rows(wb, alloc_sheet)
    _, pls = sheet_rows(wb, "出差地点")
    _, pays = sheet_rows(wb, "支付区")
    _, owns = sheet_rows(wb, own_sheet)
    _, entr = sheet_rows(wb, ENT_SHEET)
    p("=== %s ===" % os.path.basename(path))
    p("  主表 %d / 分摊 %d / 出差地点 %d / 支付区 %d / %s %d / %s %d"
      % (len(mains), len(allocs), len(pls), len(pays), own_sheet, len(owns),
         ENT_SHEET, len(entr)))

    pay_of = {}
    for r in pays:
        bn = str(r.get("单据号") or "").strip()
        if bn:
            pay_of.setdefault(bn, []).append(r)
    own_of = {}
    for r in owns:
        bn = str(r.get("单据号") or "").strip()
        if bn:
            own_of.setdefault(bn, []).append(r)

    for m in mains:
        bn = str(m.get("单据号") or "").strip()
        if not bn:
            continue
        rej = str(m.get("驳回原因") or "").strip()
        pf, notes = pay_fields(pay_of.get(bn, []))
        ow = own_of.get(bn, [])
        # 归属区实测每张单 1 行（29/29）。真出现多行就不落字段、记一句，
        # 和支付区同一条纪律：宁可空着，不许挑一行。
        bearing = (str(ow[0].get("费用归属部门") or "").strip() or None
                   if len(ow) == 1 else None)
        if len(ow) > 1:
            notes.append("%s 有 %d 行，费用归属部门不唯一：%s" % (
                own_sheet, len(ow),
                "、".join(str(r.get("费用归属部门") or "").strip() for r in ow)))
        b = {
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
            "bearing_dept": bearing,
            "notes": notes,
            # 支付区缺这张单时这四个保持 None（pay_fields 返回空字典）
            "payee": None, "pay_method": None,
            "pay_purpose": None, "pay_amount": None,
        }
        b.update(pf)
        bills.append(b)
        if rej:
            reviews.append({"bill_no": bn, "reason": rej})
    # **分摊区每一行在源文件里都重复了一遍。** 实测 2026-11-01：
    # 差旅那份 118 行去重后 61 行，57 行各出现两次。
    # 判据不是「看着像重复」，是**去重后明细合计正好等于报销金额**：
    #   29 张单里，去重后合计 == 报销金额 的有 **29 张**；
    #   不去重的话只有 1 张对得上。
    # 不去重的代价是实打实的：市内交通费 160 被算成 320，
    # 而 2 天的包干上限是 160——规则会报「超标一倍」，**而单据完全合规**。
    # 第一次跑引擎时就是这样报出 4 条假违规的。
    #
    # 去重按**整行相同**，不按业务键：同一张单上确实可能有两笔金额相同的
    # 同类费用（比如两段同价的车票），那种情况下其余列（责任部门、预算科目）
    # 多半不同；整行都一样才判定是导出重复。
    _seen_alloc = set()
    for a in allocs:
        bn = str(a.get("单据号") or "").strip()
        if not bn:
            continue
        _key = tuple(sorted((k, str(v)) for k, v in a.items()))
        if _key in _seen_alloc:
            continue
        _seen_alloc.add(_key)
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
    # 招待明细区 → entertain_detail（迁移 0053 新建的表）。
    # 三个码列（招待类型/招待方式/陪同级别）**原样存，不翻译**：
    # 导出给的是 OTHER_ENTERTAIN / EAT_WORK / LEVEL_05 这种系统码，
    # 客户没给码表。把 LEVEL_05 猜成某个职级档，整类陪同级别判定全错，
    # 而且错得看起来很合理。码表列进客户清单。
    for x in entr:
        bn = str(x.get("单据号") or "").strip()
        if not bn:
            continue
        ents.append({
            "bill_no": bn,
            # 行号跨源文件连续数（len(ents)+1），不用 enumerate 的局部序号：
            # 现在只有招待费那份导出有这张表，真多出第二份时局部序号会从 1
            # 重来，两份的行号就撞了。和 expense_detail 的编号方式一致。
            "line_no": len(ents) + 1,
            "amount": num(x.get("招待金额")),
            "entertain_type": s_(x.get("招待类型")),
            "entertain_way": s_(x.get("招待方式")),
            "companion_level": s_(x.get("陪同级别与招待对象")),
            "place": s_(x.get("招待地点")),
            "happen_date": excel_date(x.get("发生日期")),
            "guest_count": intg(x.get("招待人数")),
            "companion_count": intg(x.get("陪同人数")),
            "liquor_amount": num(x.get("领用酒水金额")),
            "guest_list": s_(x.get("招待对象名单")),
        })
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
p("=== 明细合计 vs 报销金额（这一条是闸，不是统计）===")
_sum = {}
for d in details:
    _sum[d["bill_no"]] = _sum.get(d["bill_no"], 0) + (d["amount"] or 0)
_bad = [(b["bill_no"], b["amount"], _sum.get(b["bill_no"], 0)) for b in bills
        if abs(_sum.get(b["bill_no"], 0) - b["amount"]) >= 0.01]
p("  明细合计等于报销金额的: %d/%d 张" % (len(bills) - len(_bad), len(bills)))
for b, a, s in _bad[:5]:
    p("    · %s 单据 %.2f / 明细 %.2f" % (b, a, s))
p("  （分摊区源文件里每行重复一遍，已按整行去重。不去重的话只有 1/29 对得上，")
p("   而且规则会把合规的市内交通费报成「超标一倍」——第一次跑引擎就是这样误报了 4 条。）")
assert not _bad, "明细合计与报销金额对不上，先查去重口径：%s" % _bad[:3]
p("")
p("=== 支付区 / 归属区（2026-10-31 补读）===")
p("  收款人落到字段的 %d/%d 张；支付方式 %d 张；付款用途 %d 张；付款金额 %d 张"
  % (sum(1 for b in bills if b["payee"]), len(bills),
     sum(1 for b in bills if b["pay_method"]),
     sum(1 for b in bills if b["pay_purpose"]),
     sum(1 for b in bills if b["pay_amount"])))
p("  费用归属部门落到字段的 %d/%d 张；取值 %s"
  % (sum(1 for b in bills if b["bearing_dept"]), len(bills),
     sorted({b["bearing_dept"] for b in bills if b["bearing_dept"]})))
for b in bills:
    for n in b["notes"]:
        p("  · %s  %s" % (b["bill_no"], n))
p("  收款账户名称（银行账号那一列）**故意没入库**：见本文件口径 6。")
p("  成本中心/部门属性/项目名称/项目类型 读到了但库里没有对应字段，未入库。")
p("")
p("=== 招待明细区（2026-10-31 补读，表是迁移 0053 新建的）===")
p("  %d 行 / %d 张单" % (len(ents), len({e["bill_no"] for e in ents})))
for col, lab in (("entertain_type", "招待类型"), ("entertain_way", "招待方式"),
                 ("companion_level", "陪同级别")):
    vals = sorted({e[col] for e in ents if e[col]})
    p("  %s（**系统码，客户没给码表，原样存**）: %s" % (lab, vals))
p("  招待地点: %s" % sorted({e["place"] for e in ents if e["place"]}))
p("  发生日期还原出来的: %d/%d 行；区间 %s ~ %s"
  % (sum(1 for e in ents if e["happen_date"]), len(ents),
     min((e["happen_date"] for e in ents if e["happen_date"]), default="—"),
     max((e["happen_date"] for e in ents if e["happen_date"]), default="—")))
p("  招待人数合计 %s / 陪同人数合计 %s / 酒水金额合计 %s"
  % (sum(e["guest_count"] or 0 for e in ents),
     sum(e["companion_count"] or 0 for e in ents),
     sum(e["liquor_amount"] or 0 for e in ents)))
_nolist = [e["bill_no"] for e in ents if (e["guest_list"] or "") == "详情见附件"]
p("  招待对象名单填「详情见附件」的 %d 行：%s" % (len(_nolist), sorted(set(_nolist))))
p("  名单这一列**整列不出明文**（外部人员姓名与职务黏连，闭集合切不出来），")
p("  对外只给人数，见 app/mask.py 的 roster 策略。本报告也不打印原值。")
p("")
p("=== 员工（2026-10-31 补全）===")
p("  员工导出 %d 人；部门 %d 个；职级（主岗）%s"
  % (len(emps), len({e["dept"] for e in emps if e["dept"]}),
     {v: sum(1 for e in emps if e["rank_level"] == v)
      for v in sorted({e["rank_level"] for e in emps if e["rank_level"]})}))
_real = {e["name"] for e in emps}
_subs = {b["submitter"] for b in bills if b["submitter"]}
p("  单据上的报账人 %d 人，其中导出里查不到的 %d 人：%s（仍用 SMP- 占位号）"
  % (len(_subs), len(_subs - _real), sorted(_subs - _real) or "无"))
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
# employee.rank_enum 是生成列（恒为 'EN12'），外键指向本实例的 EN12 枚举值。
# 缺一个值，整条 INSERT 被拒；所以装人之前先把这个闸过一遍。
have12 = {r["value"] for r in db.query(
    "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN12'",
    (IID,))}
used12 = {e["rank_level"] for e in emps if e["rank_level"]}
p("  EN12 rank_level  用到 %d 种，缺 %s" % (len(used12), sorted(used12 - have12) or "无"))

ORG_ID = "ORG-822GH"


def check_emp_no_clash(nos):
    """真工号入库前查跨实例撞号。

    `employee` 的主键是 **(emp_no) 单列，不含 instance_id**，而两家公司
    都在福建港口集团下、用同一套 HR 系统，工号段是重叠的
    （fjhy 库里已有 1000505 / 2010680 / 2002080 这种）。
    撞上了会被 `ON CONFLICT (emp_no) DO NOTHING` 悄悄吞掉：822港航 的人
    不会入库，而 `expense_bill.submitter_no` 会指到**另一家公司的员工行**
    上去——外键照样满足，页面照样显示一个人名，错得毫无声响。

    2026-10-31 实测：49 个工号与库里 0 条相撞。**但这是当下的事实，
    不是结构上的保证**，所以这个检查留着，撞上就停，不要悄悄跳过。
    真要从根上解决，得把主键改成 (instance_id, emp_no)，那是一次迁移。
    """
    if not nos:
        return
    hit = db.query("""SELECT emp_no, name, instance_id FROM employee
                      WHERE instance_id <> %s AND emp_no = ANY(%s)""", (IID, nos))
    if hit:
        raise SystemExit(
            "跨实例工号相撞 %d 条，已停止（employee 主键不含 instance_id）：\n%s"
            % (len(hit), "\n".join("  %s 已属于 %s" % (h["emp_no"], h["instance_id"])
                                   for h in hit)))


def emp_no(name):
    """由姓名派生占位工号，给**员工导出里查不到的人**兜底（实测 1 人：雒朔）。

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


REAL_NO = {e["name"]: e["emp_no"] for e in emps}


def emp_no_of(name):
    """报账人姓名 → 工号。导出里有就用真工号，没有才派生占位号。"""
    return REAL_NO.get(name) or emp_no(name)


# ── 入库 ───────────────────────────────────────────────────────────────────
if os.environ.get("WRITE") == "1":
    check_emp_no_clash([e["emp_no"] for e in emps])
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
        # 可重入：先清掉本实例这批。**只清 822gh**，别家一行不动。
        # 顺序要紧：expense_bill.submitter_no 有外键指向 employee，
        # 先删员工会被外键拦住，所以单据先走。
        #
        # ── 删之前先看一眼要删掉什么 ──────────────────────────────────
        # **2026-11-01 的事故**：重跑一次这个程序，附件链被整个级联删掉：
        #   attachment 147 / invoice 53 / trip_application 15 /
        #   trip_calendar 87 / attachment_vision 198 → 全部归零。
        # 原因是这几张表的 bill_no 都是 ON DELETE CASCADE 指向 expense_bill，
        # 而它们是**另一个装载程序**（load_822gh_attachments.py）写的。
        # 前四张能重建（源文件还在，解析是确定性的）；
        # **attachment_vision 重建不了**——那是读图调用的台账，
        # 「哪些客户凭证离开过本系统」的唯一证据，重建只能重新调模型花钱。
        #
        # 所以这里先数一遍、报出来，并且**默认拒绝**。
        # 确实要重跑就设 CASCADE_OK=1，但那时你已经看过要损失什么了。
        _casc = {}
        for _t, _sql in (
                ("attachment", "SELECT count(*) FROM attachment WHERE instance_id=%s"),
                ("invoice", "SELECT count(*) FROM invoice WHERE instance_id=%s"),
                ("trip_application",
                 "SELECT count(*) FROM trip_application WHERE instance_id=%s"),
                ("trip_calendar",
                 "SELECT count(*) FROM trip_calendar WHERE instance_id=%s"),
                ("attachment_vision",
                 """SELECT count(*) FROM attachment_vision v
                      JOIN attachment a ON a.id = v.attachment_id
                     WHERE a.instance_id = %s""")):
            cur.execute(_sql, (IID,))
            _casc[_t] = cur.fetchone()[0]
        if any(_casc.values()):
            p("")
            p("!! 重跑会**级联删掉**下面这些（它们是别的装载程序写的）：")
            for _t, _n in _casc.items():
                p("     %-20s %d 行%s" % (_t, _n,
                   "  ← 重建只能重新调模型" if _t == "attachment_vision" else ""))
            if os.environ.get("CASCADE_OK") != "1":
                raise SystemExit(
                    "拒绝执行：重跑会级联删掉 %s。\n"
                    "  要么先确认这些能重建（attachment/invoice/trip_* 可以：\n"
                    "    python -m app.demo.load_822gh_attachments），\n"
                    "  要么设 CASCADE_OK=1 明确接受损失。\n"
                    "  **attachment_vision 重建不了**，它是读图调用的台账。"
                    % "、".join("%s %d 行" % (k, v) for k, v in _casc.items() if v))
        cur.execute("DELETE FROM manual_review WHERE bill_no IN "
                    "(SELECT bill_no FROM expense_bill WHERE instance_id=%s)", (IID,))
        cur.execute("DELETE FROM expense_bill WHERE instance_id=%s", (IID,))
        cur.execute("DELETE FROM employee WHERE instance_id=%s", (IID,))
        # 员工：员工信息导出的 49 人全量，真工号 + 部门 + 职级（主岗）。
        # 原来只装「单据上出现过的 14 个报账人」，职级一律留空——
        # 那不是数据没给，是我方没读那份文件。
        for e in emps:
            cur.execute("""INSERT INTO employee(instance_id, emp_no, name, dept,
                               rank_level, org_id)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (IID, e["emp_no"], e["name"], e["dept"],
                         e["rank_level"], ORG_ID))
        # 导出里查不到、但单据上报过账的人：补一条占位记录，职级留空。
        # 不给她编职级，也不把她从员工表里漏掉（漏了单据外键就挂不上）。
        for nm, dept in {b["submitter"]: b["claim_dept"] for b in bills
                         if b["submitter"]}.items():
            if nm not in REAL_NO:
                cur.execute("""INSERT INTO employee(instance_id, emp_no, name, dept,
                                   org_id) VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (emp_no) DO NOTHING""",
                            (IID, emp_no(nm), nm, dept, ORG_ID))
        for b in bills:
            # remark 收的是「读到了、但库里没有语义对得上的字段」的东西，
            # 每条都注明出处。**不塞进一个语义不对的字段**。
            rk = []
            if place_of.get(b["bill_no"]):
                rk.append("出差地点（导出表「出差地点」工作表）：%s"
                          % place_of[b["bill_no"]])
            rk.extend(b["notes"])
            cur.execute("""
                INSERT INTO expense_bill(instance_id, bill_no, doc_type, biz_type,
                    bill_status, claim_dept, submitter_no, amount, currency, summary,
                    booking_period, remark, org_id, bearing_dept, payee, pay_method,
                    pay_purpose, pay_amount, is_demo, is_multi_person)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        FALSE,FALSE)""",
                (IID, b["bill_no"], b["doc_type"], b["biz_type"], b["bill_status"],
                 b["claim_dept"],
                 emp_no_of(b["submitter"]) if b["submitter"] else None,
                 b["amount"], b["currency"],
                 b["summary"], str(b["period"] or "") or None,
                 "；".join(rk) or None,
                 ORG_ID, b["bearing_dept"], b["payee"], b["pay_method"],
                 b["pay_purpose"], b["pay_amount"]))
        # 明细的 instance_id 由迁移 0046 的触发器从单据继承，这里不传
        for i, d in enumerate(details, 1):
            cur.execute("""INSERT INTO expense_detail(bill_no, expense_item, amount,
                               alloc_dept, line_no) VALUES (%s,%s,%s,%s,%s)""",
                        (d["bill_no"], d["expense_item"], d["amount"], d["dept"], i))
        # 招待明细。instance_id 由 0053 的触发器从报账单继承，这里不传。
        # 不用先 DELETE：表上 bill_no 是 ON DELETE CASCADE，
        # 上面删 expense_bill 时这批已经跟着走了。
        for e in ents:
            cur.execute("""INSERT INTO entertain_detail(bill_no, line_no, amount,
                               entertain_type, entertain_way, companion_level,
                               place, happen_date, guest_count, companion_count,
                               liquor_amount, guest_list)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (e["bill_no"], e["line_no"], e["amount"],
                         e["entertain_type"], e["entertain_way"],
                         e["companion_level"], e["place"], e["happen_date"],
                         e["guest_count"], e["companion_count"],
                         e["liquor_amount"], e["guest_list"]))
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
                  ON b.bill_no=m.bill_no WHERE b.instance_id=%s""", "人工驳回"),
            ("SELECT count(*) FROM employee WHERE instance_id=%s", "员工"),
            ("SELECT count(rank_level) FROM employee WHERE instance_id=%s",
             "员工·有职级"),
            ("""SELECT count(*) FROM employee WHERE instance_id=%s
                  AND left(emp_no,4)='SMP-'""", "员工·占位工号"),
            ("SELECT count(payee) FROM expense_bill WHERE instance_id=%s", "单据·收款人"),
            ("""SELECT count(bearing_dept) FROM expense_bill WHERE instance_id=%s""",
             "单据·费用归属部门"),
            ("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                  AND left(submitter_no,4)='SMP-'""", "单据·报账人仍是占位号"),
            ("SELECT count(*) FROM entertain_detail WHERE instance_id=%s", "招待明细"),
            ("""SELECT count(happen_date) FROM entertain_detail
                  WHERE instance_id=%s""", "招待明细·有发生日期")):
        p("  %s: %s" % (lab, db.scalar(q, (IID,))))
    p("  明细 instance_id（应由触发器自动填）: %s"
      % {r["instance_id"]: r["n"] for r in db.query(
          "SELECT instance_id, count(*) n FROM expense_detail GROUP BY 1")})
    # 补全 49 人之后，脱敏闭集合必须把这 49 个姓名全收进去。
    # 这一句不是装饰：补数据顺带堵脱敏的洞，是这次要的结果之一，
    # 没当场核过就不算做完。
    from app import mask                                             # noqa: PLC0415
    mask.refresh_names()
    have = set(mask.name_set())
    miss = sorted({e["name"] for e in emps} - have)
    p("  脱敏闭集合 %d 个；49 名员工里不在集合内 %d 个%s"
      % (len(have), len(miss), ("：%s" % miss) if miss else "（0，已全覆盖）"))
else:
    p("")
    p("（干跑，没写库。设 WRITE=1 才写）")

os.makedirs(WORK, exist_ok=True)
os.makedirs(WORK, exist_ok=True)
io.open(os.path.join(WORK, "import822.txt"), "w", encoding="utf-8").write("\n".join(out))
print("ok: 单据 %d / 明细 %d / 驳回 %d / 招待明细 %d / 员工 %d"
      % (len(bills), len(details), len(reviews), len(ents), len(emps)))
