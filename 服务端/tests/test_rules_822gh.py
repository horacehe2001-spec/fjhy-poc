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
from app import rule_feasibility as rf  # noqa: E402
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
    assert len(rs) == len(impl) == 11, (len(rs), len(impl))
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
        for rid in ("TR-差旅-02", "TR-差旅-15", "TR-差旅-20",
                    "TR-差旅-28", "TR-差旅-30"):
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


# ── 先审批后接待（TR-接待-01）───────────────────────────────────────────
def test_先审批后接待按日期比_三分钟的差不许判成违规():
    """FJPG-BX2606240151 接待时间 17:00、最后审批 17:03:45。

    按时间戳比会命中「先接待后审批」，**而人工对这张单驳的是入账期间和
    清单场所，没有驳审批滞后**。审批单上的「接待时间」是计划时间，
    3 分钟是表单填报噪声。按日期比，2 份审批单都通过，和人工 2/2 一致。

    这条用例把口径钉死：哪天有人改成按时间戳比，这里会红。
    """
    _, res = _run_all()
    f = res["FJPG-BX2606240151"]["TR-接待-01"]
    assert f.outcome == "pass", (f.outcome, f.message)
    d = f.evidence["逐份明细"][0]
    # 时间戳上审批确实晚于接待——前提没变，用例才有意义
    assert d["最后审批时间"] > d["接待时间"], d
    # 而日期相同
    assert d["审批日期"] == d["接待日期"] == "2026-06-23", d
    human = db.scalar("""SELECT return_reason FROM manual_review
                          WHERE bill_no='FJPG-BX2606240151'""")
    assert "滞后" not in human, "人工驳回原话变了：现在提到滞后了，口径要重新核"


def test_人工驳过审批滞后的招待单要命中_而差旅单那张不归这条管():
    """人工为审批滞后驳过两张，**而这两张不是同一回事**：

      FJPG-BX2607162084  业务招待费报账单
        「业务审批清单，审批流程滞后」→ 说的是**业务接待审批单**，
        由 TR-接待-01 判。2026-11-01 读图批把那张 JPG 读出来了
        （是 OA 打印件，流转记录完整）：接待 07-15、最后审批 07-16
        → **命中**，和人工原话对上。

      FJPG-BX2604160034  差旅费报账单
        「审批滞后，请在补充说明栏备注原因」→ 说的是**出差审批**，
        不是接待审批。TR-接待-01 对它返回「不适用」是对的。

    **写这条用例时我先把两张都当成 TR-接待-01 的 pending，错了**——
    差旅单上的审批滞后是另一条规则的事。把区别钉在这里，
    免得以后有人为了「让这两张都被判到」去放宽 TR-接待-01 的适用范围。
    """
    _, res = _run_all()
    # 招待单：命中，而且命中的理由必须是日期先后，不是「缺数据」
    f = res["FJPG-BX2607162084"]["TR-接待-01"]
    assert f.outcome == "hit", (f.outcome, f.message)
    d = [x for x in f.evidence["逐份明细"] if x["审批单"] == "ATT-6558"][0]
    assert d["接待日期"] == "2026-07-15" and d["审批日期"] == "2026-07-16", d
    assert d["来源"] == "审批单读图", d
    assert "滞后" in db.scalar("""SELECT return_reason FROM manual_review
                                   WHERE bill_no='FJPG-BX2607162084'""")
    # 差旅单：不适用，而且确实是差旅单
    f2 = res["FJPG-BX2604160034"]["TR-接待-01"]
    assert (f2.evidence or {}).get("not_applicable"), (f2.outcome, f2.message)
    assert db.scalar("""SELECT doc_type FROM expense_bill
                         WHERE bill_no='FJPG-BX2604160034'""") == "差旅费报账单"


def test_手写审批单上没有审批日期_这不是读图读不清():
    """26 份 JPG 已经全部读过了，TR-接待-01 仍有 5 张 pending。
    **pending 的原因不是「还没读图」，是票面上就没有这个信息**——
    手写纸质业务接待审批单的经办人/承办部门/分管领导/主管领导
    只有签名、没有日期。两种说法在报告上差很远：
    前者是「我方还没做」，后者是「要客户从 OA 导流水」。

    实测 FJPG-BX2603110060：读图读出了接待时间/场所/人数，
    四个审批节点日期全是「无」——不是「不确定」。
    """
    _, res = _run_all()
    f = res["FJPG-BX2603110060"]["TR-接待-01"]
    assert f.outcome == "pending", (f.outcome, f.message)
    d = f.evidence["逐份明细"][0]
    assert d["接待日期"] == "2026-02-06", d          # 接待时间读出来了
    assert d["审批日期"] is None, d                   # 审批日期票面上没有
    assert d["最后审批时间"] == "None", d             # 本单该流程一条流水都没有
    # 这张图确实读过，而且模型答的是「无」不是「不确定」
    content = db.scalar("""SELECT e.content FROM attachment_extract e
                             JOIN attachment a ON a.id = e.attachment_id
                            WHERE a.bill_no='FJPG-BX2603110060'
                              AND a.type_code='A16' ORDER BY e.id LIMIT 1""")
    assert content and "分管领导审批日期: 无" in content, content


def test_接待对象名单不进fact():
    """guest_roster 是外部人员姓名与职务黏连的自由文本，脱敏闭集合切不出来。
    入库但**不许进 fact**（口径同 entertain_detail.guest_list）。"""
    fact, _ = reader.snapshot_fact("FJPG-BX2604260325")
    apprs = fact.get("entertain_approvals") or []
    assert apprs, "这张单没有已解析的接待审批单，用例前提变了"
    for a in apprs:
        assert "guest_roster" not in a, a
        assert not any("roster" in k or "name" in k for k in a), list(a)
    # 库里确实有值——不是因为没装进去才取不到
    assert db.scalar("""SELECT count(*) FROM entertain_approval
                         WHERE bill_no='FJPG-BX2604260325'
                           AND guest_roster IS NOT NULL""") == 1


# ── 地点与日期一致性（TR-差旅-28 / TR-差旅-30）──────────────────────────
def test_通知上的活动起止被用上了():
    """FJPG-BX2608311795 的培训通知**有文字层**，正文自报
    「定于8 月22 日至28 日……举办」——解出 2026-08-22~2026-08-28，
    和人工原话「按照培训通知体现出差时间应为8.22-8.28」逐字一致。

    这是 TR-差旅-30 缺了很久的第三方。审批单申报 08-22~08-28 覆盖住了，
    所以这一项通过；**人工驳的「仅体现8.22、8.28两天」说的是逐日表只填了
    两天，那件事由 TR-通用-02 判（已命中）** —— 两条规则判的不是一回事，
    别混。
    """
    _, res = _run_all()
    f = res["FJPG-BX2608311795"]["TR-差旅-30"]
    assert f.evidence["通知上的活动起止"] == ["2026-08-22~2026-08-28"], \
        f.evidence["通知上的活动起止"]
    assert "比了" in f.evidence["第三方比没比"], f.evidence["第三方比没比"]
    assert f.outcome == "pass", (f.outcome, f.message)
    # 同一张单上 TR-通用-02 必须还是命中（逐日表缺 5 天）
    assert res["FJPG-BX2608311795"]["TR-通用-02"].outcome == "hit"


def test_没有通知的那些单要明说第三方没比():
    """8 份通知读完之后，仍然有单据一份通知都没有。
    那几张单的结论是「两方对得上」，**不许读成三方都核过了**。

    2026-11-01 换了例子：原来举的 FJPG-BX2606050495 的通知是扫描件，
    读图之后它有通知了（05-24~05-28）。换一张**本来就没挂通知**的单——
    这条用例守的是「没比过就要说没比」，不是守某一张单。
    """
    _, res = _run_all()
    cand = [b for b, m in res.items()
            if "TR-差旅-30" in m
            and m["TR-差旅-30"].evidence.get("第三方比没比", "").startswith("**没比**")]
    assert cand, "所有单据都有通知了？那这条用例的前提没了，要重写"
    f = res[cand[0]]["TR-差旅-30"]
    assert f.evidence["通知上的活动起止"] == "取不到"
    # 有通知的那几张，必须说「比了」——两个方向都要钉
    yes = [b for b, m in res.items()
           if "TR-差旅-30" in m
           and m["TR-差旅-30"].evidence.get("第三方比没比", "").startswith("比了")]
    assert len(yes) >= 5, yes
    assert db.scalar("""SELECT count(*) FROM meeting_notice
                         WHERE instance_id=%s AND notice_start IS NOT NULL""",
                     (IID,)) == 6



def test_地点一致性只拿住宿票比_代订机票不许参与():
    """**这组用例守的是三条实测出来的误报。**

    拿全部发票比地点，这三张单会被误报——销售方是旅行社，
    名字前缀是旅行社的注册地，不是出差地：
        FJPG-BX2607162026  申报 遵义市  → 上海华程西南国际旅行社有限公司
        FJPG-BX2608311795  申报 珠海    → 天津航路无界国际旅行社有限公司
        FJPG-BX2607280255  申报 海南…   → 成都携程旅行社有限公司广州分公司
    窄到住宿类之后 0 条。误报和漏报同样致命：对一张正常单据报
    「出差地点和票据对不上」，审核岗核对一次就不再看这条规则了。
    """
    _, res = _run_all()
    for bn in ("FJPG-BX2607162026", "FJPG-BX2608311795", "FJPG-BX2607280255"):
        f = res[bn]["TR-差旅-28"]
        assert f.outcome != "hit", (bn, f.message)
    # 这三张单里确实有旅行社开的票——前提没变，用例才有意义
    n = db.scalar("""SELECT count(*) FROM invoice
                      WHERE instance_id=%s AND issuer LIKE %s""",
                  (IID, "%旅行社%"))
    assert n >= 3, n


def test_地名比对不许依赖城市白名单():
    """第一版用城市正则，**漏了「湛江」**——漏掉不报错，
    只是「湛江市希朋酒店管理有限公司」对不上申报地「海南湛江」，
    报一条误报。闭集合的地名表永远补不全（县、区、开发区、口岸）。

    这里直接验 2 字片段的比法，不经过引擎。
    """
    from app.rules.impl_822gh import _grams
    for place, issuer in (("海南湛江", "湛江市希朋酒店管理有限公司"),
                          ("莆田市秀屿区", "莆田市必盛酒店管理有限公司秀屿分公司"),
                          ("上海", "上海交通大学"),
                          ("厦门", "厦门嘉美伦酒店管理有限公司"),
                          ("南平", "南平雅阁国际大酒店有限公司")):
        assert _grams(place) & _grams(issuer), (place, issuer)
    # 反方向：真不一致的要判出来
    for place, issuer in (("遵义市", "上海华程西南国际旅行社有限公司"),
                          ("珠海", "天津航路无界国际旅行社有限公司")):
        assert not (_grams(place) & _grams(issuer)), (place, issuer)


def test_日期匹配用乘车日期不用开票日期():
    """开票日期**本来就在行程之后**：FJPG-BX2606050495 的行程是
    2026-05-24~26，而住宿发票的开票日期是 2026-06-08。
    拿开票日期比出差区间，这张单会被判成「票据日期不在出差期间」——
    而它完全正常。
    """
    f = _run_all()[1]["FJPG-BX2606050495"]["TR-差旅-30"]
    # 2026-11-01：这张单现在是 pending，但**不是因为乘车日期**——
    # 读图补上通知之后，申报区间 05-24~05-26 与通知 05-24~05-28 不一致，
    # 而那一项的口径客户还没定，所以规则不下结论（见下一条用例）。
    # 这条用例守的仍然是「乘车日期全部落在区间内」这件事本身。
    assert f.outcome == "pending", (f.outcome, f.message)
    assert "乘车日期都在申报区间内" in f.message, f.message
    # 前提：这张单确实有一张开票日期晚于出差结束日的发票
    n = db.scalar("""SELECT count(*) FROM invoice i
                       JOIN trip_application t ON t.bill_no = i.bill_no
                      WHERE i.bill_no='FJPG-BX2606050495'
                        AND i.issue_date > t.end_date""")
    assert n >= 1, "没有开票日期晚于出差结束日的发票了，这条用例的前提变了"
    # 取到的是乘车日期，而且落在区间内
    assert f.evidence["票面乘车日期"] == ["2026-05-24", "2026-05-26"], \
        f.evidence["票面乘车日期"]
    # 假如改成按开票日期比，这张单会被判成命中——钉住这个反向事实
    assert all(d not in ("2026-06-08",) for d in f.evidence["票面乘车日期"])


def test_申报区间与通知对不上时不许自己定口径():
    """**这条是为了防我自己犯过的错而写的。**

    批次 C 里只有 1 份通知，我把口径定成「申报区间必须覆盖通知区间」，
    那一份正好覆盖，0 命中，看不出问题。2026-11-01 读图补到 6 份通知后，
    同一期干部培训班的三个人里：
        FJPG-BX2606011481 申报 05-23~05-28（早一天赶路）→ 覆盖
        FJPG-BX2606050568 申报 05-24~05-28            → 覆盖
        FJPG-BX2606050495 申报 05-24~05-26（提前回程） → 不覆盖 → 命中
    **而人工对 2606050495 驳的是「补充5.25日用餐与用车情况」，没驳这件事。**
    提前回程和提前出发都有正常解释，客户没说过哪个方向算违规。

    口径未定就不许命中：在 6 个样本、0 个人工标注上自己定一个方向，
    产出的就是误报。哪天有人把它改回 hit，这里会红。
    """
    _, res = _run_all()
    f = res["FJPG-BX2606050495"]["TR-差旅-30"]
    assert f.outcome == "pending", (f.outcome, f.message)
    assert "口径" in f.message, f.message
    assert f.evidence["申报区间与通知区间不一致"] == ["通知 2026-05-24~2026-05-28"]
    # 覆盖住的那两张不许被连坐
    for bn in ("FJPG-BX2606011481", "FJPG-BX2606050568"):
        g = res[bn]["TR-差旅-30"]
        assert "申报区间与通知区间不一致" not in g.evidence, (bn, g.evidence)
    # 人工原话里确实没提这件事——前提没变，用例才有意义
    human = db.scalar("""SELECT return_reason FROM manual_review
                          WHERE bill_no='FJPG-BX2606050495'""")
    assert "5.25" in human and "通知" not in human, human


def test_不带年份的日期一律不认():
    """票面上还有「13:35开」这种时间和「06车09F号」这种车厢座位。
    不带年份的「07月22日」如果也认，跨年行程会把年份猜错——
    猜错一年比取不到更坏，它会让这条规则对正常单据大面积误报。"""
    from app.rules import predicates as P
    assert P.cn_dates("2026年07月22日 13:35开 06车09F号 二等座") == [(2026, 7, 22)]
    assert P.cn_dates("07月22日 13:35开 06车09F号") == []
    assert P.cn_dates("*生产生活服务*住宿服务 1 398.11 6% 23.89") == []
    assert P.cn_dates("2026年13月01日") == []          # 月份越界的不认


def test_这两条判不了人工驳的那七条_要在结论里说清():
    """**0 命中要说清是「判了没事」还是「根本判不了那件事」。**

    TR-差旅-28 判不了人工那 5 条逐日地点：驳的是客户 ERP 的日历区，
      而 trip_calendar.place 本实例 90 行全空（审批单 PDF 没有地点列）。
    TR-差旅-30 的第三方 2026-11-01 读图补上了（6 份通知有活动起止），
      **但补上之后反而暴露出口径未定**：申报区间与通知区间不一致时
      算不算违规，客户没说过。所以这一项改成「算差异、报差异、
      不下结论」，而这件事同样必须写在复核结论里。
    否则「通过 9 / 通过 11」会被读成「地点和日期都核过了」。
    """
    assert db.scalar("""SELECT count(*) FROM trip_calendar
                         WHERE instance_id=%s AND place IS NOT NULL""",
                     (IID,)) == 0, "逐日地点有值了，这条用例的前提变了"
    w28 = rf.MANUAL_REVIEW["TR-差旅-28"]["why"]
    w30 = rf.MANUAL_REVIEW["TR-差旅-30"]["why"]
    assert "判不了那 5 条" in w28, "没说清 TR-差旅-28 判不了人工那 5 条逐日地点"
    assert "命中 0" in w28 and "命中 0" in w30
    # 第三方补上了，所以结论里不许再写「通知在库外」；要写的是口径未定
    assert "要客户定的口径" in w30, "没说清 TR-差旅-30 现在卡在口径上"
    assert "0 个人工标注" in w30, "没说清这个口径没有标注样本支撑"


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


def test_逐日表填满的十三张不许误报():
    """误报和漏报同样致命。这 13 张的用餐/用车逐日表覆盖了出差区间的每一天，
    对它们报「审批单要素不全」，审核岗核对一次就不再看这条规则的结论了。

    10 → 13 是 2026-11-01 第四轮读图补的（提示词加了逐日表与签名日期）：
    2604070969、2604160034、2606101698 三张的审批单是扫描件/手写勾选表，
    读出来之后逐日表**是填满的**，所以它们从 pending 转成通过，不是转成命中。
    """
    _, res = _run_all()
    passed = [bn for bn, m in res.items()
              if m["TR-通用-02"].outcome == "pass"
              and not (m["TR-通用-02"].evidence or {}).get("not_applicable")]
    assert len(passed) == 13, sorted(passed)
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


def test_没抽过逐日表的要pending_不许当成没填():
    """**这条用例守的是我自己造过的一个误报。**

    2026-11-01 第三轮读图给 8 份扫描/JPG 审批单写了 trip_application 行，
    而那一轮的提示词只问了出差起止日期和出差地点三个字段——
    **逐日用餐/用车表没问、出差人签名日期也没问**。
    规则第一版看到「逐日表一行都没有」就报「缺 N 天」、
    看到 backfill_date 为空就报「没有签名回填日期」，
    当场把这 6 张从 pending 变成命中（4→10）。

    **「没读」不是「没填」。** 这两件事在报告上长得一模一样，
    而把前者说成后者，是审核系统最该避免的那种错——
    对一张可能完全合规的单据，报出一条言之凿凿的缺陷。

    第四轮补了提示词之后，pending 从 6 降到 2：
      · FJPG-BX2608272347 两份审批单整页年份被读成 2016（实为 2026），
        表头日期和逐日行都被年份闸挡下，detail_read 留在 FALSE
      · FJPG-BX2603240507 重读后模型把握降到「低」，整行不回写，
        退回「本单没有已解析的出差审批单」
    **闸按 detail_read 判，不按 extracted_from 判**——
    按来源判的话，这一轮读到了的那 5 份也会被判成 pending（漏判）。
    """
    _, res = _run_all()
    pend = [bn for bn, m in res.items()
            if m["TR-通用-02"].outcome == "pending"]
    assert sorted(pend) == ["FJPG-BX2603240507", "FJPG-BX2608272347"], sorted(pend)
    for bn in pend:
        apps = db.query("""SELECT app_no, extracted_from, detail_read
                            FROM trip_application WHERE bill_no=%s""", (bn,))
        # 要么一份审批单都没有，要么有但这三块没抽过——两种都不许当成「没填」
        assert not apps or any(not a["detail_read"] for a in apps), (bn, apps)
        why = res[bn]["TR-通用-02"].message
        assert ("没有读到" in why or "尚未读图" in why), (bn, why)
    # 命中 5 张，一张都不许多——多出来的就是这类误报
    hits = [bn for bn, m in res.items() if m["TR-通用-02"].outcome == "hit"]
    assert len(hits) == 5, sorted(hits)


def test_读图读出来的逐日表也能命中_而且是我亲眼核过的那一天():
    """第四轮补提示词之后拿到的**新阳性样本**，不是换个说法的旧结论。

    FJPG-BX2603270004 的 雒朔出差审批单.jpg 是 OA 页面截图：
    表头「出差起止时间 2026-03-25 至 2026-03-26」，而「差旅用餐情况」
    和「差旅用车情况」两张小表**各只有 2026-03-26 一行**。
    这一条是打开原图逐格看过的（不是推断），所以它可以当锚点钉住。

    人工对这张单驳的是另一件事（「出差审批未体现出差人回填情况（签字及日期）」），
    而票面上签名日期现在是有的（雒朔 2026-03-25）——
    客户给的 29 张是**已整改**的快照，驳回原因描述的是驳回当时。
    所以这一条要说清：**它和人工驳回同一张单，但不是同一个理由。**
    """
    _, res = _run_all()
    f = res["FJPG-BX2603270004"]["TR-通用-02"]
    assert f.outcome == "hit", (f.outcome, f.message)
    mine = [a for a in f.evidence["逐份明细"] if a["审批单"] == "ATT-6496"]
    assert len(mine) == 1, f.evidence["逐份明细"]
    assert mine[0]["缺的日期"] == ["2026-03-25"], mine[0]
    assert mine[0]["出差起止"] == "2026-03-25~2026-03-26", mine[0]
    # 签名日期票面上有，所以**不许**同时报「没有签名回填日期」
    assert mine[0]["出差人签名日期"] == "2026-03-25", mine[0]
    assert "签名" not in f.message, f.message
    # 这一行确实是读图写的，不是文字层
    src = db.query_one("""SELECT extracted_from, detail_read
                            FROM trip_application WHERE app_no='ATT-6496'""")
    assert src["extracted_from"] == "审批单读图" and src["detail_read"], dict(src)


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
    # 只看**文字层**解出来的那 15 份：读图那 8 份的提示词没问这两个日期，
    # 它们两列都是空的，混进来会把这条用例变成「断言空值不相等」。
    rows = db.query("""SELECT bill_no, report_date, backfill_date, end_date
                         FROM trip_application
                        WHERE instance_id=%s AND extracted_from=%s""",
                    (IID, "审批单文字层"))
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
