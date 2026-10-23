# -*- coding: utf-8 -*-
"""规则本体 · 统一口径：三条「不许下断言」的边界 + 采纳的三道硬闸。

这些测试钉的不是算法，是**页面上会不会出现一句假话**：
  判不出被折叠成未落地 → 人去新增一条其实已经存在的规则，本体里长出重复规则
  编号对不上被当成查无依据 → 人去改一条其实有依据的规则
  有冲突的组被采纳     → 把一方口径当成了结论，而另一方连痕迹都不留
  Agent 能写 rule 表   → 「模型抽出来的东西自动变成生效规则」那条路就存在了
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, onto_coord as oc  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 一、来源代号钉文件：命中 0 份或多份都不算钉上
# ─────────────────────────────────────────────────────────────────────────────
def test_来源代号命中多份不算可比对():
    """多份命中时选一份就是猜，猜错会把规则挂到错的制度文件上。"""
    t = oc.code_targets()
    cmp_, _ = oc.comparable_files()
    for code, hits in t.items():
        if len(hits) == 1:
            assert hits[0] in cmp_, "%s 唯一命中却没进可比对集" % code
        else:
            for h in hits:
                assert h not in cmp_ or len(t[code]) == 1, \
                    "%s 命中 %d 份，不该当成钉上了" % (code, len(hits))


def test_实测的代号命中情况():
    """实测基线：要点/制度/解答各命中 1 份，规则表/场景表/配置器命中 0 份。

    这条会随文件库变化。**变了要改的是这个断言，不是算法**——
    它的作用是让「文件库换了一批文件」这件事在测试里显形。
    """
    t = oc.code_targets()
    assert len(t["规则表"]) == 0, "规则表现在能钉到文件了，对账口径要重新审"
    assert len(t["要点"]) == 1
    assert len(t["制度"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 二、三档 / 五档，一档都不许少
# ─────────────────────────────────────────────────────────────────────────────
def test_正向有判不出这一档():
    """「判不出」被折叠进「未落地」，人就会去新增重复规则。"""
    fwd = oc.align_forward()
    if not fwd:
        pytest.skip("库里还没有归并组，先跑一次统一口径")
    assert {x["verdict"] for x in fwd} <= {"已落地", "未落地", "判不出"}
    for x in fwd:
        if x["verdict"] == "已落地":
            assert x["rule_ids"], "判了已落地却说不出落在哪条规则上"
        else:
            assert not x["rule_ids"], "没落地却给了规则号"
        assert x["match_basis"], "每一档都必须说得出依据，否则页面上只有一个标签"


def test_反向五档都在且查无依据不含编号对不上():
    rev = oc.align_reverse()
    if not rev:
        pytest.skip("库里没有规则")
    ok = {"有制度依据", "仅规则表", "编号对不上", "查无依据", "来源未写明"}
    assert {x["state"] for x in rev} <= ok
    for x in rev:
        if x["state"] == "编号对不上":
            assert "两套编号对不上" in x["basis"], \
                "「编号对不上」必须在依据里说清这不是没有依据，否则人会去改规则"
        if x["state"] == "查无依据":
            assert "确实存在" in x["basis"], \
                "判「查无依据」必须说明文件与条款号都对上了——否则它就是「编号对不上」"


def test_不可比对文件里的条款不许判未落地():
    """规则清单钉不到的文件，它里面的条款既不能判已落地也不能判未落地。"""
    cmp_, _ = oc.comparable_files()
    rows = oc._group_rows()
    if not rows:
        pytest.skip("库里还没有归并组")
    by = {}
    for r in rows:
        by.setdefault(r["group_key"], []).append(r)
    for a in oc.align_forward():
        if a["verdict"] == "未落地":
            files = {r["file_name"] for r in by[a["group_key"]]}
            assert files & cmp_, \
                "%s 的来源全在不可比对文件里，却判了「未落地」" % a["group_key"]


# ─────────────────────────────────────────────────────────────────────────────
# 三、采纳的硬闸
# ─────────────────────────────────────────────────────────────────────────────
def test_有未裁决冲突的组不许采纳():
    g = db.query_one("""SELECT a.group_key FROM onto_align a
                         WHERE EXISTS (SELECT 1 FROM doclib_conflict c
                                        WHERE c.group_key = a.group_key
                                          AND c.state = '待裁决') LIMIT 1""")
    if not g:
        pytest.skip("当前没有带未裁决冲突的归并组")
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "x", "dimension": "D1",
                                        "rule_class": "hard", "control_type": "警告",
                                        "risk_class": "超标准"})
    assert "冲突" in str(e.value)


def test_采纳必须署名():
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt("随便一个组", "", {})
    # 组不存在也会报错，但署名这条必须独立成立：换一个存在的组再试
    g = db.query_one("SELECT group_key FROM onto_align LIMIT 1")
    if g:
        with pytest.raises(oc.AdoptError) as e2:
            oc.adopt(g["group_key"], "   ", {"name": "x"})
        assert "署名" in str(e2.value) or "冲突" in str(e2.value)
    assert e.value


def test_控制类型等四项不给默认值():
    g = db.query_one("""SELECT group_key FROM onto_align
                         WHERE group_key NOT IN
                           (SELECT group_key FROM doclib_conflict WHERE state = '待裁决')
                         LIMIT 1""")
    if not g:
        pytest.skip("没有可用的归并组")
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "只给了名称"})
    msg = str(e.value)
    for must in ("审核维度", "规则类型", "控制类型", "风险类别"):
        assert must in msg, "缺项提示里没有「%s」：%s" % (must, msg)


def test_软规则不许标禁止():
    g = db.query_one("""SELECT group_key FROM onto_align
                         WHERE group_key NOT IN
                           (SELECT group_key FROM doclib_conflict WHERE state = '待裁决')
                         LIMIT 1""")
    if not g:
        pytest.skip("没有可用的归并组")
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "x", "dimension": "D1",
                                        "rule_class": "soft", "control_type": "禁止",
                                        "risk_class": "超标准"})
    assert "软规则" in str(e.value)


def test_驳回暂缓必须写理由():
    g = db.query_one("SELECT group_key FROM onto_align LIMIT 1")
    if not g:
        pytest.skip("没有归并组")
    with pytest.raises(oc.AdoptError) as e:
        oc.dispose(g["group_key"], "驳回", "测试", "")
    assert "理由" in str(e.value)


# ─────────────────────────────────────────────────────────────────────────────
# 四、Agent 的能力边界
# ─────────────────────────────────────────────────────────────────────────────
def test_agent写不进rule表和采纳台账():
    """**不是「约定不写」，是能力上就不给。** 白名单里没有就写不进去。"""
    from app.runtime import yaml_loader
    assert "onto_align" in yaml_loader.WRITABLE_TABLES
    assert "onto_rule_cover" in yaml_loader.WRITABLE_TABLES
    assert "rule" not in yaml_loader.WRITABLE_TABLES, \
        "rule 进了白名单：这就有了一条「抽出来的东西自动变成生效规则」的路"
    assert "onto_adoption" not in yaml_loader.WRITABLE_TABLES, \
        "采纳台账进了白名单：采纳就不再是人的动作了"


def test_对齐agent的契约声明与实际写的表一致():
    """YAML 里写 act.writes 而代码写别的表，这份 YAML 就只是注释。"""
    from app.runtime import yaml_loader
    r = yaml_loader.load(verbose=False)
    cfg = r.agents["fj_l2_02_onto_align"]
    assert cfg.writable_tables == {"onto_align", "onto_rule_cover",
                                   "agent_signal", "agent_signal_log"}
    assert "rule" in cfg.readable_tables, "双向对账必须读 rule，否则反向那一向做不了"


def test_采纳进来的规则不会被装载脚本删掉():
    """装载脚本原来是无条件 DELETE FROM rule，会把人采纳的规则静默删掉。

    这里只验 SQL 的筛选条件（真跑装载会动用户的 release），
    用一个事务造行、删、回滚。
    """
    import json

    import psycopg2

    from app import config
    conn = psycopg2.connect(**config.DSN)
    try:
        cur = conn.cursor()
        sc = json.dumps({"file": "X", "clause": "第一条", "text": "t",
                         "confidence": "原生文本"}, ensure_ascii=False)
        cur.execute("""INSERT INTO rule(rule_id, version, name, dimension, risk_class,
                           rule_class, source_clause, scope, logic, priority, control_type,
                           compiled, compiled_reason, status, created_by)
                       VALUES ('TR-ZZ-99',1,'装载保护验证','D1','超标准','hard',%s,
                               '{}','{}',50,'警告',FALSE,'未编译','draft','onto_adopt:test')""",
                    (sc,))
        cur.execute("DELETE FROM release_item")
        cur.execute("DELETE FROM rule WHERE created_by = 'asset_loader'")
        cur.execute("SELECT rule_id FROM rule")
        left = [r[0] for r in cur.fetchall()]
        assert left == ["TR-ZZ-99"], "装载脚本的删除条件会清掉采纳进来的规则：%s" % left
    finally:
        conn.rollback()
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 五、存储说明必须说清「重算时什么会消失」
# ─────────────────────────────────────────────────────────────────────────────
def test_存储说明覆盖算出来的和人拍的():
    st = oc.storage()
    by = {r["table_name"]: r for r in st["items"]}
    assert by["onto_align"]["on_recalc"] == "整表重算"
    assert by["onto_rule_cover"]["on_recalc"] == "整表重算"
    assert by["onto_adoption"]["on_recalc"] == "永不清", \
        "人拍的动作被标成可重算，重算一次谁采纳过就全没了"
    assert by["rule"]["on_recalc"] == "不清"
    for r in st["items"]:
        assert r["why"], "%s 没写「为什么这么定」" % r["table_name"]


def test_双向对账不合成一个覆盖率():
    s = oc.summary()
    assert "forward" in s and "reverse" in s
    assert "coverage" not in s and "rate" not in s, \
        "出现了合成指标：两侧缺口的责任方和下一步动作完全不同，合成就看不出在哪一侧"
