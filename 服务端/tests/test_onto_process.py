# -*- coding: utf-8 -*-
"""流程本体：分层、旁路标记、说明文字都不许写死某一家的东西。

这组用例钉的是同一类 bug 的两处新发现（2026-10-31）：

1. **分层 BFS 的起点写死成 `"N01"`**，而那是福建海运集团的提单节点号。
   822港航 的提单是 P01，于是它 10 个节点全都 rank=None、
   全被当成「排不进层次」画在单独一行。**图还是画得出来的**，
   页面上看不出哪里错了——这正是这类 bug 难发现的原因。

2. **流程说明是一段写死的文字**，讲的是福建海运集团那条
   N04 AI审核 → N05 共享审核 的非阻断边。822港航 没有 N04 也没有 N05，
   可它的流程页照样显示这段话：另一家的结论画在这一家的页面上。

顺带钉住一个装载侧的缺陷：YAML 里 `bypasses:` 分组下的节点
**就是旁路**，不该再要求每条自己写一遍 `bypass: true`。
原来两个列表直接相加，福建海运集团的 B01/B02 入库时 is_bypass=false，
前端那条「旁路画虚线」的样式一直没生效。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, onto_process as op  # noqa: E402

FJHY, GH = "fjhy", "822gh"


def _rank(iid):
    return {n["node_id"]: n["rank"] for n in op.graph(iid)["nodes"]}


# ── 分层 ────────────────────────────────────────────────────────────────
def test_起点不写死另一家的节点号():
    """822港航 的节点号是 P 打头的，分层必须照样算出来。"""
    r = _rank(GH)
    assert r.get("P01") == 0, "提单 P01 不是第 0 层：%s" % r
    ranked = [k for k, v in r.items() if v is not None]
    assert len(ranked) >= 8, "822港航 只有 %d 个节点排进了层次：%s" % (len(ranked), r)


def test_福建海运集团的分层一个都没变():
    """改起点推定规则是为了修 822港航，**不许动另一家已经对的结果**。
    这几个数是改之前实测的，照抄下来当基线。"""
    r = _rank(FJHY)
    for node, want in (("N01", 0), ("N02", 1), ("N03", 1), ("N04", 2),
                       ("N05", 3), ("N06", 4), ("N99", 5)):
        assert r.get(node) == want, "%s 的层次从 %s 变成了 %s" % (node, want, r.get(node))
    # 这三个本来就排不进层次（撤回、跳过、以及没有入边的 N07）
    for node in ("B01", "B02", "N07"):
        assert r.get(node) is None, "%s 本来排不进层次，现在排进了第 %s 层" % (
            node, r.get(node))


def test_起点推定方式要写在说明里():
    """「这张图的第 0 层凭什么是它」必须查得到。
    推定规则有三级阶梯，用了哪一级要说出来，不能让人去读源码。"""
    for iid in (FJHY, GH):
        note = op.graph(iid)["note"]
        assert "起点" in note, (iid, note[:80])


def test_无入边的规则单独用会把福建海运集团整张图的层次弄没():
    """这条记录的是**为什么不能只用「没有入边」这一条规则**。

    福建海运集团的 N01 有入边（退回之后回到提单，是个环），
    只按「没有入边但有出边」推，它会退化成 0 个起点，整张图的层次一起没了，
    比写死 N01 更糟。所以阶梯第一级按节点名认。
    """
    g = op.graph(FJHY)
    has_in = {e["to_node"] for e in g["edges"]}
    assert "N01" in has_in, (
        "这条用例的前提没了：福建海运集团的 N01 现在没有入边了，"
        "起点推定的阶梯可以简化")


def test_推不出起点时不猜一个():
    """猜错的层次比没有层次更坏：页面会把它当成真的审批顺序。"""
    nodes = [{"node_id": "X1", "node_name": "甲", "is_bypass": False, "alias": []},
             {"node_id": "X2", "node_name": "乙", "is_bypass": False, "alias": []}]
    edges = [{"to_node": "X1"}, {"to_node": "X2"}]       # 互为入边 = 环
    adj = {"X1": ["X2"], "X2": ["X1"]}
    roots, how = op._start_nodes(nodes, edges, adj)
    assert roots == ["X1"] and "退化" in how, (roots, how)
    # 一个非旁路节点都没有时，返回空列表而不是硬塞一个
    roots, how = op._start_nodes([], [], {})
    assert roots == [] and "推不出" in how, (roots, how)


# ── 说明文字 ────────────────────────────────────────────────────────────
def test_说明里不许出现另一家的节点号():
    """822港航 的流程页原来显示的是福建海运集团 N04→N05 那条边的实测结论。"""
    note = op.graph(GH)["note"]
    for bad in ("N04", "N05", "AI审核"):
        assert bad not in note, "822港航 的流程说明里出现了 %s：%s" % (bad, note)


def test_非阻断的边要从本实例的数据里数出来():
    """福建海运集团有一条非阻断边，822港航 一条都没有。
    这两句话都必须是数出来的，不是写死的。"""
    gf = op.graph(FJHY)
    soft_f = [e for e in gf["edges"] if not e["blocking"]]
    assert len(soft_f) == 1, "福建海运集团的非阻断边应该是 1 条：%s" % soft_f
    assert "非阻断的边 1 条" in gf["note"], gf["note"]

    gg = op.graph(GH)
    assert not [e for e in gg["edges"] if not e["blocking"]]
    assert "没有非阻断的边" in gg["note"], gg["note"]


# ── 旁路标记 ────────────────────────────────────────────────────────────
def test_旁路分组本身就是声明():
    """YAML 里写在 `bypasses:` 下面的节点，入库必须 is_bypass=true。
    原来要求每条再写一遍 `bypass: true`，而福建海运集团那两条都没写，
    于是前端「旁路画虚线」的样式一直没生效——写了样式不等于样式在跑。"""
    rows = db.query("""SELECT node_id, is_bypass FROM process_node
                        WHERE instance_id = %s AND version = '0.1'
                          AND node_id IN ('B01','B02')""", (FJHY,))
    assert len(rows) == 2, "福建海运集团的 B01/B02 不在库里了：%s" % rows
    assert all(r["is_bypass"] for r in rows), rows


# ── 实例隔离 ────────────────────────────────────────────────────────────
def test_两家的节点与迁移互不可见():
    g1, g2 = op.graph(FJHY), op.graph(GH)
    n1 = {n["node_id"] for n in g1["nodes"]}
    n2 = {n["node_id"] for n in g2["nodes"]}
    assert not (n1 & n2), "两家的节点号撞上了：%s" % sorted(n1 & n2)
    assert len(g2["edges"]) != len(g1["edges"]), (
        "两家的迁移条数一样，八成又在查同一份数据")


def test_实测次数按本实例数():
    """822港航 的**报账流程**一条审批流水都没有，实测次数必须全空。
    原来不带实例去数，它的流程页显示的是另一家的 388 条。"""
    assert db.scalar("""SELECT count(*) FROM approval_record
                         WHERE instance_id = %s AND flow IS NULL""",
                     (GH,)) == 0, "822港航 现在有报账流程的审批流水了，前提要改"
    assert all(n["measured"] is None for n in op.graph(GH)["nodes"])
    assert any(n["measured"] for n in op.graph(FJHY)["nodes"])


def test_别条流程的审批流水不许计进报账流程():
    """**这条用例守的是一次真实的串流程事故。**

    2026-11-01 从业务接待审批单 PDF 解出 8 条流转记录写进 approval_record
    （发起节点/部门负责人/分管领导/主管领导）。而这张表原本装的是
    **报账流程**的审批记录，两条流程都有「分管领导」这个角色名——
    实测次数是按节点名匹配的，于是 822港航 的报账流程图上
    P05 分管领导 显示 `measured = 2`，**而这 29 张报账单一次都没被
    分管领导审过**。

    流程本体页上的「实测次数」是用来核对「本体画的流程和真实走的流程
    差多少」的，混了别条流程的记录，这个核对整段不可信。
    修法是加 approval_record.flow（迁移 0059）而不是给节点名加前缀：
    节点名就是节点名，它属于哪条流程是另一个维度。
    """
    # 8 → 16：2026-11-01 读图批又解出 2 份审批单的流转记录。
    # **数字会随装载批次长，用例守的不是数字，是「别条流程不许计进来」。**
    n_other = db.scalar("""SELECT count(*) FROM approval_record
                            WHERE instance_id=%s AND flow='业务接待审批'""",
                        (GH,))
    assert n_other == 16, n_other
    # P05 就是那个会被串进去的节点，必须是空的
    p05 = next(n for n in op.graph(GH)["nodes"] if n["node_id"] == "P05")
    assert p05["measured"] is None, p05
    assert db.scalar("""SELECT count(*) FROM approval_record
                         WHERE instance_id=%s AND flow='业务接待审批'
                           AND node='分管领导'""", (GH,)) == 4, (
        "接待审批里的分管领导条数变了，这条用例的前提要重核")
    # build() 的来源说明要把两条流程分开报，不许合成一个数
    src = op.build(GH)["source"]
    assert src["审批记录"] == 0, src
    assert src["另一条流程的审批记录"] == n_other, src
    # 福建海运集团那 388 条是报账流程的，不许被标成别条流程
    assert db.scalar("""SELECT count(*) FROM approval_record
                         WHERE instance_id='fjhy' AND flow IS NOT NULL""") == 0
