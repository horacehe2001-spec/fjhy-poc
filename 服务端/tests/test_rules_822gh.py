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
    assert len(rs) == len(impl) == 7, (len(rs), len(impl))
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
