# -*- coding: utf-8 -*-
"""按实例测「业务数据到底有什么」——规则能不能判，先看这个。

── 为什么要有这个模块 ──────────────────────────────────────────────────
2026-10-31 实测：822港航 的 88 条规则**一条都编译不了**，而原因不是实现
没写，是数据不在——29 张单里 18 个字段全空，发票/附件/审批记录/出差申请
四张表 0 行。而这件事之前是一条条凭印象判断的：一条规则自报
「auto_judgeable=yes」就被当成能编，直到拿数据一核才发现它要的
「审批完成时间」在这个实例里根本没有记录。

**自报的可行性不能当证据。** 要判一条规则能不能跑，得拿它需要的数据项
去库里实测。这个模块就干这一件事，并且做成模块而不是一次性脚本：
客户补完数据要能立刻重测，看哪些规则解锁了。

── 可得性分三档，不是两档 ────────────────────────────────────────────
    无      表 0 行，或该列在本实例的行里**全为空**
    部分    该列有值但不是全部（比如 19/29）
    有      该列在本实例每一行都有值
**「部分」不能并进「有」**：拿一个 19/29 的字段去判规则，剩下 10 张单会
被静默跳过——那是漏判，而页面上看不出来，只会显示「这条规则没发现问题」。
宁可标「部分」让人看见，不要给一个干净的绿灯。

── 「归属」怎么算 ───────────────────────────────────────────────────
大部分业务表直接带 instance_id（迁移 0040/0046）。6 张不带的经外键推：
hotel_invoice→invoice、attachment_vision/extract→attachment、
bill_traveler/expense_allocation→expense_bill、budget→org。
**不要给这些表补 instance_id 列**：它们永远是「按某个根的主键取一组」，
加列就等于再造一处要应用层同步、DB 不强制的隐患（doc_lib_file 那次的教训）。
"""
from . import db

# 表 → 怎么把它过滤到某个实例。
#   None            表自己带 instance_id
#   (SQL 片段, )    经外键推，%s 绑实例
_SCOPE = {
    "expense_bill": None,
    "expense_detail": None,
    "invoice": None,
    "transport_voucher": None,
    "approval_record": None,
    "attachment": None,
    "trip_application": None,
    "trip_calendar": None,
    "standard_limit": None,
    "rank_tier_map": None,
    "org_special_rule": None,
    "org": None,
    "employee": None,
    "authority_approval": None,
    "meeting_notice": None,
    "hotel_invoice":
        "invoice_id IN (SELECT invoice_id FROM invoice WHERE instance_id = %s)",
    "bill_traveler":
        "bill_no IN (SELECT bill_no FROM expense_bill WHERE instance_id = %s)",
    "expense_allocation":
        "bill_no IN (SELECT bill_no FROM expense_bill WHERE instance_id = %s)",
    "attachment_vision":
        "attachment_id IN (SELECT id FROM attachment WHERE instance_id = %s)",
    "attachment_extract":
        "attachment_id IN (SELECT id FROM attachment WHERE instance_id = %s)",
    "budget":
        "org_id IN (SELECT org_id FROM org WHERE instance_id = %s)",
    # 招待明细（迁移 0053）。它自己带 instance_id（由触发器从报账单继承），
    # 所以走直表过滤，不用经 bill_no join。
    "entertain_detail": None,
}

# 这几列是**簿记字段，不是业务数据**，不许拿来判规则：
#   created_at / loaded_at  是写库那一刻，不是业务发生时间。
#     822港航 的 29 张单 created_at 全有值，而 doc_date 一个都没有——
#     拿 created_at 当「单据日期」去判时效，判出来的是「我什么时候导的数」。
#   id / instance_id / 各种 *_enum 是主键与内部编码。
_BOOKKEEPING = ("id", "instance_id", "created_at", "updated_at", "loaded_at",
                "is_demo")


def _where(table, instance_id):
    s = _SCOPE[table]
    return ("instance_id = %s", (instance_id,)) if s is None else (s, (instance_id,))


def table_rows(instance_id, tables=None):
    """每张业务表在本实例有多少行。"""
    out = {}
    for t in (tables or _SCOPE):
        w, p = _where(t, instance_id)
        try:
            out[t] = db.scalar("SELECT count(*) FROM %s WHERE %s" % (t, w), p) or 0
        except Exception as e:                                  # noqa: BLE001
            # **不要静默吞掉**：查不到就是查不到，标出来。吞掉会让一张
            # 其实没查成的表显示成 0 行，而 0 行和「没查」要给人不同的动作。
            out[t] = "查不到(%s)" % type(e).__name__
    return out


def columns(instance_id, table):
    """这张表每一列在本实例的填充情况。返回 {列: (有值行数, 总行数, 档位)}。"""
    w, p = _where(table, instance_id)
    total = db.scalar("SELECT count(*) FROM %s WHERE %s" % (table, w), p) or 0
    cols = [r["column_name"] for r in db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position""", (table,))]
    out = {}
    for c in cols:
        if c in _BOOKKEEPING or c.endswith("_enum"):
            continue
        n = 0 if not total else (db.scalar(
            'SELECT count("%s") FROM %s WHERE %s' % (c, table, w), p) or 0)
        out[c] = (n, total, grade(n, total))
    return out


def grade(n, total):
    """三档。**部分不能并进有**，理由见模块头。"""
    if total == 0 or n == 0:
        return "无"
    return "有" if n == total else "部分"


# 「表里有行」不等于「有我要的那一类行」。
# 实测：822港航 的 standard_limit 有 317 行，看起来「有标准」，
# 但 317 行**全是住宿费限额**；交通工具等级标准、伙食补助费标准、
# 市内交通费标准各 0 行。按表行数判，这三类规则会被当成「数据齐备」，
# 而它们一条都判不了。所以这几列的**取值集合**要单独测出来。
_KIND_COLS = (
    ("standard_limit", "std_type"),
    ("expense_detail", "expense_item"),
    ("expense_bill", "doc_type"),
    ("expense_bill", "biz_type"),
)


def kinds(instance_id):
    """这几列在本实例实际出现了哪些取值。返回 {表.列: {取值集合}}。"""
    out = {}
    for t, c in _KIND_COLS:
        w, p = _where(t, instance_id)
        try:
            out["%s.%s" % (t, c)] = {
                r["v"] for r in db.query(
                    'SELECT DISTINCT "%s" AS v FROM %s WHERE %s' % (c, t, w), p)
                if r["v"] is not None}
        except Exception:                                      # noqa: BLE001
            out["%s.%s" % (t, c)] = set()
    return out


def snapshot(instance_id):
    """一次把全部业务表与列的可得性测出来。给规则可行性判定和客户数据需求用。"""
    rows = table_rows(instance_id)
    cols = {}
    for t in _SCOPE:
        if isinstance(rows.get(t), int) and rows[t] >= 0:
            cols[t] = columns(instance_id, t)
    empty_tables = sorted(t for t, n in rows.items() if n == 0)
    empty_cols = sorted("%s.%s" % (t, c)
                        for t, d in cols.items() for c, v in d.items()
                        if v[2] == "无" and rows.get(t))
    partial_cols = sorted("%s.%s(%d/%d)" % (t, c, v[0], v[1])
                          for t, d in cols.items() for c, v in d.items()
                          if v[2] == "部分")
    return {"instance_id": instance_id, "tables": rows, "columns": cols,
            "empty_tables": empty_tables, "empty_columns": empty_cols,
            "partial_columns": partial_cols, "kinds": kinds(instance_id)}
