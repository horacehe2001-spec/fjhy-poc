# -*- coding: utf-8 -*-
"""领域与实例。

**这组测试会改全局状态**（当前领域/实例是库里一行，所有人共用），
所以每个用例跑完都把它还原——否则跑一次测试，演示环境的当前实例
就停在「报表 / 无实例」上，而页面上看到的是「该领域下还没有实例」，
看的人会以为功能坏了。这是本项目第 12 条纪律的同类：
**验证动作的副作用不许留在演示环境里。**
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                      # noqa: E402
from app.api import domain as dm        # noqa: E402


@pytest.fixture(autouse=True)
def _还原当前选择():
    before = (dm._get(dm.KEY_DOMAIN), dm._get(dm.KEY_INSTANCE))
    yield
    dm._set(dm.KEY_DOMAIN, before[0], "test:restore")
    dm._set(dm.KEY_INSTANCE, before[1], "test:restore")


@pytest.fixture
def tmp_domain():
    """一个没有实例的临时领域。**用完必须删**——留在库里，
    页面上就多出一个谁也不知道哪来的领域（纪律 12）。"""
    did = "dom-test-empty"
    db.execute("""INSERT INTO domain (domain_id, label, sort_no, note)
                  VALUES (%s, %s, 900, %s)
                  ON CONFLICT (domain_id) DO NOTHING""",
               (did, "测试空领域", "单元测试建的，跑完即删"))
    yield did
    db.execute("DELETE FROM domain WHERE domain_id = %s", (did,))


def test_种子数据齐全():
    st = dm.state()
    ids = {d["domain_id"] for d in st["domains"]}
    assert {"audit", "report"} <= ids
    audit = [d for d in st["domains"] if d["domain_id"] == "audit"][0]
    insts = [i["instance_id"] for i in audit["instances"]]
    # **不写死成完整相等**（原来是 == ["fjhy"]）：这一页允许用户用「添加实例」
    # 建新实例，写死全等会让这条测试在每次加实例时变红——而它要钉的不是
    # "审核领域下只有一个实例"，是"种子数据在"。
    # 822gh（822港航）是 0040 的种子，也一起钉住：它是第一个验证按实例隔离
    # 真的生效的实例，被人误删了要立刻红。
    assert {"fjhy", "822gh"} <= set(insts)
    # 默认实例仍然必须是福建海运集团：现有全部资产都归它，
    # 默认指向空实例会让新打开页面的人看到一片 0。
    dft = [i for i in audit["instances"] if i["is_default"]]
    assert [i["instance_id"] for i in dft] == ["fjhy"]
    report = [d for d in st["domains"] if d["domain_id"] == "report"][0]
    assert [i["label"] for i in report["instances"]] == ["华电集团"]


def test_默认是审核加福建海运集团():
    dm.set_current({"domain_id": "audit"})
    cur = dm.current()
    assert cur["domain_id"] == "audit"
    assert cur["instance_id"] == "fjhy"
    assert cur["instance_label"] == "福建海运集团"


def test_不传实例时取该领域的默认实例():
    dm.set_current({"domain_id": "audit", "instance_id": None})
    assert dm.current()["instance_id"] == "fjhy"


def test_报表领域的默认实例是华电集团():
    """用户 2026-10-21 给的初始值：审核→福建海运集团，报表→华电集团。"""
    r = dm.set_current({"domain_id": "report"})
    assert r["current"]["instance_id"] == "hdjt"
    assert r["current"]["instance_label"] == "华电集团"


def test_切到没有实例的领域时实例置空而不是留着上一个(tmp_domain):
    """**最关键的一条。** 留着上一个实例 = 显示「新领域 / 福建海运集团」，
    那是个不存在的组合，而且看不出错。

    两个种子领域现在都有默认实例了，所以这条用一个临时领域来测——
    这个分支不能因为种子数据变了就没人守。"""
    dm.set_current({"domain_id": "audit"})          # 先确保当前有实例
    assert dm.current()["instance_id"] == "fjhy"
    r = dm.set_current({"domain_id": tmp_domain})
    assert r["current"]["domain_id"] == tmp_domain
    assert r["current"]["instance_id"] is None
    assert "还没有实例" in r["note"]


def test_新增领域不自动建实例也不自动切过去():
    """建一个东西 ≠ 我要开始用它。当前领域是全局单例，自动切会影响所有人。"""
    before = dm.current()["domain_id"]
    r = dm.add_domain({"label": "测试领域-自动清理"})
    try:
        assert r["ok"] and r["domain_id"].startswith("dom-")
        st = dm.state()
        d = [x for x in st["domains"] if x["domain_id"] == r["domain_id"]][0]
        assert d["instances"] == []                 # 不自动建实例
        assert dm.current()["domain_id"] == before  # 不自动切过去
    finally:
        db.execute("DELETE FROM domain WHERE domain_id = %s", (r["domain_id"],))


def test_新增领域要查重和非空():
    with pytest.raises(Exception) as e:
        dm.add_domain({"label": "   "})
    assert "不能为空" in str(e.value)
    with pytest.raises(Exception) as e:
        dm.add_domain({"label": "审核"})
    assert "已有同名" in str(e.value)


def test_不许跨领域选实例():
    with pytest.raises(Exception) as e:
        dm.set_current({"domain_id": "report", "instance_id": "fjhy"})
    assert "不属于" in str(e.value)


def test_领域不存在时报错而不是静默置空():
    with pytest.raises(Exception) as e:
        dm.set_current({"domain_id": "nope"})
    assert "不存在" in str(e.value)


def test_实例不存在时报错():
    with pytest.raises(Exception) as e:
        dm.set_current({"domain_id": "audit", "instance_id": "nope"})
    assert "不存在" in str(e.value)


def test_库里不一致时如实暴露不静默纠正():
    """手工把当前实例写成不属于当前领域的那个，current() 要标 mismatch。
    静默纠正会让「我明明选的是 A，怎么显示 B」查不出来。"""
    dm._set(dm.KEY_DOMAIN, "report", "test")
    dm._set(dm.KEY_INSTANCE, "fjhy", "test")        # fjhy 属于 audit
    cur = dm.current()
    assert cur["mismatch"] is True
    assert cur["instance_id"] is None


def test_切换留痕():
    dm.set_current({"domain_id": "audit", "who": "测试员"})
    who = db.scalar("SELECT updated_by FROM app_setting WHERE setting_key = %s",
                    (dm.KEY_DOMAIN,))
    assert who == "测试员"


def test_概览的本体数不含旧草稿版也不含别的实例():
    """ontology_entity 里躺着 0.1 与 v0.1-draft 两版，各 17 行。
    直接 count(*) 得 34；按 max(version) 取「最新」会选中 **v0.1-draft**
    （TEXT 比大小 v > 0）。两种写法都错，必须走 ontology_version.is_current。

    **2026-10-31 补第三个维度：实例。** 原来这里断言
    `onto_entities == count(WHERE version=cur_ver)`，**把一个错的行为钉住了**：
    两个实例的当前版本号都叫「0.1」，所以那个数是两家加总。实测概览页报
    18 实体 / 133 概念，而 822港航 自己只有 1 / 12——一张骨架看着像建厚了。
    而且这个用例里 `SELECT version FROM ontology_version WHERE is_current`
    在多实例下有两行，取到哪一行靠运气。
    用例钉的必须是**对的口径**，不是当时的实际输出。
    """
    from app import scope
    iid = scope.require()
    o = dm.overview()
    total_all = db.scalar("SELECT count(*) FROM ontology_entity")
    cur_ver = db.scalar("""SELECT version FROM ontology_version
                            WHERE is_current AND instance_id = %s""", (iid,))
    assert o["onto_version"] == cur_ver
    assert o["counts"]["onto_entities"] < total_all
    assert o["counts"]["onto_entities"] == db.scalar(
        """SELECT count(*) FROM ontology_entity
            WHERE version = %s AND instance_id = %s""", (cur_ver, iid))
    # 版本和实例各自都要真的起作用：去掉任一个条件，数都应该变大（或相等，
    # 但绝不能更小）——写成 <= 是因为单实例/单版本的库里本来就相等。
    assert o["counts"]["onto_entities"] <= db.scalar(
        "SELECT count(*) FROM ontology_entity WHERE version = %s", (cur_ver,))
    assert o["counts"]["onto_entities"] <= db.scalar(
        "SELECT count(*) FROM ontology_entity WHERE instance_id = %s", (iid,))


def test_概览的生效版本与引擎一致():
    """全站只许有一个「哪一版生效」的定义。概览自己写一句
    WHERE status='current' 会返回 None（库里没有这个状态值），
    页面就会显示「没有发布版本」，而引擎其实正按某一版在判。"""
    from app.rules import engine
    rel, _why = engine.effective_release()
    o = dm.overview()
    assert o["current_release"] == (rel["release_id"] if rel else None)


def test_概览标明哪些数按实例过滤哪些没有():
    """**口径必须写在返回里。**

    原来这条钉的是"尚未按实例过滤"——那是只有一个实例时的实话。
    2026-10-29 加了第二个实例并做完隔离（迁移 0040/0041）之后，实话变了：
    制度文件/条款/规则/单据这几个数是当前实例的，**本体那两个数仍是全库的**
    （ontology_* 还没有 instance_id，两实例共用同一份本体投影）。
    真正要防的不是某句措辞，是**让人把全库数字当成本实例数字**，
    所以这里钉的是"差别有没有被说出来"。
    """
    o = dm.overview()
    note = o["scope_note"]
    assert "已按当前实例过滤" in note, "过滤了就要说，不说等于让人自己猜"
    assert "全库" in note and "本体" in note, \
        "本体仍是全库数字这件事必须明写，否则下一个人会以为本体也隔离了"
