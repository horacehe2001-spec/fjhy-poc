# -*- coding: utf-8 -*-
"""检出矩阵：证明「抓得住」，而且「不误伤」。

为什么必须有这组测试：
  客户给的 40 张真实样本全部是人工放行的合规单据。在它们上面跑出
  「0 命中」只能证明没误拦，**证明不了抓得住**。
  要验证检出能力，只能自己造带已知缺陷的单据。

**最重要的一条是 test_执行集里每条规则都要有正例或声明为什么没有。**
  它防的是「规则看起来在跑，实际永远不会命中」——
  TR-D4-04 就是这么被抓出来的：
  天数由伙食补助倒算，人日均 = 总额/((总额/100)×人数) = 100/人数 ≤ 100，
  是个恒等式，穷举证明永远不超标。
  这种失效不报错、不抛异常，报告上看着一切正常，
  正是「确定性的闸失效而没人发现，比没有这道闸更危险」的实例。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.demo import generator  # noqa: E402
from app.perception import reader  # noqa: E402
from app.rules import engine, impl  # noqa: E402,F401


@pytest.fixture(scope="module")
def ctx():
    generator.generate(verbose=False)
    rs = engine.load_ruleset()
    return {"rs": rs, "std": reader.standards(), "tiers": reader.tier_map(),
            "exp": generator.expectations()}


def _run(ctx, bill_no):
    fact, _ = reader.snapshot_fact(bill_no)
    findings, stats = engine.evaluate(ctx["rs"], fact, ctx["std"], ctx["tiers"])
    return {
        "hits": {f.rule_id for f in findings if f.outcome == "hit"},
        "pending": {f.rule_id for f in findings if f.outcome == "pending"},
        "conclusion": engine.conclude(findings, ctx["rs"]),
        "findings": findings,
    }


def _cases():
    return list(generator.expectations().items())


@pytest.mark.parametrize("bill_no,case", _cases(), ids=[c["id"] for _, c in _cases()])
def test_检出矩阵(ctx, bill_no, case):
    r = _run(ctx, bill_no)
    for rid in case["expect_hit"]:
        assert rid in r["hits"], (
            "%s（%s）期望命中 %s，实际命中 %s、待人工 %s"
            % (case["id"], case["label"], rid, sorted(r["hits"]), sorted(r["pending"])))
    for rid in case.get("expect_not_hit", []):
        assert rid not in r["hits"], (
            "%s（%s）不该命中 %s 却命中了——这是误报"
            % (case["id"], case["label"], rid))
    for rid in case["expect_pending"]:
        assert rid in r["pending"], (
            "%s（%s）期望 %s 转人工，实际命中 %s、待人工 %s。"
            "区间判定的中间段必须转人工，既不能判违规也不能放过"
            % (case["id"], case["label"], rid, sorted(r["hits"]), sorted(r["pending"])))


def test_干净单不得有任何命中(ctx):
    bad = []
    for bn, c in ctx["exp"].items():
        if c["expect_hit"] or c["expect_pending"]:
            continue
        r = _run(ctx, bn)
        if r["hits"] or r["pending"]:
            bad.append((c["id"], sorted(r["hits"]), sorted(r["pending"])))
    assert not bad, "干净单上出现了命中或待人工：%s" % bad


def test_执行集里每条规则都要有正例或声明为什么没有(ctx):
    """**这条是整组测试里最重要的。**

    一条规则在执行集里、却永远不可能命中，比它不在执行集里更坏——
    因为报告上看着它在工作。
    所以：执行集里的每条 detect 型规则，要么检出矩阵里有一个期望命中的用例，
    要么在规则上显式声明为什么造不出来（cannot_hit_without 或 rule_effect=exempt）。
    """
    covered = set()
    for c in generator.CASES:
        covered.update(c["expect_hit"])
    missing = []
    for r in ctx["rs"].rules:
        rid = r["rule_id"]
        if rid in covered:
            continue
        lg = r["logic"] or {}
        if lg.get("rule_effect") == "exempt":
            continue                      # 豁免型规则按设计不命中
        if (lg.get("degraded") or {}).get("cannot_hit_without"):
            continue                      # 已显式声明结构性无法命中
        missing.append((rid, r["name"]))
    assert not missing, (
        "这些规则在执行集里，但检出矩阵没有正例，也没声明原因：\n  %s\n"
        "要么补一个期望命中的演示用例，要么在 logic 里声明 "
        "cannot_hit_without / rule_effect=exempt。\n"
        "一条永远不会命中的规则，比它不在执行集里更坏——报告上看着它在工作。"
        % "\n  ".join("%s %s" % m for m in missing))


def test_声明了结构性无法命中的规则要说清退化成了什么(ctx):
    for r in ctx["rs"].rules:
        deg = (r["logic"] or {}).get("degraded") or {}
        if not deg.get("cannot_hit_without"):
            continue
        assert deg.get("cannot_hit_why"), "%s 声明了 cannot_hit_without 但没写原因" % r["rule_id"]
        assert deg.get("degenerates_to"), (
            "%s 没说清它退化成了什么。审核岗要知道这条规则现在实际在查什么，"
            "否则会以为标准控制已经覆盖了。" % r["rule_id"])


def test_演示数据一眼可辨(ctx):
    """全局纪律第 12 条：演示数据混进真实环境会造成困惑。"""
    rows = db.query("SELECT bill_no, is_demo, summary FROM expense_bill "
                    "WHERE bill_no LIKE %s", (generator.PREFIX + "%",))
    assert rows, "没有演示数据"
    for r in rows:
        assert r["is_demo"] is True, "%s 没标 is_demo" % r["bill_no"]
        assert "【演示数据】" in (r["summary"] or ""), \
            "%s 的摘要没有显著标注" % r["bill_no"]
    # 真实单据不许被误标
    n = db.scalar("SELECT count(*) FROM expense_bill WHERE is_demo "
                  "AND bill_no NOT LIKE %s", (generator.PREFIX + "%",))
    assert n == 0, "有 %d 张非演示前缀的单据被标成了演示数据" % n


def test_演示数据不影响真实单据的结论(ctx):
    """演示数据和真实数据共存时，真实单据的结论不能变。

    TR-D10-06 拆单检测会看「同主体 2 日内的其他单据」——
    如果演示数据和真实数据在同一个主体下，就会互相污染。
    演示主体用 DEMO-ORG-* 前缀正是为了隔开。
    """
    real_orgs = db.query("""SELECT DISTINCT org_id FROM expense_bill WHERE NOT is_demo""")
    demo_orgs = db.query("""SELECT DISTINCT org_id FROM expense_bill WHERE is_demo""")
    overlap = {r["org_id"] for r in real_orgs} & {r["org_id"] for r in demo_orgs}
    assert not overlap, (
        "演示数据与真实数据共用了核算主体 %s——"
        "TR-D10-06 拆单检测会把它们算在一起，相互污染" % overlap)


def test_区间判定在不确定段既不判违规也不放过(ctx):
    """这是降级设计的核心：中间段必须是 pending，不能偏向任何一侧。"""
    for bn, c in ctx["exp"].items():
        if not c["id"].startswith("PEND-"):
            continue
        r = _run(ctx, bn)
        assert r["conclusion"] == "review", \
            "%s 应该是 review（待人工落到 review），实际 %s" % (c["id"], r["conclusion"])
        assert not r["hits"], "%s 不该有命中：%s" % (c["id"], sorted(r["hits"]))
