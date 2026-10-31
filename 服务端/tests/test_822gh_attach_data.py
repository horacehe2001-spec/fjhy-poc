# -*- coding: utf-8 -*-
"""822港航 附件链入库结果（附件 / 发票 / 出差审批单 / 逐日）。

装载程序本身是脚本（模块级就跑完），import 不进来，所以这里**测库里的结果**，
不测解析函数。这组用例钉的是解析过程中真实踩过的坑：

1. **全角 ￥**。铁路电子客票的金额用的是 ￥（U+FFE5），不是 ¥（U+00A5）。
   第一版正则只认半角，39 张火车票一张都没取到金额——而 39 张正是这批
   发票里最多的一类，交通费规则全靠它。

2. **sorted(dict) 给的是键不是键值对**。第一版按流顺序读 PDF 文字层，
   113 个 PDF 全报「打不开」，差一点据此判定「发票是扫描件、得上视觉」。
   错的是遍历，不是 PDF。

3. **同一张单里内容完全相同的两份附件**。客户导出会把同一张发票
   用两个文件名各存一份（「机票.pdf」和「发票附件-<号>.pdf」）。
   库里 attachment(bill_no, sha256) 唯一约束拦住了——那道闸是对的。

4. **「字段齐备」不等于「能判」**。197 个扫描件还没解析，拿一个不全的
   票据集合去判「凭证缺失」就是误报。这一点落在 rule_feasibility 的
   复核结论里，见下面最后一组。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, rule_feasibility as rf  # noqa: E402

IID = "822gh"


def _n(sql, *a):
    return db.scalar(sql, a or (IID,))


# ── 入库结果 ────────────────────────────────────────────────────────────
def test_附件发票审批单都入库了且只在本实例():
    assert _n("SELECT count(*) FROM attachment WHERE instance_id=%s") == 147
    assert _n("SELECT count(*) FROM invoice WHERE instance_id=%s") == 53
    assert _n("SELECT count(*) FROM trip_application WHERE instance_id=%s") == 15
    assert _n("SELECT count(*) FROM trip_calendar WHERE instance_id=%s") == 87
    # 别家一行不动（这几个数是第四批之前实测的基线）
    assert db.scalar("SELECT count(*) FROM attachment WHERE instance_id='fjhy'") == 273
    assert db.scalar("SELECT count(*) FROM invoice WHERE instance_id='fjhy'") == 184
    assert db.scalar(
        "SELECT count(*) FROM trip_application WHERE instance_id='fjhy'") == 49


def test_附件按内容去重_同一张单里不许有两份一样的():
    """客户导出把同一张发票用两个文件名各存一份。
    库里 (bill_no, sha256) 是唯一约束，装载侧必须先去重，
    **不是去放宽约束**。"""
    dup = db.query("""SELECT bill_no, sha256, count(*) n FROM attachment
                       WHERE instance_id = %s GROUP BY 1,2 HAVING count(*) > 1""",
                   (IID,))
    assert not dup, dup
    assert _n("SELECT count(*) FROM attachment WHERE instance_id=%s "
              "AND source_kind <> 'zip附件包'") == 0


def test_附件来源不冒充上传():
    """source_kind='上传' 在这套系统里指走文件库上传链路、有 doc_lib_upload
    留痕的文件。这批是客户交付的嵌套 zip，没有那条留痕，
    硬标成「上传」会让「这个附件哪来的」查不清。"""
    assert _n("""SELECT count(*) FROM attachment WHERE instance_id=%s
                   AND source_ref IS NOT NULL""") == 147
    assert _n("""SELECT count(*) FROM attachment WHERE instance_id=%s
                   AND attach_type IS NOT NULL""") == 0, (
        "附件类型被填上了。本实例没有客户自己的附件类型词表"
        "（EN17 是空枚举），按文件名猜一套出来是在编客户的分类口径")


# ── 发票 ────────────────────────────────────────────────────────────────
def test_铁路电子客票的金额取到了_全角人民币符号():
    """这条是为全角 ￥ 那个 bug 留的。铁路票是这批里最多的一类。"""
    # 39 是**文件**数，29 是去重后的**发票**数（同一张票两个文件名各存一份）。
    # 这两个数要分清：按文件数写断言会红，而红的是断言不是数据。
    n_rail = _n("""SELECT count(*) FROM invoice WHERE instance_id=%s
                     AND inv_type = '铁路电子客票'""")
    assert n_rail == 29, "铁路票 %d 张，和实测的 29 张对不上" % n_rail
    no_amt = _n("""SELECT count(*) FROM invoice WHERE instance_id=%s
                     AND inv_type = '铁路电子客票' AND total_amount IS NULL""")
    assert no_amt == 0, "%d 张铁路票没取到金额——全角 ￥ 又没认" % no_amt
    # 席别在 detail_text 里（交通工具等级规则要用）
    assert _n("""SELECT count(*) FROM invoice WHERE instance_id=%s
                   AND detail_text ~ '等座|卧'""") >= 29


def test_发票号唯一且票面标题全部映到了本体枚举():
    dup = db.query("""SELECT inv_no, count(*) n FROM invoice WHERE instance_id=%s
                       GROUP BY 1 HAVING count(*) > 1""", (IID,))
    assert not dup, dup
    assert _n("SELECT count(*) FROM invoice WHERE instance_id=%s "
              "AND inv_type IS NULL") == 0, "有发票的类型没映上，会被外键拒或留空"
    kinds = {r["inv_type"] for r in db.query(
        "SELECT DISTINCT inv_type FROM invoice WHERE instance_id=%s", (IID,))}
    assert kinds == {"数电普票", "数电专票", "铁路电子客票"}, kinds


def test_票面上没有的字段留空而不是补零():
    """铁路电子客票没有税额/税率、也不写销售方——**票面本来就没有**。
    补一个 0 会让「税率是否正确」那类规则对着假数据判通过。"""
    assert _n("""SELECT count(*) FROM invoice WHERE instance_id=%s
                   AND inv_type='铁路电子客票' AND tax_amount IS NOT NULL""") == 0
    assert _n("""SELECT count(*) FROM invoice WHERE instance_id=%s
                   AND total_amount IS NULL""") == 3, (
        "还取不到金额的张数变了；变少是好事，但这个数要跟着改，"
        "别让它悄悄从 3 涨回去")


# ── 出差审批单与逐日 ────────────────────────────────────────────────────
def test_出差审批单的表头字段全取到了():
    for col in ("start_date", "end_date", "place", "days", "person_count",
                "approve_date", "backfill_date", "reason"):
        n = _n("SELECT count(%s) FROM trip_application WHERE instance_id=%%s" % col)
        assert n == 15, "%s 只取到 %d/15" % (col, n)
    # 填报日期那一格的标签排在值后面，按「标签—值」取会 0/15。这条钉住它。
    assert _n("""SELECT count(*) FROM trip_application WHERE instance_id=%s
                   AND backfill_date > approve_date""") == 0, (
        "填报日期晚于审批日期，说明这两格取反了")


def test_逐日表一天三行_餐次齐全():
    """口径：一天三行（早/中/晚），has_meal = 该餐不是「自行用餐（含免费）」。
    **这个口径是装载程序定的**（库里原来一行都没有），改口径就改这条用例。"""
    bad = db.query("""SELECT app_no, cal_date, count(*) n FROM trip_calendar
                       WHERE instance_id = %s GROUP BY 1,2 HAVING count(*) <> 3""",
                   (IID,))
    assert not bad, bad
    slots = {r["meal_slot"] for r in db.query(
        "SELECT DISTINCT meal_slot FROM trip_calendar WHERE instance_id=%s", (IID,))}
    assert slots == {"早", "中", "晚"}, slots
    assert _n("""SELECT count(*) FROM trip_calendar WHERE instance_id=%s
                   AND has_meal""") == 24


def test_逐日地点仍然是空的_这才是真缺口():
    """出差审批单给了逐日的用餐和用车，**没给逐日地点**。
    整单只有一个出差地点，所以判不了「在途每天按哪个目的地的标准计补助」。
    这一格空着是实话；哪天它有值了，客户侧那一项就该划掉。"""
    assert _n("""SELECT count(place) FROM trip_calendar WHERE instance_id=%s""") == 0


# ── 归属判断：这一批改正了三处 ───────────────────────────────────────────
def test_出差日历不许整项判给客户():
    """原来整项判给客户，理由是「没有逐日的出差日历」。
    错了一半：逐日用餐/用车在出差审批单里，已入库 87 行。"""
    owner, ev = rf.source_of("出差日历")
    assert owner == rf.NEED_CLIENT, owner        # 逐日地点确实还缺
    assert "已在手并入库" in ev and "逐日地点" in ev, ev


def test_审批流水要分清是哪条流程():
    """出差审批那条流程的流水在手（15 份 PDF 上有四个角色 + 3 个时间戳）；
    缺的是报账单那条流程的流水。笼统说「流水缺」会让客户去补已经给过的那一半。"""
    owner, ev = rf.source_of("审批时间")
    assert owner == rf.NEED_CLIENT, owner
    assert "报账单" in ev and "出差审批" in ev, ev


def test_一个姓名列不许填成两列():
    """导出主表区只有一个「姓名」列。把 claimant_no 也填成它，
    等于断言「报账人就是经办人」，而有规则正是在比这两者——
    填成相等会让那类比较永远成立。所以这一项归客户定口径。"""
    assert db.scalar("""SELECT count(claimant_no) FROM expense_bill
                         WHERE instance_id=%s""", (IID,)) == 0, (
        "claimant_no 被填上了——那是一次口径断言，不是解析")
    owner, ev = rf.SOURCE_STATUS["expense_bill.claimant_no"]
    assert owner == rf.NEED_CLIENT, owner
    assert "口径" in ev, ev


def test_复核发现的缺口必须进清单_不许只留在文字里():
    """**这条用例是为一个我自己犯过的错留的。**

    第四批复核 28 条规则时发现，9 条缺的是标准表里的某一类标准，
    而模型的 need_fields 一条都没列。我当时把结论写进了 MANUAL_REVIEW 的
    why 文字里——然后 unlock_plan 看不到它，「我方还剩什么」显示 **0 项**，
    而那张清单是要发给客户的：少报一项我方的活，就等于把活推给客户。
    现在那 9 条的额外数据项登记在 EXTRA_NEEDS 里，进 judge、进 unlock_plan。

    另外注意「表里有行」不等于「有我要的那一类行」：standard_limit 有
    317 行，看着像标准都在，而 317 行**全是住宿费限额**。
    """
    p = rf.unlock_plan(IID)
    ours = {x["要补的"] for x in p["ours"]}
    assert ours == {"标准表里缺这一类：交通工具等级标准",
                    "标准表里缺这一类：伙食补助费标准",
                    "标准表里缺这一类：市内交通费标准"}, ours
    assert p["undecided"] == []
    assert len(p["theirs"]) >= 20
    # 这三类在库里确实 0 行，而住宿费那一类有 317 行
    kinds = rf.da.snapshot(IID)["kinds"]["standard_limit.std_type"]
    assert kinds == {"住宿费限额标准"}, kinds
    # 归属是我方（制度里有，抽取没落到标准表），不许算到客户头上
    for n in ("交通工具等级标准", "伙食补助费标准", "市内交通费标准"):
        owner, ev = rf.source_of(n)
        assert owner == rf.IN_HAND, (n, owner)
        assert "0 行" in ev or "抽取" in ev, (n, ev)


def test_表里有行不等于有那一类行():
    """只按表行数判，这三类规则会显示「数据齐备」而一条都判不了。
    这是同一个坑的第四次：自报 auto_judgeable → 自报 need_fields 齐备
    → 字段有值但无适用单据 → 表有行但没有那一类行。"""
    snap = rf.da.snapshot(IID)
    assert snap["tables"]["standard_limit"] == 317
    v, bl, nf, un, pa, ok = rf.judge(["交通工具等级标准"], snap)
    assert v == "判不了", v
    assert bl and "没有「交通工具等级标准」这一类" in bl[0], bl
    v2 = rf.judge(["住宿费限额标准"], snap)[0]
    assert v2 == "未发现数据缺口·待复核", v2


# ── 复核结论本身 ────────────────────────────────────────────────────────
def test_每条待复核的都有复核结论():
    rep = rf.report(IID)
    pending = [x["rule_id"] for x in rep["rules"]
               if x["verdict"] == "未发现数据缺口·待复核"]
    # 31 → 23：EXTRA_NEEDS 把 8 条带标准表缺口的规则挪去了「判不了」。
    # 这个数会随数据装载变，所以只钉下限和「一条都不许没人复核」。
    assert len(pending) >= 20, len(pending)
    assert not sorted(set(pending) - set(rf.MANUAL_REVIEW))


def test_等视觉的那些规则范围必须写零():
    """扫描件没解析完就判「凭证缺失」是误报。
    这几条的复核结论必须是 0/29，**不许给一个非零范围**——
    非零范围会被当成「这条可以上线了」。"""
    for rid in ("TR-差旅-08", "TR-差旅-21", "TR-差旅-29", "TR-通用-05",
                "TR-通用-02", "TR-接待-23"):
        sc = rf.MANUAL_REVIEW[rid]["scope"]
        assert sc.startswith("0/"), (rid, sc)


def test_真能判的那几条范围要和库里数得出来的一致():
    """复核结论里给了非零范围的，范围必须对得上库里的数。
    写一个漂亮但对不上的数，比不写更坏。"""
    n_ent_bills = _n("""SELECT count(DISTINCT bill_no) FROM entertain_detail
                         WHERE instance_id=%s""")
    assert n_ent_bills == 9, n_ent_bills
    for rid in ("TR-接待-04", "TR-接待-22"):
        assert rf.MANUAL_REVIEW[rid]["scope"].startswith("9/29"), rid
    # 招待单里有电子发票的只有 2 张，靠发票类目判的那几条就只能是 2/29
    n_inv_bills = _n("""SELECT count(DISTINCT i.bill_no) FROM invoice i
                         JOIN expense_bill b ON b.bill_no = i.bill_no
                        WHERE i.instance_id=%s AND b.doc_type='业务招待费报账单'""")
    assert n_inv_bills == 2, n_inv_bills
    for rid in ("TR-接待-10", "TR-接待-11", "TR-接待-13", "TR-接待-14"):
        assert rf.MANUAL_REVIEW[rid]["scope"].startswith("2/29"), rid
