# -*- coding: utf-8 -*-
"""822港航 本体的实体与关系：**不许宣称库里不成立的事**。

2026-11-01 之前，822港航 的本体是 1 个脚手架实体 + 21 个全挂在它下面的概念
+ 0 条关系。「没有关系的本体是一张平铺的字段表，不是本体。」

这一版按**库里真有数据的表**划了 10 个实体、11 条关系。
这组用例守的是这个口径本身：

1. **每个实体声称的 rows 必须等于库里实际行数。** YAML 里写 rows 是为了
   让人一眼看出这个实体实不实；写了一个对不上的数，比不写更坏——
   读的人会拿它当依据。

2. **概念只收实际有值的列。** 立一个取不到值的概念，规则会以为自己
   能读到它。唯一的例外是 C42 逐日地点，它立出来是**为了让缺口可见**，
   注释里写明了。

3. **关系的基数取实测分布，不取制度上的应然。** 应然 1..n 而实测 0..n 的，
   写 0..n——否则本体在宣称一件库里不成立的事。
"""
import io
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db  # noqa: E402

IID = "822gh"
YML = os.path.join(config.PROJECT_ROOT, "本体_822gh_v0.1.yaml")

# 实体 → 它背后那张表。**这张表是本组用例的唯一真相来源**，
# 别处不许再抄一份：抄两份，口径一变就会漏改一处。
ENTITY_TABLE = {
    "E01": "expense_bill", "E02": "expense_detail", "E03": "trip_application",
    "E04": "trip_calendar", "E05": "invoice", "E08": "employee",
    "E09": "org", "E10": "standard_limit", "E13": "attachment",
    "E18": "entertain_detail",
    # 2026-11-01 补的三个。这三张表当天从 0 行变成有数据，按本文件
    # 「0 行的表不立实体」的反面就该立。编号与福建海运集团同表同号
    # （E12 审批记录 / E15 会议培训通知），E19 是 822港航 特有的。
    "E12": "approval_record", "E15": "meeting_notice",
    "E19": "entertain_approval",
}


def _onto():
    return yaml.safe_load(io.open(YML, encoding="utf-8").read())


def _rows(table):
    return db.scalar("SELECT count(*) FROM %s WHERE instance_id=%%s" % table,
                     (IID,))


# ── 实体 ────────────────────────────────────────────────────────────────
def test_实体声称的行数必须等于库里实际():
    ents = _onto()["business"]["entities"]
    assert len(ents) == 13, len(ents)
    bad = []
    for e in ents:
        t = ENTITY_TABLE.get(e["id"])
        assert t, "实体 %s 没登记背后的表" % e["id"]
        n = _rows(t)
        if n != e.get("rows"):
            bad.append((e["id"], e["name"], e.get("rows"), n))
    assert not bad, "声称与实际对不上：%s" % bad


def test_每个实体都有数据支撑():
    """0 行的表不立实体。福建海运集团有而这边没建的 8 个，
    YAML 注释里逐个写了原因——**「没建」和「漏了」要分得开**。"""
    for eid, t in ENTITY_TABLE.items():
        assert _rows(t) > 0, "%s 背后的 %s 是 0 行，不该立成实体" % (eid, t)
    # 这几张在本实例是 0 行，所以没有对应实体。
    # **用 data_availability.table_rows 数，不要自己拼 SQL**：
    # budget / hotel_invoice / expense_allocation 这几张**没有 instance_id 列**，
    # 归属是经外键推的，裸查 `WHERE instance_id=...` 会直接报列不存在。
    # 那个模块的职责就是这件事，别处再写一遍就会漏掉这一类表。
    # approval_record 原来在这个名单里。2026-11-01 从业务接待审批单 PDF
    # 解出 8 条流转记录之后它不是 0 行了，于是**按这条用例自己的规矩
    # 立了实体（E12）**，从名单里移出去。
    # 这正是这条用例该起的作用：它提示的是「该考虑立实体」，
    # 不是「把断言改掉」。
    from app import data_availability as da
    for t in ("budget", "expense_allocation", "hotel_invoice",
              "transport_voucher", "org_special_rule"):
        assert da.table_rows(IID, [t])[t] == 0, "%s 现在有数据了，该考虑立实体" % t
        assert t not in ENTITY_TABLE.values()


def test_实体编号与福建海运集团对齐():
    """同一样东西用同一个号，两家才对照得了。
    822港航 独有的从 E18 起编，不占对方已经用掉的 E01~E17。"""
    fj = {r["entity_id"]: r["name"] for r in db.query(
        """SELECT entity_id, name FROM ontology_entity
            WHERE instance_id='fjhy' AND version='0.1'""")}
    gh = {r["entity_id"]: r["name"] for r in db.query(
        """SELECT entity_id, name FROM ontology_entity
            WHERE instance_id=%s AND version='0.1'""", (IID,))}
    assert gh, "822港航 本体没装进库"
    for eid, name in gh.items():
        if eid in fj:
            assert name == fj[eid], (
                "E%s 两家指的不是同一样东西：fjhy=%s / 822gh=%s"
                % (eid, fj[eid], name))
        else:
            assert int(eid[1:]) >= 18, "新实体 %s 占了对方已用的编号段" % eid


# ── 概念 ────────────────────────────────────────────────────────────────
def test_概念不许凭空立():
    """立一个取不到值的概念，规则会以为自己能读到它。
    唯一的例外是 C42 逐日地点——它立出来是为了让缺口可见，
    **而且必须在注释里写明是例外**，否则下一个人会照着再立几个。"""
    cons = [c for e in _onto()["business"]["entities"]
            for c in (e.get("concepts") or [])]
    # 63 → 76：2026-11-01 补了三个实体（E12 审批记录 / E15 会议培训通知 /
    # E19 业务接待审批单），它们带 13 个概念。
    assert len(cons) == 76, len(cons)
    # 故意立出来的空概念。立它们是为了让缺口可见，**每一个都要写明**，
    # 否则下一个人会照着再立几个，而立一个取不到值的概念会让规则
    # 以为自己能读到它。
    #   C42 逐日地点 —— 出差审批单 PDF 的逐日表没有地点列
    #   C69 活动地点 —— 这一份通知的地点被 PDF 页码行截断了
    empty_ok = {"C42", "C69"}
    for c in cons:
        if c["id"] in empty_ok:
            assert "没有" in (c.get("source") or "") \
                or "0/" in (c.get("source") or "") \
                or "缺口" in (c.get("note") or "") \
                or "留空" in (c.get("note") or ""), (
                "%s 是故意立的空概念，但没写明" % c["id"])
    # 反面也要守：**除了名单里那几个，其余概念都得有 source**
    for c in cons:
        assert c.get("source"), "%s 没写出处" % c["id"]


def test_概念id不许重复():
    cons = [c["id"] for e in _onto()["business"]["entities"]
            for c in (e.get("concepts") or [])]
    dup = sorted({c for c in cons if cons.count(c) > 1})
    assert not dup, dup


def test_概念引用的枚举都真的存在():
    o = _onto()
    enums = {e["id"] for e in o["business"]["enums"]}
    used = {c["enum"] for e in o["business"]["entities"]
            for c in (e.get("concepts") or []) if c.get("enum")}
    assert not (used - enums), used - enums
    # 库里也要能查到，否则装载时外键会拒
    for eid in used:
        assert db.scalar("""SELECT count(*) FROM ontology_enum
                             WHERE instance_id=%s AND enum_id=%s""",
                         (IID, eid)), eid


def test_招待对象名单没有被立成概念():
    """那一列整列不出明文（外部人员姓名与职务黏连，脱敏闭集合切不出来）。
    立概念会让它看起来是可以拿来判的字段。"""
    cons = [c["name"] for e in _onto()["business"]["entities"]
            for c in (e.get("concepts") or [])]
    assert "招待对象名单" not in cons


# ── 关系 ────────────────────────────────────────────────────────────────
def test_关系两端都要落在实体上():
    o = _onto()
    ids = {e["id"] for e in o["business"]["entities"]}
    rels = o["business"]["relations"]
    assert len(rels) == 11, len(rels)
    for r in rels:
        assert r["from"] in ids and r["to"] in ids, r


def test_关系基数取实测分布不取应然():
    """应然 1..n 而实测 0..n 的，要写 0..n。
    这里逐条拿库里的数验「下限是 0 还是 1」。"""
    rels = {(r["from"], r["to"]): r for r in _onto()["business"]["relations"]}

    def covered(sql):
        """有多少张报账单满足这个 EXISTS 条件。"""
        return db.scalar(
            "SELECT count(*) FROM expense_bill b WHERE b.instance_id=%s AND "
            + sql, (IID,))

    total = _rows("expense_bill")
    checks = [
        (("E01", "E02"),
         "EXISTS (SELECT 1 FROM expense_detail d WHERE d.bill_no=b.bill_no)"),
        (("E01", "E13"),
         "EXISTS (SELECT 1 FROM attachment a WHERE a.bill_no=b.bill_no)"),
        (("E01", "E05"),
         "EXISTS (SELECT 1 FROM invoice i WHERE i.bill_no=b.bill_no)"),
        (("E01", "E03"),
         "EXISTS (SELECT 1 FROM trip_application t WHERE t.bill_no=b.bill_no)"),
        (("E01", "E18"),
         "EXISTS (SELECT 1 FROM entertain_detail e WHERE e.bill_no=b.bill_no)"),
    ]
    for key, sql in checks:
        n = covered(sql)
        low = rels[key]["card"].split("..")[0]
        if n == total:
            assert low == "1", "%s 实测 %d/%d 全覆盖，下限该是 1" % (key, n, total)
        else:
            assert low == "0", (
                "%s 实测只有 %d/%d 张单有，下限必须是 0——"
                "写 1 就是在宣称一件库里不成立的事" % (key, n, total))


def test_没有外键的关系要说明是算出来的():
    """E13→E05（附件佐证发票）靠文件名里的发票号对上，库里没有外键。
    **算出来的关系要标出来**，否则下一个人会去找那条不存在的外键。"""
    r = {(x["from"], x["to"]): x for x in _onto()["business"]["relations"]}
    note = r[("E13", "E05")].get("note") or ""
    assert "算出来" in note or "没有外键" in note, note


# ── 装载结果 ────────────────────────────────────────────────────────────
def test_装进库的数与源文件一致():
    o = _onto()
    assert db.scalar("""SELECT count(*) FROM ontology_entity
                         WHERE instance_id=%s AND version='0.1'""",
                     (IID,)) == len(o["business"]["entities"])
    assert db.scalar("""SELECT count(*) FROM ontology_relation
                         WHERE instance_id=%s AND version='0.1'""",
                     (IID,)) == len(o["business"]["relations"])
    n_con = sum(len(e.get("concepts") or []) for e in o["business"]["entities"])
    assert db.scalar("""SELECT count(*) FROM ontology_concept
                         WHERE instance_id=%s AND version='0.1'""",
                     (IID,)) == n_con


def test_别家的本体没被动():
    assert db.scalar(
        "SELECT count(*) FROM ontology_entity WHERE instance_id='fjhy'") == 34
    assert db.scalar(
        "SELECT count(*) FROM ontology_relation WHERE instance_id='fjhy'") == 36
