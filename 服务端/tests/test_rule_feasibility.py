# -*- coding: utf-8 -*-
"""规则的数据可行性：拿库里的数据实测，不看规则自报什么。

这组用例守的是 2026-10-31 连着踩的三个坑：

1. **自报的可行性不是证据。** `TR-通用-03 审批流程时效性` 自报
   auto_judgeable=yes，于是被当成「最便宜的第一条、先编译它」。
   拿数据一核：这个实例 approval_record 0 行、doc_date 0/29，
   两个输入一个都没有。模型判的是规则文字，不是这个实例的数据。

2. **写错列。** 第一版把结论写进了 `rule.feasibility`，而那一列是客户
   规则清单里的「是否已有」（见 app/rule_detail.py 的 FIELD_MAP，取值
   是/否/已有/无/部分/部分已有），记的是**客户系统里有没有这条规则**。
   结果 822港航 的 feasibility='判不了' 而福建海运集团同一列是 '已有'——
   同一列两套语义，下游没法一起读。POC 侧的结论属于 demo_blocker/demo_scope。

3. **「部分」不许并进「有」。** 一个 19/29 的字段拿去判规则，剩下 10 张单
   会被静默跳过，页面上只会显示「这条规则没发现问题」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import data_availability as da, db, rule_feasibility as rf  # noqa: E402

IID = "822gh"

# 客户规则清单「是否已有」那一列的取值。**本模块不许往这一列写东西**，
# 所以这里列出来是为了反向断言：库里出现的值必须都是客户词表里的。
_CUSTOMER_FEASIBILITY = {"是", "否", "已有", "无", "部分", "部分已有",
                         "可以实现", "部分可实现", "无法实现", "未评估"}


# ── 可得性分档 ──────────────────────────────────────────────────────────
def test_部分填充不许算成有():
    assert da.grade(29, 29) == "有"
    assert da.grade(19, 29) == "部分"
    assert da.grade(0, 29) == "无"
    assert da.grade(0, 0) == "无"


def test_簿记字段不许当业务数据():
    """created_at 是写库那一刻，不是业务时间。822港航 的 29 张单
    created_at 全有值而 doc_date 一个都没有——拿前者判时效，
    判出来的是「我什么时候导的数」。"""
    cols = da.columns(IID, "expense_bill")
    for c in ("created_at", "id", "instance_id", "is_demo"):
        assert c not in cols, "%s 是簿记字段，不该出现在业务数据可得性里" % c
    assert "doc_date" in cols and "booking_period" in cols


def test_可得性快照认得出空表和空列():
    snap = da.snapshot(IID)
    assert snap["tables"]["expense_bill"] == 29
    assert snap["tables"]["attachment"] == 0
    assert "attachment" in snap["empty_tables"]
    assert "expense_bill.doc_date" in snap["empty_columns"]
    # 部分填充要单独列出来，不能混进「有」也不能混进「无」
    assert any(p.startswith("expense_bill.remark") for p in snap["partial_columns"])


def test_不带实例的表计数会把另一家算进来():
    """这个用例是说明为什么 _SCOPE 必须存在：approval_record 全库 388 条
    一条都不属于 822港航。不按实例过滤，这个实例会显示「有审批记录」。"""
    all_rows = db.scalar("SELECT count(*) FROM approval_record")
    mine = da.table_rows(IID, ["approval_record"])["approval_record"]
    assert mine == 0
    assert all_rows > 0, "全库一条审批记录都没有，这个用例验不出东西"


def test_经外键推归属的表也要算对():
    """6 张表不带 instance_id，经外键推。推错会让它们看起来有数据。"""
    for t in ("hotel_invoice", "bill_traveler", "expense_allocation",
              "attachment_vision", "attachment_extract", "budget"):
        assert da.table_rows(IID, [t])[t] == 0, "%s 在 822港航 应该是 0 行" % t


# ── 映射 ────────────────────────────────────────────────────────────────
def test_规则记下的数据项必须全部映射得上():
    """映射不上的会进「口径未定」。**现在应该一条都没有**——
    这样新加规则带来一个认不出的数据项时，这条用例会红，
    而不是悄悄给那条规则一个「口径未定」然后没人看。
    """
    rows = db.query("""SELECT rule_id, note FROM rule
                        WHERE instance_id = %s AND version = 1""", (IID,))
    needs = rf.need_items(rows)
    assert needs, "一条规则都没登记数据项，这个用例没验到东西"
    unmapped = sorted({n for v in needs.values() for n in v
                       if n not in rf.NEED_MAP})
    assert not unmapped, "这些数据项还没定它指库里的什么：%s" % unmapped


def test_映射不上的一律进口径未定而不是当成可得():
    snap = da.snapshot(IID)
    v, bl, nf, un, pa, ok = rf.judge(["一个不存在的数据项"], snap)
    assert v == "口径未定"
    assert un == ["一个不存在的数据项"]
    assert not ok, "认不出的数据项绝不能算成已具备"


def test_缺字段和判不了要分开():
    """两者给人的动作不同：缺字段是找我们改表，判不了是找客户要数。
    合成一句「数据不足」，客户补完数据回来发现还是判不了。"""
    snap = da.snapshot(IID)
    v, bl, nf, un, pa, ok = rf.judge(["单据提交时间"], snap)
    assert v == "缺字段" and nf == ["单据提交时间"]
    v2, bl2, nf2, un2, pa2, ok2 = rf.judge(["附件清单"], snap)
    assert v2 == "判不了" and bl2 and not nf2


def test_自报可判不等于真能判():
    """TR-通用-03 自报 auto_judgeable=yes，实测两个输入都没有。"""
    r = db.query_one("""SELECT note FROM rule WHERE instance_id = %s
                         AND rule_id = 'TR-通用-03' AND version = 1""", (IID,))
    assert r and "可自动判定：yes" in r["note"], "这条规则的自报值变了，用例要跟着改"
    rep = rf.report(IID, "ai_draft_from_rejection")
    it = [x for x in rep["rules"] if x["rule_id"] == "TR-通用-03"][0]
    assert it["verdict"] != "可判", "自报 yes 的规则被实测判成可判了，核对一下数据是不是补上了"


# ── 不许污染客户那一列 ──────────────────────────────────────────────────
def test_本模块不许往feasibility写东西():
    """feasibility 是客户规则清单的「是否已有」。两个实例同一列必须同一个意思。"""
    import inspect
    src = inspect.getsource(rf.apply)
    assert "feasibility" not in src.split('"""')[2], \
        "apply() 的代码里出现了 feasibility——那一列是客户评估，不是 POC 结论"


def test_两个实例的feasibility取值都在客户词表里():
    bad = [dict(r) for r in db.query(
        """SELECT instance_id, feasibility, count(*) n FROM rule
            WHERE version = 1 AND feasibility IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 2""")
        if r["feasibility"] not in _CUSTOMER_FEASIBILITY]
    assert not bad, "这些 feasibility 取值不属于客户词表，像是被别的语义占用了：%s" % bad


def test_没有客户评估表的实例这一列就该空着():
    n = db.scalar("""SELECT count(*) FROM rule
                      WHERE instance_id = %s AND version = 1
                        AND feasibility IS NOT NULL""", (IID,))
    assert n == 0, ("822港航 没有客户规则清单评估表，feasibility 应全空，"
                    "现在有 %d 条有值——很可能又被 POC 侧的结论占用了" % n)


# ── 写库 ────────────────────────────────────────────────────────────────
def test_结论写进demo_blocker和demo_scope且可重入():
    a = rf.apply(IID, "ai_draft_from_rejection")
    before = db.query("""SELECT rule_id, demo_blocker, demo_scope FROM rule
                          WHERE instance_id = %s AND version = 1
                            AND created_by = 'ai_draft_from_rejection'
                          ORDER BY rule_id""", (IID,))
    assert a["written"] == len(before) > 0
    assert all(r["demo_blocker"] and r["demo_scope"] for r in before)
    rf.apply(IID, "ai_draft_from_rejection")
    after = db.query("""SELECT rule_id, demo_blocker, demo_scope FROM rule
                         WHERE instance_id = %s AND version = 1
                           AND created_by = 'ai_draft_from_rejection'
                         ORDER BY rule_id""", (IID,))
    assert [dict(r) for r in before] == [dict(r) for r in after], "重跑结果不一样"


def test_未登记数据项的规则不给结论():
    """不猜——给它一个看起来评估过的结论，比留空更坏。

    2026-10-31 之后 88 条都登记了，所以这里改成「如果还有未登记的，
    它就不许带结论」。一条都没有时直接通过，**不再 assert 必须有**：
    那种断言会在把活干完之后反过来变红。
    """
    rep = rf.report(IID)
    unlogged = [x for x in rep["rules"] if x["verdict"] == "未登记数据项"]
    if not unlogged:
        return
    ids = tuple(x["rule_id"] for x in unlogged)
    rows = db.query("""SELECT rule_id, demo_blocker FROM rule
                        WHERE instance_id = %s AND version = 1
                          AND rule_id IN %s""", (IID, ids))
    assert all(not (r["demo_blocker"] or "").startswith("数据可行性")
               for r in rows), "未登记数据项的规则被写了数据可行性结论"


def test_没有数据缺口不等于能判所以必须有人复核():
    """**这一档是这组用例里最要紧的一条。**

    need_fields 是模型列的，它**会列漏**。2026-10-31 实测：6 条落到
    「未发现数据缺口」的规则，逐条复核后一条都站不住——
    TR-会议-04/11 漏列了会议费综合定额标准（822港航 的 standard_limit
    只有住宿费 317 条）、TR-差旅-03 漏列交通工具信息、TR-接待-19 漏列
    附件内容、TR-差旅-11 根本是要人判断实质、TR-日常-02 数据项真齐备
    但 136 条明细里没有一条车辆使用费。
    所以落到这一档的规则必须有一条人工复核结论；没有就说明没人看过，
    **这条用例会红，而不是让它顶着一张绿灯进编译**。
    """
    rep = rf.report(IID)
    pending = [x["rule_id"] for x in rep["rules"]
               if x["verdict"] == "未发现数据缺口·待复核"]
    unreviewed = sorted(set(pending) - set(rf.MANUAL_REVIEW))
    assert not unreviewed, (
        "这些规则没发现数据缺口但也没人复核过，不许当成可编译：%s" % unreviewed)


def test_结论里不许出现可判这种承诺口径():
    """「可判」会让人直接去编译。这一档测的只是「它自己列出的数据项都在」。"""
    snap = da.snapshot(IID)
    v = rf.judge(["报销明细"], snap)[0]
    assert v == "未发现数据缺口·待复核", v
    assert "可判" not in v or "待复核" in v


def test_人工复核的范围结论盖过机械推断():
    """TR-差旅-03/11 机械上是「部分字段不全」，人看过之后是 0/29。
    两个数并排写在页面上互相打架，而对的是人那一个。"""
    for rid in ("TR-差旅-03", "TR-差旅-11"):
        r = db.query_one("""SELECT demo_scope FROM rule WHERE instance_id = %s
                             AND rule_id = %s AND version = 1""", (IID, rid))
        assert r["demo_scope"] == rf.MANUAL_REVIEW[rid]["scope"], (
            rid, r["demo_scope"])


def test_判不了的规则演示范围必须写零而不是留空():
    """留空会被读成「没限制」。0/29 张要明说。"""
    rows = db.query("""SELECT rule_id, demo_scope FROM rule
                        WHERE instance_id = %s AND version = 1
                          AND demo_blocker LIKE %s""", (IID, "数据可行性（判不了%"))
    assert rows
    for r in rows:
        assert r["demo_scope"].startswith("0/"), (r["rule_id"], r["demo_scope"])
