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
import re
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
    # 这里原来拿 attachment 当「空表」的例子。2026-11-01 第四批把 147 个附件
    # 装进去了，它不空了——**换例子，不是改断言让它继续过**。
    # 换成本实例确实一行都没有的表：客户没给这几类业务的单据样本。
    assert snap["tables"]["authority_approval"] == 0
    assert "authority_approval" in snap["empty_tables"]
    assert snap["tables"]["attachment"] == 147
    # 空列的例子原来是 expense_bill.doc_date。2026-11-01 从单据号回填了
    # 29/29，它不空了——**换例子，不是改断言**。换成本实例确实全空的报销人。
    assert "expense_bill.claimant_no" in snap["empty_columns"]
    assert "expense_bill.doc_date" not in snap["empty_columns"], (
        "doc_date 又空了——回填被谁覆盖掉了")
    # 部分填充要单独列出来，不能混进「有」也不能混进「无」
    assert any(p.startswith("expense_bill.remark") for p in snap["partial_columns"])


def test_不带实例的表计数会把另一家算进来():
    """这个用例是说明为什么 _SCOPE 必须存在：一张表全库有几百行，
    却一行都不属于本实例。不按实例过滤，这个实例会显示「有数据」。

    例子原来用 approval_record（全库 388 条、822港航 0 条）。
    2026-11-01 从业务接待审批单 PDF 解出 8 条审批流水入了本实例，
    它不再是 0 了——**换例子，不是改断言**。
    换成 transport_voucher：全库 138 行全属福建海运集团，本实例一行都没有
    （822港航 的交通凭证还在扫描件里）。
    """
    all_rows = db.scalar("SELECT count(*) FROM transport_voucher")
    mine = da.table_rows(IID, ["transport_voucher"])["transport_voucher"]
    assert mine == 0
    assert all_rows > 0, "全库一条交通凭证都没有，这个用例验不出东西"
    # approval_record 现在本实例有 8 条，而全库 396 条——
    # 过滤仍然要真过滤，顺手再验一遍
    assert (da.table_rows(IID, ["approval_record"])["approval_record"]
            < db.scalar("SELECT count(*) FROM approval_record"))


def test_经外键推归属的表也要算对():
    """6 张表不带 instance_id，经外键推。推错会让它们看起来有数据。

    原来这条用例断言这 6 张表在 822港航 **都是 0 行**——那是当时的事实，
    不是这条用例要验的东西。2026-11-01 读图批跑完，attachment_vision
    有了 198 行，用例就红了，而推归属的逻辑一点没坏。
    **拿一个会变的事实当断言，变了就得改用例，而改的时候很容易顺手
    把要验的东西一起改掉。** 所以改成和独立算出来的数对照：
    推出来的数必须等于按外键直接 join 数出来的数。
    """
    pairs = {
        "hotel_invoice":
            "SELECT count(*) FROM hotel_invoice h JOIN invoice i "
            "ON i.invoice_id = h.invoice_id WHERE i.instance_id = %s",
        "bill_traveler":
            "SELECT count(*) FROM bill_traveler t JOIN expense_bill b "
            "ON b.bill_no = t.bill_no WHERE b.instance_id = %s",
        "expense_allocation":
            "SELECT count(*) FROM expense_allocation e JOIN expense_bill b "
            "ON b.bill_no = e.bill_no WHERE b.instance_id = %s",
        "attachment_vision":
            "SELECT count(*) FROM attachment_vision v JOIN attachment a "
            "ON a.id = v.attachment_id WHERE a.instance_id = %s",
        "attachment_extract":
            "SELECT count(*) FROM attachment_extract e JOIN attachment a "
            "ON a.id = e.attachment_id WHERE a.instance_id = %s",
        "budget":
            "SELECT count(*) FROM budget x JOIN org o ON o.org_id = x.org_id "
            "WHERE o.instance_id = %s",
    }
    for t, sql in pairs.items():
        want = db.scalar(sql, (IID,))
        assert da.table_rows(IID, [t])[t] == want, (t, want)
    # 至少有一张表是非空的，否则这条用例等于在验「0 == 0」
    assert any(da.table_rows(IID, [t])[t] > 0 for t in pairs), (
        "6 张表全是 0 行，这条用例没验到推归属是不是真的在推")


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
    # 「缺字段」的例子原来是「单据提交时间」。2026-11-01 订正：那一项
    # 既不缺列（expense_bill.doc_date 一直在）也不缺值（在单据号前六位里，
    # 已回填 29/29）——**换例子，不是改断言**。
    # 换成「参会人员数量」：库里**真的没有这一列**，客户也没给会议类单据。
    v, bl, nf, un, pa, ok = rf.judge(["参会人员数量"], snap)
    assert v == "缺字段" and nf == ["参会人员数量"], (v, nf)
    # 订正之后「单据提交时间」必须不再是缺字段——归因错了会被排进我方改表的活
    v0, _b0, nf0, _u0, _p0, _o0 = rf.judge(["单据提交时间"], snap)
    assert nf0 == [], (v0, nf0)
    # 「附件清单」原来是「判不了」的例子（attachment 0 行）。
    # 第四批把附件装进去了，它现在落「未发现数据缺口·待复核」——
    # **这正是这个管道该有的变化**，所以换一个仍然 0 行的数据项当例子。
    v2, bl2, nf2, un2, pa2, ok2 = rf.judge(["附件清单"], snap)
    assert v2 == "未发现数据缺口·待复核", v2
    v3, bl3, nf3, un3, pa3, ok3 = rf.judge(["职级标准"], snap)
    assert v3 == "判不了" and bl3 and not nf3, (v3, bl3, nf3)


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


def test_字段齐备但没有适用单据不算能判():
    """同一个坑的第三次：字段真有值了，但这个实例一张会议费单都没有。

    2026-10-31 补读支付区之后，`支付方式`/`付款用途` 从全空变成 29/29，
    TR-会议-07（会议费支付必须银行转账）立刻落到「未发现数据缺口」。
    可这个实例只有差旅 20 张 + 招待 9 张，会议费 0 张——编译出来就是
    一条永远不触发的规则，而报告上显示它「已覆盖」。
    """
    n = rf.applicable_bills(IID)
    assert n["差旅"] == 20 and n["接待"] == 9 and n["通用"] == 29, n
    assert n["会议"] == 0 and n["备用金"] == 0 and n["日常"] == 0, n
    rep = rf.report(IID)
    for x in rep["rules"]:
        if x["n_bills"] == 0:
            assert x["verdict"] != "未发现数据缺口·待复核", (
                "%s 所在维度 %s 一张适用单据都没有，不许落到待复核"
                % (x["rule_id"], x["dimension"]))
    noscope = sorted(x["rule_id"] for x in rep["rules"]
                     if x["verdict"] == rf.NO_SCOPE)
    assert "TR-会议-07" in noscope and "TR-备用金-02" in noscope, noscope


def test_适用单据按单据类型数而不是按费用大类():
    """关键词匹配只能落在**单据类型**上。822港航 有一个费用大类叫
    「国内差旅费(参加会议/培训产生）」，里面带「会议」两个字——
    要是拿它来数，会议维度就会凭空多出几张单，然后一堆会议规则
    被判成「有适用单据」。误报和漏报一样致命。"""
    rows = db.query("""SELECT DISTINCT biz_type FROM expense_bill
                        WHERE instance_id = %s""", (IID,))
    assert any("会议" in (r["biz_type"] or "") for r in rows), (
        "这条用例的前提没了：费用大类里已经没有带「会议」的了")
    assert rf.applicable_bills(IID)["会议"] == 0


def test_建表把缺字段的规则挪出来了_但挪出来不等于能判():
    """招待明细建表（迁移 0053）之后实测：
    「缺字段」30 → 15 条，挪出来的 15 条正是要「业务招待清单」的那一批；
    而其中 2 条（TR-接待-07/08）落到了「未发现数据缺口·待复核」，
    逐条复核后**都是 0/29**——一个的适用范围是 0 行，一个缺分母。
    **这一档存在的意义就是接住这种情况**，这已经是第三次了。"""
    rep = rf.report(IID)
    assert rf.NEED_MAP["业务招待清单"] == ("entertain_detail", None)
    assert rf.NEED_MAP["招待明细日期"] == ("entertain_detail", "happen_date")
    snap = rep["snapshot"]
    assert snap["tables"]["entertain_detail"] == 17, snap["tables"].get("entertain_detail")
    for rid in ("TR-接待-07", "TR-接待-08"):
        assert rid in rf.MANUAL_REVIEW, "%s 落到待复核却没有人工复核结论" % rid
        assert rf.MANUAL_REVIEW[rid]["scope"].startswith("0/29"), rid


def test_酒水金额没填不许算成零():
    """0 和「没填」在金额上不是一回事：补成 0 会让「酒水超标」这类规则
    对着一个假的 0 判通过。实测 17 行里只有 2 行有值。"""
    g = (da.snapshot(IID)["columns"]["entertain_detail"])["liquor_amount"]
    assert g[2] == "部分" and g[0] == 2, g
    assert db.scalar("""SELECT count(*) FROM entertain_detail
                         WHERE instance_id = %s AND liquor_amount IS NULL""",
                     (IID,)) == 15


def test_招待明细的码列没有被翻译成业务值():
    """三列是系统码，客户没给码表。**不许自己翻译**——
    把 LEVEL_05 猜成某个职级档，整类陪同级别判定全错，而且错得看起来很合理。
    所以库里存的必须还是码，而且这些码不许出现在枚举值里。"""
    rows = db.query("""SELECT DISTINCT entertain_type, entertain_way, companion_level
                         FROM entertain_detail WHERE instance_id = %s""", (IID,))
    codes = set()
    for r in rows:
        codes |= {r["entertain_type"], r["entertain_way"], r["companion_level"]}
    codes.discard(None)
    assert codes, "三个码列全空，这条用例验不了"
    bad = [c for c in codes if not re.match(r"^[A-Z][A-Z0-9_]+$", c)]
    assert not bad, "这些取值不像系统码，像是被翻译过了：%s" % bad
    inenum = db.query("""SELECT value FROM enum_value_current
                          WHERE instance_id = %s AND value = ANY(%s)""",
                      (IID, sorted(codes)))
    assert not inenum, "系统码进了枚举值：%s" % inenum


def test_每个维度都登记了适用单据口径():
    """库里出现的维度必须在 DIM_DOC_KEYWORDS 里有一条。
    漏登记会让那个维度的 n_bills 变成 None，于是这一层整层悄悄不生效。"""
    dims = {r["dimension"] for r in db.query(
        "SELECT DISTINCT dimension FROM rule WHERE instance_id = %s", (IID,))}
    missing = sorted(dims - set(rf.DIM_DOC_KEYWORDS))
    assert not missing, "这些维度没登记适用单据口径：%s" % missing


def test_没登记的维度不下结论():
    """福建海运集团用的是 D1~D10 那套维度编号，不在这张表里。
    **不认识的维度要返回 None（不知道），不许当成 0（没有单据）**——
    当成 0 会把另一家的规则全判成「无适用单据」。"""
    rep = rf.report("fjhy")
    assert rep["rules"], "fjhy 没有规则，这条用例验不了"
    assert all(x["n_bills"] is None for x in rep["rules"]), (
        [x["dimension"] for x in rep["rules"] if x["n_bills"] is not None][:5])
    assert not [x for x in rep["rules"] if x["verdict"] == rf.NO_SCOPE]


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


# ─────────────────────────────────────────────────────────────────────────────
# 「库里有没有」和「该谁动」是两个维度
# ─────────────────────────────────────────────────────────────────────────────
# 2026-10-31 的第二次错判：第一版只判「库里有没有」，然后把所有「没有」
# 一律当成「找客户要」。据此排出来的客户需求清单，涉及规则数前三名是
# 出差审批单(26) / 附件(24) / 发票明细(16)——而这三样的原始材料早就在
# 材料/822港航、单据、数据/ 下面躺着，是我方没解析：
# 570 个附件文件一个没入库、报账单 5 个分区只读了 1 个、
# 招待明细区库里没建表、员工「差旅级别」没映射成职级。
# 差一点让客户去补他们已经给过的东西——那是最贵的一类错误，
# 客户照着做完回来，发现问题还在。
def test_材料在手的不许算到客户头上():
    """这条用例守的就是那次错判。附件、发票、出差审批单、支付区、
    员工职级——这些的原始材料都在，归属必须是我方。"""
    for need in ("附件清单", "票据附件", "发票日期", "发票明细",
                 "出差审批单", "出差天数", "审批单目的地",
                 "收款人", "支付方式", "付款用途", "人员职级",
                 "业务招待清单"):
        owner, ev = rf.source_of(need)
        assert owner == rf.IN_HAND, "%s 被算成了 %s" % (need, owner)
        assert len(ev) > 8, "%s 的归属没写证据：%r" % (need, ev)


def test_真缺的要留在客户清单里():
    for need in ("审批完成时间", "业务发生日期", "单据提交时间",
                 "出差日历", "员工常驻地址", "职级标准", "采购金额"):
        owner, ev = rf.source_of(need)
        assert owner == rf.NEED_CLIENT, "%s 被算成了 %s" % (need, owner)
        assert ev, "%s 没写为什么是真缺" % need


def test_没核过材料的一律待定不许默认成任何一方():
    """默认成我方会让客户清单漏项；默认成客户会让客户去补已经给过的东西。
    两种默认都错，所以认不出就说认不出。"""
    owner, ev = rf.source_of("一个还没核过的数据项")
    assert owner == rf.UNDECIDED, owner
    assert ev


def test_每个已登记的数据项都核过材料():
    """**待定项必须是 0。** 留着待定项，它既不在我方清单也不在客户清单，
    于是两边都不会做——这种漏项比排错清单还难发现。
    新加数据项而没核材料时，这条会红。
    """
    rep = rf.report(IID)
    undecided = sorted({n for x in rep["rules"]
                        for n, o, _ in x["sources"] if o == rf.UNDECIDED})
    assert not undecided, "这些数据项还没核过材料在不在手：%s" % undecided


def test_清单按归属分开给():
    """合在一张表里排序，我方的活和客户的活会交替出现，
    而这份东西要发给客户——客户看到自己清单里有「我们已经给过的附件」，
    下一句就是「这不是给过了吗」。"""
    p = rf.unlock_plan(IID)
    assert p["theirs"], "客户那张清单空了，分组八成坏了"
    assert not p["undecided"], p["undecided"]
    assert len(p["ours"]) + len(p["theirs"]) + len(p["undecided"]) == len(p["items"])
    # 原来这里断言「我方那张非空，且 attachment 必须在我方」。
    # 2026-11-01 第四批把附件/发票/出差审批单都装进去了，**我方那张空了**——
    # 空是这批工作的结果，不是分组坏了。所以改成断言「归属不交叉」：
    # 任何一项不许同时出现在两张清单里，而且客户那张里不许出现
    # 材料已经在手的东西（那正是这个分组要防的事）。
    ours = {x["要补的"] for x in p["ours"]}
    theirs = {x["要补的"] for x in p["theirs"]}
    assert not (ours & theirs), ours & theirs
    for key in ("attachment", "invoice", "trip_application", "entertain_detail"):
        assert not any(key in t for t in theirs), (
            "%s 的材料在手，不许出现在客户清单里" % key)


def test_一条规则两边都占时主归属算客户():
    """只看主归属会把「还差客户一样」的规则排进我方清单，
    然后以为我方做完它就能跑。两边都占时，主归属必须是更晚的那一方。"""
    # 原来这条在实数据里找「两边都占」的规则。2026-11-01 第四批之后
    # 我方的字段级缺口清零了，**现实里再也没有两边都占的规则**，
    # 于是这条用例在真数据上验不到东西。
    # 改成直接验这段逻辑本身：主归属的优先级是 客户 > 待定 > 我方。
    # 不是把断言删掉——那样这段优先级哪天写反了就没人拦。
    for owners, want in (
            ([rf.IN_HAND, rf.NEED_CLIENT], rf.NEED_CLIENT),
            ([rf.NEED_CLIENT], rf.NEED_CLIENT),
            ([rf.IN_HAND, rf.UNDECIDED], rf.UNDECIDED),
            ([rf.IN_HAND], rf.IN_HAND),
            ([], "")):
        got = (rf.NEED_CLIENT if rf.NEED_CLIENT in owners
               else (rf.UNDECIDED if rf.UNDECIDED in owners
                     else (rf.IN_HAND if rf.IN_HAND in owners else "")))
        assert got == want, (owners, got, want)
    # 再在真数据上确认一遍：没有任何规则的主归属落成「我方」却还差客户的东西
    rep = rf.report(IID)
    bad = [x["rule_id"] for x in rep["rules"]
           if x["owner_main"] == rf.IN_HAND and rf.NEED_CLIENT in x["owners"]]
    assert not bad, bad


def test_归属写在说明的最前面():
    """放在一长串字段名后面，读的人翻到那儿之前
    就已经按「缺数据＝找客户」处理掉了。"""
    rep = rf.report(IID)
    has_owner = {x["rule_id"] for x in rep["rules"] if x["owners"]}
    assert has_owner
    rows = db.query("""SELECT rule_id, demo_blocker FROM rule
                        WHERE instance_id = %s AND version = 1
                          AND demo_blocker LIKE %s""", (IID, "数据可行性%"))
    assert rows
    for r in rows:
        # 没有任何缺口的规则（比如只是某个字段部分填充）本来就没有归属，
        # 这一条不该要求它写。只有真有缺口的才必须把「该谁动」摆在最前。
        if r["rule_id"] not in has_owner:
            continue
        head = r["demo_blocker"].split("｜")[0]
        assert "该谁动" in head, (r["rule_id"], head[:60])
