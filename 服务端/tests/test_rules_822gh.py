# -*- coding: utf-8 -*-
"""822港航 的 7 条规则实现 + 与 29 条人工驳回意见的比对。

这组用例守三件事：

1. **规则只在该适用的单据上说话。** 接待类规则不许对差旅单下结论，
   反之亦然——「不适用」和「通过」在报告上是两码事，混了会让覆盖面
   看起来比实际大。

2. **有标注的案例不许退化。** 客户给了 29 条真实人工驳回意见，
   其中 FJPG-BX2609201921 驳的原话是「简餐请按照接待日期，在招待明细区
   分开进行填列」——TR-接待-22 判的就是这件事。
   第一版实现**漏报了它**（只查「发生日期为空」）。
   改完之后这条用例钉住它：哪天又漏了，这里会红。

3. **明细不许重复计数。** 分摊区源文件里每行重复一遍，不去重的话
   市内交通费 160 会被算成 320，而 2 天的包干上限是 160——
   规则会报「超标一倍」，**而单据完全合规**。第一次跑引擎就这样误报了 4 条。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.perception import reader  # noqa: E402
from app.rules import engine  # noqa: E402
from app.rules import impl_822gh  # noqa: E402,F401  导入即注册
from app.rules import predicates as P  # noqa: E402

IID = "822gh"


def _run_all():
    rs = engine.load_ruleset(instance_id=IID)
    std, tm = reader.standards(IID), reader.tier_map(IID)
    out = {}
    for r in db.query("""SELECT bill_no FROM expense_bill WHERE instance_id=%s
                          ORDER BY bill_no""", (IID,)):
        fact, _ = reader.snapshot_fact(r["bill_no"])
        findings, _s = engine.evaluate(rs, fact, std, tm)
        out[r["bill_no"]] = {f.rule_id: f for f in findings}
    return rs, out


# ── 装载与绑定 ──────────────────────────────────────────────────────────
def test_可执行规则数等于有实现的规则数():
    """demo_executable 是从注册表推出来的，不手工维护名单。
    手工名单迟早和实现对不上，而对不上的表现之一是
    「写了实现但从来没跑过」——它什么都不报。"""
    rs = engine.load_ruleset(instance_id=IID)
    have = {r["rule_id"] for r in db.query(
        "SELECT rule_id FROM rule WHERE instance_id=%s AND version=1", (IID,))}
    impl = set(engine.registered()) & have
    assert len(rs) == len(impl) == 8, (len(rs), len(impl))
    assert not rs.missing_impl, rs.missing_impl


def test_每条可执行规则都绑了本实例的流程节点():
    rows = db.query("""SELECT rule_id, binding_node FROM rule
                        WHERE instance_id=%s AND version=1 AND demo_executable""",
                    (IID,))
    assert rows
    nodes = {r["node_id"] for r in db.query(
        """SELECT node_id FROM process_node WHERE instance_id=%s
            AND version='0.1'""", (IID,))}
    for r in rows:
        assert r["binding_node"] in nodes, (r["rule_id"], r["binding_node"])


def test_别家的规则没被动():
    assert db.scalar("""SELECT count(*) FROM rule WHERE instance_id='fjhy'
                         AND version=1 AND demo_executable""") == 28


# ── 适用范围 ────────────────────────────────────────────────────────────
def test_接待规则不对差旅单下结论():
    """「不适用」和「通过」在报告上是两码事。"""
    _, res = _run_all()
    travel = [b["bill_no"] for b in db.query(
        """SELECT bill_no FROM expense_bill WHERE instance_id=%s
            AND doc_type='差旅费报账单'""", (IID,))]
    for bn in travel:
        for rid in ("TR-接待-04", "TR-接待-10", "TR-接待-13", "TR-接待-22"):
            f = res[bn][rid]
            assert (f.evidence or {}).get("not_applicable"), (
                "%s 对差旅单 %s 下了结论：%s" % (rid, bn, f.message))


def test_差旅规则不对招待单下结论():
    _, res = _run_all()
    ent = [b["bill_no"] for b in db.query(
        """SELECT bill_no FROM expense_bill WHERE instance_id=%s
            AND doc_type='业务招待费报账单'""", (IID,))]
    for bn in ent:
        for rid in ("TR-差旅-02", "TR-差旅-15", "TR-差旅-20"):
            f = res[bn][rid]
            assert (f.evidence or {}).get("not_applicable"), (
                "%s 对招待单 %s 下了结论：%s" % (rid, bn, f.message))


# ── 有标注的案例 ────────────────────────────────────────────────────────
def test_整月合并必须命中_这是人工驳过的案例():
    """FJPG-BX2609201921：事由「8月份工作餐」、招待明细只有 1 行。
    人工驳回原话：「简餐请按照接待日期，在招待明细区分开进行填列」。
    **第一版实现漏报了它。**"""
    _, res = _run_all()
    f = res["FJPG-BX2609201921"]["TR-接待-22"]
    assert f.outcome == "hit", (f.outcome, f.message)
    assert "8月份" in f.message and "1 行" in f.message, f.message
    human = db.scalar("""SELECT return_reason FROM manual_review
                          WHERE bill_no='FJPG-BX2609201921'""")
    assert "分开进行填列" in human, "人工驳回的原话变了，这条用例的锚点要重找"


def test_已经按日期拆开的不许误报():
    """FJPG-BX2603230510：同样是「二月份工作餐」，但招待明细**已经拆成 9 行**，
    人工在这一条上没有驳回。误报和漏报同样致命——
    对着一张已经填对的单报违规，审核岗很快就不再看 AI 的结论了。"""
    _, res = _run_all()
    f = res["FJPG-BX2603230510"]["TR-接待-22"]
    assert f.outcome == "pass", (f.outcome, f.message)
    n = db.scalar("""SELECT count(*) FROM entertain_detail
                      WHERE bill_no='FJPG-BX2603230510'""")
    assert n == 9, n


def test_月份表述的识别正反都要准():
    """「8月份工作餐」是一个月，「8月6日工作餐」是一次——差一个字，
    业务含义相反。认错了每张写了具体日期的单都会被当成整月合并。"""
    for t, want in (("8月份工作餐", "8月份"), ("二月份工作餐", "二月份"),
                    ("5月工作餐", "5月")):
        assert P.month_phrase(t) == want, t
    for t in ("8月6日工作餐", "2026年8月6日接待", "蒯崟莆田差旅报销",
              "业务招待费", "参加集团安全隐患大排查"):
        assert P.month_phrase(t) is None, t


# ── 重复计数 ────────────────────────────────────────────────────────────
def test_费用明细不许重复计数():
    """分摊区源文件里每行重复一遍。判据不是「看着像重复」，
    是**去重后明细合计正好等于报销金额**：29/29 张对得上，
    不去重只有 1/29。"""
    bad = db.query("""SELECT b.bill_no, b.amount, sum(d.amount) s
                        FROM expense_bill b JOIN expense_detail d
                          ON d.bill_no = b.bill_no
                       WHERE b.instance_id = %s
                       GROUP BY 1, 2
                      HAVING abs(sum(d.amount) - b.amount) >= 0.01""", (IID,))
    assert not bad, [(r["bill_no"], float(r["amount"]), float(r["s"])) for r in bad]


def test_市内交通费那四张单不许再误报():
    """这四张单在去重之前被 TR-差旅-20 报成「超标一倍」，而它们完全合规。"""
    _, res = _run_all()
    for bn in ("FJPG-BX2606050495", "FJPG-BX2607240109",
               "FJPG-BX2607280255", "FJPG-BX2607310058"):
        f = res[bn]["TR-差旅-20"]
        assert f.outcome != "hit", (bn, f.message)


# ── 审批单关键要素完整性（TR-通用-02）───────────────────────────────────
# 这条规则原来被标成「0/29 张（判的就是图片内容）」。那个结论是错的：
# 15 份出差审批单是 PDF 且有文字层，表尾两张小表和签名日期都能确定性解析。
# **而且上一次复核的 why 里已经写着「用车用餐都取到了」——
# 证据对、结论错。** 下面这组用例把实跑结果钉住，别再退回去。
def test_逐日表缺哪一天要逐字对上人工驳回():
    """这是两个有人工原话的标注案例，算出来的日期必须和人工说的是同一天。

    FJPG-BX2606050495 人工：「出差审批单需补充**5.25日**用餐与用车情况」
    FJPG-BX2608311795 人工：「出差时间应为8.22-8.28，审批单**仅体现8.22、8.28**」

    判的是日期本身，不是「有没有缺」——少一天多一天都会让审核岗
    拿着报告找不到对应的格子。
    """
    _, res = _run_all()
    f = res["FJPG-BX2606050495"]["TR-通用-02"]
    assert f.outcome == "hit", (f.outcome, f.message)
    gaps = [d for a in f.evidence["逐份明细"] for d in a["缺的日期"]]
    assert gaps == ["2026-05-25"], gaps
    human = db.scalar("""SELECT return_reason FROM manual_review
                          WHERE bill_no='FJPG-BX2606050495'""")
    assert "5.25" in human, "人工驳回原话变了，这条用例的锚点要重找"

    f2 = res["FJPG-BX2608311795"]["TR-通用-02"]
    assert f2.outcome == "hit", (f2.outcome, f2.message)
    gaps2 = [d for a in f2.evidence["逐份明细"] for d in a["缺的日期"]]
    assert gaps2 == ["2026-08-23", "2026-08-24", "2026-08-25",
                     "2026-08-26", "2026-08-27"], gaps2
    human2 = db.scalar("""SELECT return_reason FROM manual_review
                           WHERE bill_no='FJPG-BX2608311795'""")
    assert "8.22" in human2 and "8.28" in human2, human2


def test_逐日表填满的十张不许误报():
    """误报和漏报同样致命。这 10 张的用餐/用车逐日表覆盖了出差区间的每一天，
    对它们报「审批单要素不全」，审核岗核对一次就不再看这条规则的结论了。"""
    _, res = _run_all()
    passed = [bn for bn, m in res.items()
              if m["TR-通用-02"].outcome == "pass"
              and not (m["TR-通用-02"].evidence or {}).get("not_applicable")]
    assert len(passed) == 10, sorted(passed)
    for bn in passed:
        ev = res[bn]["TR-通用-02"].evidence
        for a in ev["逐份明细"]:
            assert not a["缺的日期"], (bn, a)
            assert a["出差人签名日期"], (bn, a)


def test_招待单是不适用不是通过():
    """招待类单据要查的是业务接待审批单（A16），不是出差审批单。
    对它们返回 pass 会让覆盖面看起来比实际大 9 张。"""
    _, res = _run_all()
    ent = [b["bill_no"] for b in db.query(
        """SELECT bill_no FROM expense_bill WHERE instance_id=%s
            AND doc_type='业务招待费报账单'""", (IID,))]
    assert len(ent) == 9, len(ent)
    for bn in ent:
        f = res[bn]["TR-通用-02"]
        assert (f.evidence or {}).get("not_applicable"), (bn, f.message)


def test_审批单是扫描件的六张要pending():
    """附件里的审批单是 JPG 或非标准格式，文字层取不到——
    这六张的真实状态是「还没判」，不是「合规」。

    **人工为「未体现出差人回填情况（签字及日期）」驳过的两张单就在这里面。**
    所以「签名回填」那一半的真实状态是「有能力判、本批判不到阳性样本」，
    报告上不能写成「本批全部有回填」。
    """
    _, res = _run_all()
    pend = [bn for bn, m in res.items()
            if m["TR-通用-02"].outcome == "pending"]
    assert len(pend) == 6, sorted(pend)
    assert "FJPG-BX2603270004" in pend, "人工驳「未体现回填」的那张不在 pending 里了"
    for bn in pend:
        assert db.scalar("""SELECT count(*) FROM trip_application
                             WHERE bill_no=%s""", (bn,)) == 0, bn


def test_一张单挂两份审批单要逐份判():
    """FJPG-BX2607280255 挂了两份审批单（07-23~24 与 07-25~26）。
    reader 原来是 LIMIT 1，只取第一份——另一份的缺漏会被整段放过去。"""
    _, res = _run_all()
    ev = res["FJPG-BX2607280255"]["TR-通用-02"].evidence
    assert ev["审批单份数"] == 2, ev["审批单份数"]
    assert len(ev["逐份明细"]) == 2
    assert {a["出差起止"] for a in ev["逐份明细"]} == {
        "2026-07-23~2026-07-24", "2026-07-25~2026-07-26"}


def test_回填日期装的是签名日期不是填报日期():
    """migration 0056 订正的那件事。两个日期**全部 15 份都不相同**，
    所以哪天又把填报日期塞回 backfill_date，这里会红。

    为什么这条要单独钉：拿填报日期判「有没有回填」，15 份全有值、
    一条都判不出来，而报告上显示的是「全部通过」——
    判错字段比闸没跑更难发现。
    """
    rows = db.query("""SELECT bill_no, report_date, backfill_date, end_date
                         FROM trip_application WHERE instance_id=%s""", (IID,))
    assert len(rows) == 15, len(rows)
    for r in rows:
        assert r["report_date"] and r["backfill_date"], dict(r)
        assert r["report_date"] != r["backfill_date"], dict(r)
        # 填报在出差之前、签名在出差之后，这是两个日期的业务含义
        assert r["backfill_date"] >= r["end_date"], dict(r)
        assert r["report_date"] <= r["end_date"], dict(r)


def test_签名段里有流转记录时要取最后一个日期():
    """实测 FJPG-BX2606050495 的审批单签名段上面紧挨着一条流转记录
    「<姓名>-><姓名> 2026-05-29」，取第一个日期会取到流转那一行。
    这里直接验解析函数，不经过库。"""
    from app.demo.load_822gh_attachments import parse_trip
    lines = ["出差审批单",
             "填写人 某甲 出差地点 上海",
             "出差人 某乙 人数 1",
             "出差起止时间 2026-05-24 至 2026-05-26 出差天数 3",
             "经办人 某甲 部室负责人 某丙 分管领导 某丁 主管领导 某乙",
             "填报日期 审批日期",
             "2026-05-20 2026-05-21",
             "以下内容由出差人员如实申报",
             "差旅用餐情况",
             "时间 早 中 晚 备注",
             "2026-05-24 自行用餐（含免费） 自行用餐（含免费） 自行用餐（含免费）",
             "差旅用车情况",
             "时间 上午 下午",
             "2026-05-24 自行用车 自行用车",
             "附件",
             "某乙->某甲 2026-05-29",
             "出差人签名",
             "某乙 2026-06-02",
             "注：.参加会议、培训的，可在说明栏中备注，附上相关通知及领导批件。"]
    t = parse_trip(lines)
    assert t["backfill_date"] == "2026-06-02", t["backfill_date"]
    assert t["report_date"] == "2026-05-20", t["report_date"]


# ── 判不了就说判不了 ────────────────────────────────────────────────────
def test_判不了的一律pending不许算通过():
    """把 pending 当 pass，报告上就会出现「全部通过」，
    而实际上根本没跑。"""
    _, res = _run_all()
    # 没有已解析发票的招待单，烟酒/礼品那两条必须是 pending
    no_inv = [r["bill_no"] for r in db.query(
        """SELECT b.bill_no FROM expense_bill b
            WHERE b.instance_id=%s AND b.doc_type='业务招待费报账单'
              AND NOT EXISTS (SELECT 1 FROM invoice i WHERE i.bill_no=b.bill_no)""",
        (IID,))]
    assert no_inv, "所有招待单都有发票了，这条用例的前提变了"
    for bn in no_inv:
        for rid in ("TR-接待-10", "TR-接待-13"):
            f = res[bn][rid]
            assert f.outcome == "pending", (bn, rid, f.outcome, f.message)
            assert "尚未读图" in f.message or "没有已解析的发票" in f.message


def test_职级落不进档位的要pending不许猜():
    """等级表写「公司正职领导／其他人员」，员工表写「董事长／副总经理／…」，
    两套对不上。董事长/副总经理落在哪一档客户还没确认——**不猜**。"""
    _, res = _run_all()
    leaders = [r["bill_no"] for r in db.query(
        """SELECT b.bill_no FROM expense_bill b
             JOIN employee e ON e.emp_no = b.submitter_no
             JOIN invoice i ON i.bill_no = b.bill_no
            WHERE b.instance_id=%s AND i.inv_type='铁路电子客票'
              AND (e.rank_level IN ('董事长','副总经理') OR e.rank_level IS NULL)""",
        (IID,))]
    assert leaders, "没有领导报账人的火车票单据，这条用例验不了"
    for bn in set(leaders):
        f = res[bn]["TR-差旅-02"]
        assert f.outcome == "pending", (bn, f.outcome, f.message)
        assert "不猜" in f.message or "未确认" in f.message, f.message
