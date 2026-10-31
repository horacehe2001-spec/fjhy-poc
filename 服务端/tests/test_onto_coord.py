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

from app import db, onto_coord as oc, scope  # noqa: E402


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
    # **选组的判据必须和闸的判据是同一个**，所以直接用 oc.pending_conflicts()。
    # 这里原来自己写了一遍 SQL（按 group_key 找冲突、不带实例），而 doclib_conflict
    # 没有 instance_id、group_key 又是跨实例会撞的文本键，于是 822港航 搭完本体
    # 产生新冲突之后，测试选到了归属对面的那条——闸正确地不响，
    # 报出来却像是闸坏了。两处各写一遍判据，迟早对不上。
    iid = scope.require()
    g = None
    for row in db.query("SELECT group_key FROM onto_align WHERE instance_id = %s", (iid,)):
        if oc.pending_conflicts(row["group_key"], iid):
            g = row
            break
    if not g:
        pytest.skip("当前没有带未裁决冲突的归并组")
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "x", "dimension": "D1",
                                        "rule_class": "hard", "control_type": "警告",
                                        "risk_class": "超标准"})
    assert "冲突" in str(e.value)


def test_来源条款已不存在的陈旧冲突照样拦住采纳():
    """**修过的 bug，而且失效方向是「放行」。**

    doclib_conflict 没有 instance_id，只能经 left_clause → doclib_clause →
    doc_lib_file 反推归属。而 clause_id 是内容定址的，**条款一重切就换号**，
    旧冲突行就此悬空。原来的写法用 INNER JOIN，悬空行被直接丢掉，
    count 变 0，闸就不响了。
    2026-10-30 实测：9 条待裁决里 2 条悬空，「业务接待·审批」组实际有 1 条
    待裁决而闸数出 0——于是它被成功采纳，还往 rule 表里落了一条。

    这道闸必须 fail-closed：**判不出归属时算「有冲突」**。
    拦错了人能看懂并去裁决；放过了没有任何人会发现。
    """
    iid = scope.require()
    g = db.query_one("SELECT group_key FROM onto_align WHERE instance_id=%s LIMIT 1",
                     (iid,))
    if not g:
        pytest.skip("没有归并组")
    gk = g["group_key"]
    had = db.query("SELECT conflict_id FROM doclib_conflict WHERE group_key=%s", (gk,))
    # 造一条**来源条款不存在**的待裁决冲突
    db.execute("""INSERT INTO doclib_conflict(conflict_id, group_key, kind, detail,
                      left_clause, right_clause, state)
                  VALUES ('zz-test-dangling', %s, '口径不一致', '测试用悬空冲突',
                          'dlc-这个条款不存在', 'dlc-这个也不存在', '待裁决')""", (gk,))
    try:
        with pytest.raises(oc.AdoptError) as e:
            oc.adopt(gk, "测试", {"name": "x", "dimension": "D1",
                                 "rule_class": "hard", "control_type": "警告",
                                 "risk_class": "超标准"})
        assert "冲突" in str(e.value), \
            "悬空冲突没拦住——闸在「放行」这一侧静默失效了：%s" % e.value
    finally:
        db.execute("DELETE FROM doclib_conflict WHERE conflict_id='zz-test-dangling'")
        # 顺手确认没留下半成品规则
        db.execute("DELETE FROM rule WHERE created_by LIKE %s AND name=%s",
                   ("onto_adopt:测试%", "x"))
    assert len(db.query("SELECT conflict_id FROM doclib_conflict WHERE group_key=%s",
                        (gk,))) == len(had), "测试造的冲突没清干净"



# ── 取归并组一律按**当前实例**取 ─────────────────────────────────────────────
# 2026-11-01：下面几条用例原来写的是 `SELECT group_key FROM onto_align LIMIT 1`，
# **不带实例**。而 oc.adopt / oc.dispose 是按**当前实例**查这个组的。
# onto_align 里现在 12 个组全属 822港航，于是只要跑到这几条时当前实例不是它，
# 就会报「本实例下没有这个归并组」——而用例想验的是署名/缺项/软规则那几件事。
# 当前实例是全局单例，谁都可能在前面把它切走，所以**不能依赖它恰好是哪一个**。
# 改成按当前实例取组：取不到就跳过，取到的一定是 adopt 查得到的那一个。
def _a_group(where=""):
    """随便取一个归并组，**连它属于哪个实例一起返回**。

    第一版写的是「按当前实例取」，结果三条用例从 FAILED 变成了 SKIPPED
    ——**跳过的用例什么都不验，那是把问题藏起来不是修好**。
    原因是当前实例是全局单例，pytest 里被别的 fixture 切走了，
    而 onto_align 的组全属另一个实例，于是取不到就跳过。

    正确做法：用例不依赖「当前实例恰好是哪一个」，
    自己把 instance_id 显式传给 adopt/dispose。
    """
    row = db.query_one(
        "SELECT group_key, instance_id FROM onto_align WHERE TRUE " + where
        + " LIMIT 1")
    return row

def test_采纳必须署名():
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt("随便一个组", "", {})
    # 组不存在也会报错，但署名这条必须独立成立：换一个存在的组再试
    g = _a_group()
    if g:
        with pytest.raises(oc.AdoptError) as e2:
            oc.adopt(g["group_key"], "   ", {"name": "x"},
                     instance_id=g["instance_id"])
        assert "署名" in str(e2.value) or "冲突" in str(e2.value)
    assert e.value


def test_控制类型等四项不给默认值():
    g = _a_group(
        """AND group_key NOT IN
             (SELECT group_key FROM doclib_conflict WHERE state = '待裁决')""")
    assert g, "库里一个可用的归并组都没有，这条用例要重新找锚点"
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "只给了名称"},
                 instance_id=g["instance_id"])
    msg = str(e.value)
    for must in ("审核维度", "规则类型", "控制类型", "风险类别"):
        assert must in msg, "缺项提示里没有「%s」：%s" % (must, msg)


def test_软规则不许标禁止():
    g = _a_group(
        """AND group_key NOT IN
             (SELECT group_key FROM doclib_conflict WHERE state = '待裁决')""")
    assert g, "库里一个可用的归并组都没有，这条用例要重新找锚点"
    with pytest.raises(oc.AdoptError) as e:
        oc.adopt(g["group_key"], "测试", {"name": "x", "dimension": "D1",
                                        "rule_class": "soft", "control_type": "禁止",
                                        "risk_class": "超标准"},
                 instance_id=g["instance_id"])
    assert "软规则" in str(e.value)


def test_驳回暂缓必须写理由():
    g = _a_group()
    assert g, "库里一个归并组都没有，这条用例要重新找锚点"
    with pytest.raises(oc.AdoptError) as e:
        oc.dispose(g["group_key"], "驳回", "测试", "",
                   instance_id=g["instance_id"])
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
        cur.execute("""INSERT INTO rule(instance_id, rule_id, version, name, dimension,
                           risk_class, rule_class, source_clause, scope, logic, priority,
                           control_type, compiled, compiled_reason, status, created_by)
                       VALUES ('fjhy','TR-ZZ-99',1,'装载保护验证','D1','超标准','hard',%s,
                               '{}','{}',50,'警告',FALSE,'未编译','draft','onto_adopt:test')""",
                    (sc,))
        # **删与查都要带实例。** 装载脚本只服务 fjhy（rules/loader.py 里
        # INSTANCE 写死），所以它的删除条件和这里的断言都只该看那一片。
        # 不带实例的后果 2026-10-30 实测到了：822港航 转入 74 条规则之后，
        # `SELECT rule_id FROM rule` 把它们全查了出来，断言报
        # 「装载脚本会删掉采纳进来的规则」——而装载脚本根本碰不到那 74 条。
        # 报出来的结论和真相相反，排查方向会被整个带偏。
        cur.execute("DELETE FROM release_item WHERE instance_id = 'fjhy'")
        cur.execute("DELETE FROM rule "
                    "WHERE instance_id = 'fjhy' AND created_by = 'asset_loader'")
        cur.execute("SELECT rule_id FROM rule WHERE instance_id = 'fjhy'")
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
