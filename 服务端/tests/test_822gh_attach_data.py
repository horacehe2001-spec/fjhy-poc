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
    # 15 文字层 + 7 读图。**读图那一批从 8 变 7 是重读的结果，不是漏了一份**：
    # 2026-11-01 第四轮补逐日表时顺带重读了这 8 份，
    # 6f8ca644…jpg 第二次模型自报把握从「中」降到「低」，
    # 低把握不回写（本模块的既有闸），于是它那一行没了。
    # 这是对的：同一张图两次读出来的起止日期是 2024-03-04~2024-03-18，
    # 而报账单是 2026 年 3 月——差两年，本来就不该当成读准了。
    assert _n("SELECT count(*) FROM trip_application WHERE instance_id=%s") == 22
    assert _n("""SELECT count(*) FROM trip_application
                  WHERE instance_id=%s AND extracted_from='审批单文字层'""") == 15
    assert _n("""SELECT count(*) FROM trip_application
                  WHERE instance_id=%s AND extracted_from='审批单读图'""") == 7
    # 90 不是 87：逐日行按**用餐段与用车段日期的并集**铺（2026-11-01 改）。
    # 原来只按用餐段铺，某天只填了用车就整天不入库，判「逐日表覆盖了几天」时
    # 会把它算成「两样都没填」——比实际严重，报告上多报一天。
    # 111 = 文字层 90 + 读图 21（第四轮给 A01 的提示词补了那两张小表）。
    assert _n("SELECT count(*) FROM trip_calendar WHERE instance_id=%s") == 111
    assert _n("""SELECT count(*) FROM trip_calendar c
                   JOIN trip_application t ON t.app_no = c.app_no
                  WHERE c.instance_id=%s AND t.extracted_from='审批单读图'""") == 21
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
    # 原来这里断言 attach_type **全空**——那是定类之前的事实，不是这条用例
    # 要验的东西（它验的是「来源不冒充上传」）。2026-11-01 定类做完，
    # 133 个有了类型，用例就红了。改成验真正该验的：
    # 填上去的类型必须都落在本实例的 EN17 里，来路必须是记得清的那四种之一。
    bad = db.query("""SELECT a.attach_type, a.type_source FROM attachment a
                       WHERE a.instance_id = %s AND a.attach_type IS NOT NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM enum_value_current e
                              WHERE e.instance_id = a.instance_id
                                AND e.enum_id = 'EN17'
                                AND e.value = a.attach_type)""", (IID,))
    assert not bad, bad
    srcs = {r["type_source"] for r in db.query(
        """SELECT DISTINCT type_source FROM attachment
            WHERE instance_id=%s AND attach_type IS NOT NULL""", (IID,))}
    assert srcs <= {"读图判定", "文字层解析"}, srcs


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
    """**只看文字层解出来的那 15 份。** 2026-11-01 读图批又写了 7 份，
    那两轮的提示词问的字段比文字层少（第三轮只问表头三个字段，
    第四轮补了逐日表和签名日期，但天数/人数/事由/审批日期仍然没问）——
    其余列本来就是空的，混进来这条用例就变成在骂读图批没读它没问的东西。
    「哪些份该有全部字段」是按**来源**分的，不是按实例分的。"""
    for col in ("start_date", "end_date", "place", "days", "person_count",
                "approve_date", "backfill_date", "report_date", "reason"):
        n = _n("""SELECT count(%s) FROM trip_application
                   WHERE instance_id=%%s AND extracted_from='审批单文字层'""" % col)
        assert n == 15, "%s 只取到 %d/15" % (col, n)
    # 填报日期那一格的标签排在值后面，按「标签—值」取会 0/15。这条钉住它。
    # **2026-11-01 搬了一列**：原来这个断言写在 backfill_date 上，
    # 因为那时 backfill_date 里装的就是填报日期。migration 0056 把
    # backfill_date 订正成「出差人签名日期」、填报日期挪到 report_date 之后，
    # 这个守的还是同一件事，只是换到了正确的列上——**断言不能跟着删**，
    # 它防的是「标签排在值后面」那个解析陷阱。
    assert _n("""SELECT count(*) FROM trip_application WHERE instance_id=%s
                   AND extracted_from='审批单文字层'
                   AND report_date > approve_date""") == 0, (
        "填报日期晚于审批日期，说明这两格取反了")
    # 两个日期的业务含义：填报在出差之前、签名在出差之后。
    # 哪天又把填报日期塞回 backfill_date，这两条会红。
    assert _n("""SELECT count(*) FROM trip_application WHERE instance_id=%s
                   AND extracted_from='审批单文字层'
                   AND backfill_date < end_date""") == 0, (
        "出差人签名日期早于出差结束日期，说明 backfill_date 又装回填报日期了")
    assert _n("""SELECT count(*) FROM trip_application WHERE instance_id=%s
                   AND extracted_from='审批单文字层'
                   AND report_date = backfill_date""") == 0, (
        "填报日期和签名日期相等，说明两列装的是同一个值（实测 15 份全不同）")


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
    # 2026-11-01：这三类标准当天就抽进库了，所以我方清单又空了。
    # **空不是这条用例的重点**——重点是「复核发现的缺口要能被 unlock_plan
    # 看见」这条路还通着，下面 test_表里有行不等于有那一类行 钉的就是它。
    assert {x["要补的"] for x in p["ours"]} == set(), [x["要补的"] for x in p["ours"]]
    assert p["undecided"] == []
    assert len(p["theirs"]) >= 20
    # 四类标准现在都在库里了：三类在 standard_limit，交通工具等级在 lookup_table
    # （它的值是「允许坐什么等级」不是金额，塞不进 limit_amount）
    kinds = rf.da.snapshot(IID)["kinds"]
    assert kinds["standard_limit.std_type"] == {
        "住宿费限额标准", "伙食补助费标准", "市内交通费标准"}, kinds["standard_limit.std_type"]
    assert "交通工具等级标准" in kinds["lookup_table.table_id"]
    # 归属仍然是我方（制度里有，是我方抽取），**不许因为做完了就改成客户**
    for n in ("交通工具等级标准", "伙食补助费标准", "市内交通费标准"):
        owner, ev = rf.source_of(n)
        assert owner == rf.IN_HAND, (n, owner)


def test_表里有行不等于有那一类行():
    """standard_limit 有 317 行时看着像「标准都在」，而 317 行全是住宿费限额。
    这是同一个坑的第四次：自报 auto_judgeable → 自报 need_fields 齐备
    → 字段有值但无适用单据 → 表有行但没有那一类行。

    **用构造的快照验这个机制**，不依赖当下的库：三类标准 2026-11-01 已经
    抽进库了，拿实数据验就验不到「缺这一类」那条分支，而那条分支正是
    这个机制的全部意义。"""
    snap = {"tables": {"standard_limit": 317}, "columns": {},
            "kinds": {"standard_limit.std_type": {"住宿费限额标准"},
                      "lookup_table.table_id": set()}}
    v, bl, nf, un, pa, ok = rf.judge(["伙食补助费标准"], snap)
    assert v == "判不了", v
    assert bl and "没有「伙食补助费标准」这一类" in bl[0], bl
    assert not nf, "缺一类行不是缺字段，两者给人的动作不同"
    # 有那一类行就算具备
    assert rf.judge(["住宿费限额标准"], snap)[0] == "未发现数据缺口·待复核"
    # 交通工具等级标准查的是 lookup_table 那一列，不是 standard_limit
    assert rf.judge(["交通工具等级标准"], snap)[0] == "判不了"
    snap["kinds"]["lookup_table.table_id"] = {"交通工具等级标准"}
    assert rf.judge(["交通工具等级标准"], snap)[0] == "未发现数据缺口·待复核"


def test_三类标准是从本实例自己的制度抽的():
    """取值和福建海运集团**完全相同**（100/120/100、80），
    但来源必须是 822港航 自己的条款号——同值只是交叉核对，不是出处。
    来源记错了，制度改版时就不知道该跟着哪一份改。"""
    rows = db.query("""SELECT std_type, region, limit_amount, source_clause,
                              confidence, cross_checked
                         FROM standard_limit WHERE instance_id = %s
                          AND std_type IN ('伙食补助费标准','市内交通费标准')
                        ORDER BY std_type, region""", (IID,))
    assert len(rows) == 4, rows
    for r in rows:
        assert "闽港发资财〔2026〕3号" in r["source_clause"], r["source_clause"]
        assert r["confidence"] == "原生文本"
        assert r["cross_checked"] is True, "新旧两版逐条对过，这一列应为 True"
    amts = {(r["std_type"], r["region"]): float(r["limit_amount"]) for r in rows}
    assert amts[("伙食补助费标准", "省内")] == 100.0
    assert amts[("伙食补助费标准", "省外（西藏、青海、新疆）")] == 120.0
    assert amts[("市内交通费标准", "")] == 80.0


def test_交通工具等级不进金额表():
    """那张表的值是「允许坐什么等级」，不是钱。塞进 standard_limit 就得把
    等级写进 region 或另造一列——又是一列两种语义，这个项目为此踩过三次。"""
    assert db.scalar("""SELECT count(*) FROM standard_limit
                         WHERE instance_id=%s AND std_type='交通工具等级标准'""",
                     (IID,)) == 0
    n = db.scalar("""SELECT count(*) FROM lookup_row WHERE instance_id=%s
                      AND table_id='交通工具等级标准'""", (IID,))
    assert n == 8, "2 个级别 × 4 种交通工具 = 8 行，实际 %d" % n
    # 级别名原样抄制度，**没有往住宿费档位（EN21）上映射**
    tiers = {r["row_key"]["tier"] for r in db.query(
        """SELECT row_key FROM lookup_row WHERE instance_id=%s
            AND table_id='交通工具等级标准'""", (IID,))}
    assert len(tiers) == 2, tiers
    en21 = {r["value"] for r in db.query(
        "SELECT value FROM enum_value_current WHERE instance_id=%s AND enum_id='EN21'",
        (IID,))}
    assert not (tiers & en21) or tiers != en21, (
        "交通工具等级表的档位被映射成住宿费档位了——两张表不是一套档位，"
        "两个「其他人员」是不是同一群人还没问过客户")


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
                "TR-接待-23"):
        sc = rf.MANUAL_REVIEW[rid]["scope"]
        assert sc.startswith("0/"), (rid, sc)


def test_提单日期回填了_但补充说明区一个字都不许动():
    """**这条用例守的是一次真实的事故。**

    doc_date 本实例 29 张全空，而值在单据号前六位里
    （FJPG-BX**260728**0255 = 2026-07-28，实测 29/29、提单日 ≥ 业务发生日
    0 条反例）。回填是对的。

    但第一版把「这个值是算出来的」这句标记**追加进了 expense_bill.remark**，
    而那一列是业务上的**补充说明区**：原来 19/29 有值，追加之后 29/29 非空，
    其中 10 张里只有我写的标记。
    后果当场量到：规则可行性里「部分字段不全」从 17 条掉到 2 条——
    因为 TR-差旅-06/07/25/31/33、TR-接待-15、TR-通用-04 判的正是
    「补充说明区是否填写了必要信息」，它们看到一列非空的「说明」，
    而那不是客户填的说明。**审计痕迹写进了被审计的字段。**

    来源现在在 doc_date_source（migration 0058）。这条用例钉三件事：
    回填到了、来源标了、**remark 回到 19**。
    """
    assert _n("SELECT count(doc_date) FROM expense_bill WHERE instance_id=%s") == 29
    assert _n("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                   AND doc_date_source = '单据号推出'""") == 29
    assert _n("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                   AND remark IS NOT NULL AND remark <> ''""") == 19, (
        "补充说明区非空的张数不是 19——有东西写进了业务列")
    assert _n("""SELECT count(*) FROM expense_bill WHERE instance_id=%s
                   AND remark LIKE %s""", IID, "%非客户字段%") == 0, (
        "remark 里又出现了来源标记")
    # 提单日 ≥ 业务发生日，这是回填成立的判据，不许退化
    bad = db.query("""SELECT b.bill_no, b.doc_date::date d,
                             coalesce((SELECT max(e.happen_date) FROM entertain_detail e
                                        WHERE e.bill_no=b.bill_no),
                                      (SELECT max(t.end_date) FROM trip_application t
                                        WHERE t.bill_no=b.bill_no),
                                      (SELECT max(i.issue_date) FROM invoice i
                                        WHERE i.bill_no=b.bill_no)) biz
                        FROM expense_bill b WHERE b.instance_id=%s""", (IID,))
    for r in bad:
        if r["biz"]:
            assert r["d"] >= r["biz"], dict(r)
    # 别家不许被动
    assert db.scalar("""SELECT count(doc_date) FROM expense_bill
                         WHERE instance_id='fjhy'""") == 69


def test_接待审批单与审批流水入库了_枚举是本实例自己的():
    """业务接待审批单 29 份里 2 份是 PDF 有文字层，解出来的审批流转记录
    进了 approval_record——那张表本实例原来 **0 行**，而
    「审批完成时间」正是 TR-接待-01 / TR-通用-03 / TR-通用-04 / TR-会议-01
    四条规则的拦路输入。

    拦路的其实是**本体里没有 EN16 审批节点枚举**：approval_record.node
    有外键指过去，没有取值一行都入不了库。取值逐字抄自客户票面的 4 个节点，
    **不是从福建海运集团那 10 个里挑的**（那边是报账流程的节点）。
    """
    # 2026-11-01 读图批补了 4 份（26 份 JPG + 1 份扫描 PDF 读完，
    # 只有 4 份是真·业务接待审批单且有接待时间；其余是简餐审批单、
    # 酒水领用单，或只有签名没有日期的手写表）。
    assert _n("""SELECT count(*) FROM entertain_approval
                  WHERE instance_id=%s AND extracted_from='审批单文字层'""") == 2
    assert _n("""SELECT count(*) FROM entertain_approval
                  WHERE instance_id=%s AND extracted_from='审批单读图'""") == 4
    assert _n("SELECT count(*) FROM entertain_approval WHERE instance_id=%s") == 6
    assert _n("""SELECT count(*) FROM approval_record
                  WHERE instance_id=%s AND flow='业务接待审批'""") == 16
    # **两批必须分得开**：来源列是 migration 0060 加的，没有它，
    # 文字层那个装载程序重跑一次就会把读图的成果整批删掉。
    assert _n("""SELECT count(*) FROM approval_record
                  WHERE instance_id=%s AND extracted_from IS NULL""") == 0
    assert db.scalar("""SELECT count(*) FROM approval_record
                         WHERE instance_id='fjhy'""") == 388, "别家被动了"
    en16 = {r["value"] for r in db.query(
        """SELECT value FROM enum_value_current
            WHERE instance_id=%s AND enum_id='EN16'""", (IID,))}
    assert en16 == {"发起节点", "部门负责人", "分管领导", "主管领导"}, en16
    # fjhy 的 EN16 是另一套（报账流程节点），不许被改成一样
    en16_f = {r["value"] for r in db.query(
        """SELECT value FROM enum_value_current
            WHERE instance_id='fjhy' AND enum_id='EN16'""")}
    assert len(en16_f) == 10 and "AI审核" in en16_f, en16_f
    # 节点名必须都在枚举里（外键保证，这里是显式复核）
    assert _n("""SELECT count(*) FROM approval_record a WHERE a.instance_id=%s
                   AND NOT EXISTS (SELECT 1 FROM enum_value_current e
                                    WHERE e.instance_id=a.instance_id
                                      AND e.enum_id='EN16' AND e.value=a.node)""") == 0


def test_接待审批单的预算总额要和标准乘人数对上():
    """票面自洽是解析对不对的免费判据：600 × 10 = 6000、600 × 8 = 4800。

    第一版 预算总额 取到的是 None——票面是「预算总额 元」+ 换行 + 「6,000.00」,
    标签行后面跟的是**单位**不是数值，而「元」不为空，于是不去看下一行。
    空值兜底和「取到了但不是数」是两种情况。
    """
    rows = db.query("""SELECT app_no, meal_standard, meal_persons, budget_total
                         FROM entertain_approval
                        WHERE instance_id=%s AND extracted_from='审批单文字层'""",
                    (IID,))
    assert len(rows) == 2, len(rows)
    for r in rows:
        assert r["meal_standard"] and r["meal_persons"] and r["budget_total"], dict(r)
        assert (float(r["meal_standard"]) * int(r["meal_persons"])
                == float(r["budget_total"])), dict(r)


def test_读图那一批一个金额都没落库():
    """2026-10-22 用户决策：这一轮读图只抽文字/日期/布尔，不问金额。

    业务接待审批单票面上「标准（元/人）」和「预算总额」都看得清
    （实测手写表上写着 600元/人、4200元），**但提示词一个都没问**。
    要不要放开是口径决策，决策权在用户——这条用例防的是
    「反正看得见，顺手加上」。哪天真要放开，先改口径再改这条。
    """
    rows = db.query("""SELECT app_no, meal_standard, budget_total
                         FROM entertain_approval
                        WHERE instance_id=%s AND extracted_from='审批单读图'""",
                    (IID,))
    assert len(rows) == 4, len(rows)
    for r in rows:
        assert r["meal_standard"] is None and r["budget_total"] is None, dict(r)


def test_培训通知的活动起止解出来了():
    """8 份会议/培训通知：1 份有文字层（零成本），7 份扫描件 2026-11-01 读图。

    文字层那份正文自报「定于8 月22 日至28 日」，解出 2026-08-22~2026-08-28，
    和人工原话「体现出差时间应为8.22-8.28」逐字一致——这是整条
    TR-差旅-30 第三方比对的锚点，不许退化。
    读图补上 5 份（干部培训班 05-24~05-28 三张、省属国企党支部书记班、邀请函）。
    """
    rows = db.query("""SELECT bill_no, notice_start, notice_end, notice_place
                         FROM meeting_notice WHERE instance_id=%s
                        ORDER BY bill_no""", (IID,))
    assert len(rows) == 6, [r["bill_no"] for r in rows]
    by = {r["bill_no"]: r for r in rows}
    r = by["FJPG-BX2608311795"]
    assert str(r["notice_start"]) == "2026-08-22", r["notice_start"]
    assert str(r["notice_end"]) == "2026-08-28", r["notice_end"]
    human = db.scalar("""SELECT return_reason FROM manual_review
                          WHERE bill_no='FJPG-BX2608311795'""")
    assert "8.22-8.28" in human, human
    # 起止一律 起 ≤ 止、跨度 ≤ 31 天——读图读反了要在入库前就被挡掉
    for x in rows:
        assert x["notice_start"] and x["notice_end"], dict(x)
        assert x["notice_start"] <= x["notice_end"], dict(x)
        assert (x["notice_end"] - x["notice_start"]).days <= 31, dict(x)
    # 同一期干部培训班挂在三张单上，三张解出来的区间必须一致
    same = {str(by[b]["notice_start"]) + "~" + str(by[b]["notice_end"])
            for b in ("FJPG-BX2606011481", "FJPG-BX2606050495",
                      "FJPG-BX2606050568")}
    assert same == {"2026-05-24~2026-05-28"}, same


def test_通用02已经不靠读图了_剩下两张要明说没读到():
    """**TR-通用-02 从这张「等视觉」名单里摘出来了，摘的理由要能复核。**

    原来它的结论是「0/29 张（判的就是图片内容）」。那个结论是错的：
    20 张差旅单的出差审批单是 PDF 且有文字层，用餐/用车逐日表和签名日期
    都能确定性解析——而且**上一次复核的 why 里就写着「用车用餐都取到了」，
    scope 那一格却写了 0/29**。证据对、结论错，在复核清单上看起来是
    「已经看过了」，这种最难发现。

    摘出来之后要守住另一头：剩下 6 张审批单确实是扫描件/JPG，
    它们必须是 pending，**不许因为规则整体能跑了就算进「通过」**。

    **2026-11-01 第三轮：读图把这 8 份读进来了，但只问了表头三个字段**，
    逐日表和签名日期没读。当时的判据是「这张单的审批单全部来自读图批」，
    数字仍是 6。

    **第四轮：提示词补上了逐日表和签名日期，判据换成 detail_read**
    （migration 0062 的列，TRUE = 这三块抽过了）。数字从 6 降到 2，
    而且这两张**不是「还没做」**：它们是 image_t0000000896/897，
    整页年份被读成 2016（实为 2026），表头日期和逐日行都被年份闸挡下，
    所以 detail_read 留在 FALSE。另有 1 张差旅单（FJPG-BX2603240507）
    重读后模型自报把握降到「低」，整行不回写——它退回「没有审批单」。

    **判据不许再写成 extracted_from。** 来源和覆盖面是两个维度：
    按来源判，下一轮提示词再补字段时，读到了也会被判成 pending（漏判）。
    """
    sc = rf.MANUAL_REVIEW["TR-通用-02"]["scope"]
    assert sc.startswith("20/29"), sc
    # 读图批之后只剩 1 张差旅单一份审批单都没有（低把握那张不回写）
    assert _n("""SELECT count(*) FROM expense_bill b
                  WHERE b.instance_id=%s AND b.doc_type='差旅费报账单'
                    AND NOT EXISTS (SELECT 1 FROM trip_application t
                                     WHERE t.bill_no=b.bill_no)""") == 1
    # 有审批单、但其中有份没抽过逐日表/签名日期的差旅单 —— 这就是「判不了 2」
    n_unread = _n("""SELECT count(*) FROM expense_bill b
                      WHERE b.instance_id=%s AND b.doc_type='差旅费报账单'
                        AND EXISTS (SELECT 1 FROM trip_application t
                                     WHERE t.bill_no=b.bill_no)
                        AND EXISTS (SELECT 1 FROM trip_application t
                                     WHERE t.bill_no=b.bill_no
                                       AND NOT t.detail_read)""")
    assert n_unread == 1, n_unread
    assert "没读" in rf.MANUAL_REVIEW["TR-通用-02"]["why"], (
        "复核结论里没说清这些是「没读」不是「没填」")
    # 抽过的那些，三块必须是一整套：逐日行有了，签名日期也该有
    # （票面真没有签名日期是允许的——那正是这条规则要命中的情形——
    #  所以只钉「读图来源里 detail_read 的份数」，不钉签名日期非空）
    assert _n("""SELECT count(*) FROM trip_application
                  WHERE instance_id=%s AND extracted_from='审批单读图'
                    AND detail_read""") == 5
    assert "判不了 2" in sc, sc
    # 人工为「未体现出差人回填情况（签字及日期）」驳过的那张就在这 6 张里，
    # 所以「签名回填」那一半本批没有阳性样本。这件事必须写在复核结论里，
    # 不能让报告读成「本批全部有回填」。
    why = rf.MANUAL_REVIEW["TR-通用-02"]["why"]
    assert "本批没有阳性样本" in why, "复核结论里没说清②这一半判不到阳性样本"


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


def test_标准入库后重核的几条范围也要对得上库():
    """三类标准 2026-11-01 入库后，7 条规则的复核结论从「缺标准」重写过。
    **重写的结论同样要能被库里的数验证**，否则就只是换了一段好听的话。"""
    # 明确非领导（部门负责人/员工/其他员工）且有火车票的单据数
    n_clear = db.scalar("""
        SELECT count(DISTINCT b.bill_no) FROM expense_bill b
          JOIN invoice i ON i.bill_no = b.bill_no
          JOIN employee e ON e.emp_no = b.submitter_no
         WHERE b.instance_id = %s AND i.inv_type = '铁路电子客票'
           AND e.rank_level IN ('部门负责人','员工','其他员工')""", (IID,))
    assert n_clear == 12, n_clear
    for rid in ("TR-差旅-02", "TR-差旅-17"):
        assert rf.MANUAL_REVIEW[rid]["scope"].startswith("12/29"), rid
    # 这 12 张单的火车票必须全是二等座——否则「判了不会误报」那句话不成立
    seats = {r["detail_text"] for r in db.query("""
        SELECT i.detail_text FROM expense_bill b
          JOIN invoice i ON i.bill_no = b.bill_no
          JOIN employee e ON e.emp_no = b.submitter_no
         WHERE b.instance_id = %s AND i.inv_type = '铁路电子客票'
           AND e.rank_level IN ('部门负责人','员工','其他员工')""", (IID,))}
    assert all("二等座" in (s or "") for s in seats), (
        "有非领导坐了二等座以外的席别，TR-差旅-02 的「全部合规」结论要重写")
    # 有审批单且有市内交通费明细 / 伙食补助费明细的单据数
    n_city = db.scalar("""
        SELECT count(DISTINCT b.bill_no) FROM expense_bill b
          JOIN trip_application t ON t.bill_no = b.bill_no
          JOIN expense_detail d ON d.bill_no = b.bill_no
         WHERE b.instance_id = %s AND d.expense_item LIKE '市内交通%%'""", (IID,))
    n_meal = db.scalar("""
        SELECT count(DISTINCT b.bill_no) FROM expense_bill b
          JOIN trip_application t ON t.bill_no = b.bill_no
          JOIN expense_detail d ON d.bill_no = b.bill_no
         WHERE b.instance_id = %s AND d.expense_item = '伙食补助费'""", (IID,))
    # 2026-11-01：读图批给扫描/JPG 审批单补了出差日期，
    # 「有审批单」的单据变多了，这两条规则的适用面跟着变大。
    # **数字变了就改数字，不许把断言删掉**——这一条守的是
    # 「复核结论里的范围要对得上库里数得出来的数」。
    # 第四轮重读之后 12→11、18→17：FJPG-BX2603240507 那份审批单
    # 第二次读的把握降到「低」，整行不回写，于是它不再「有审批单」。
    assert (n_city, n_meal) == (11, 17), (n_city, n_meal)
    assert rf.MANUAL_REVIEW["TR-差旅-20"]["scope"].startswith("12/29")
    # TR-差旅-15 的适用面不按这里的 n_meal 算，按**有没有逐日日历**算
    # （它判的是「向接待单位交纳伙食费」，判据在 trip_calendar 上）。
    # 第四轮读图写了 21 行逐日，于是 14→18。两个数不相等是对的，
    # 别为了让它们相等去改其中一个。
    assert rf.MANUAL_REVIEW["TR-差旅-15"]["scope"].startswith("18/29")
    assert db.scalar("""SELECT count(DISTINCT t.bill_no) FROM trip_application t
                         JOIN trip_calendar c ON c.app_no = t.app_no
                        WHERE t.instance_id=%s""", (IID,)) == 18
