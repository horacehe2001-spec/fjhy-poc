# -*- coding: utf-8 -*-
"""发布管理：资产化的三条口径，和会把它们破掉的那些写法。

钉的全是「破了之后很难发现」的地方：
  版本号复用        → 同一个号两次拉到不同内容，对外消费里最难查的事故
  哈希含簿记字段    → 重装一次规则，内容没变但「变没变」永远判不出来
  快照用 name 当键  → 经 HTTP 出去被人名脱敏吃成「付**********」（上一轮刚踩过）
  装载清了当前指针  → 「装载一次，生效版本就没了」（用户 2026-10-19 明确否掉的行为）
  未签字能发布      → 等于让脚本直接改生产口径
  草稿能被消费      → 没签字的东西流出去了
  已发布能被删      → 资产没了，审计断链

**测试数据一律用 rs-TEST- 前缀命名**，跑完逐条删掉：
这些测试在用户的开发库上跑，留一条假发布在演示环境里会造成困惑。
"""
import hashlib
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, onto_yaml as oy, release_pack as rp  # noqa: E402
from app.rules import engine, impl  # noqa: E402,F401

TEST_PREFIX = "rs-TEST-"


def _cleanup():
    db.execute("DELETE FROM release WHERE rule_set_version LIKE %s", (TEST_PREFIX + "%",))


@pytest.fixture
def clean():
    _cleanup()
    yield
    _cleanup()


def _freeze(tag, by="测试", note=None):
    return rp.freeze(TEST_PREFIX + tag, by, note)


# ─────────────────────────────────────────────────────────────────────────────
# 一、八层与三元本体
# ─────────────────────────────────────────────────────────────────────────────
def test_九层都抄全了并且三元本体在里面():
    a = rp.collect()
    assert set(a) == {"文件", "规则", "规则参数", "本体源文件",
                      "业务本体", "规则本体", "流程本体",
                      "事实模型", "规则绑定"}
    # 用户 2026-10-19 定的「三元本体」= 业务 / 规则 / 流程
    assert rp.TRIPLE == ("业务本体", "规则本体", "流程本体")
    for k in rp.TRIPLE:
        assert a[k]["item_count"] > 0, "%s 这一层是空的" % k


def test_文件层冻的是清单不是文件本身():
    """发布包里不塞客户的原始文档，只塞「用了哪些、内容指纹是什么」。

    这条边界要是糊了，别人以为拿到包就等于拿到源文件。
    """
    n, p = rp._snap_files()
    assert n > 0
    f = p["files"][0]
    assert "content_sha256" in f and "rel_path" in f
    assert "body" not in f, "把文件正文塞进发布包了"
    assert "abs_path" not in f, "绝对路径不该进对外发布包"
    # mtime **故意不进**：拷一次盘 mtime 就变，内容没变。身份用内容哈希认。
    assert "mtime" not in f


def test_本体源文件冻的是带注释的原文():
    """三元本体那三层是库投影，投影只剩结构。

    925 行里的口径说明与踩坑记录才是本体资产最贵的部分（CLAUDE.md 第 10 条
    讲的是同一件事：注释丢了在 review 里极难发现）。
    """
    n, p = rp._snap_onto_source()
    assert n > 800
    # 实测 2026-10-19：925 行里有 63 个注释行。阈值取 40 留余量——
    # 第一版这里拍了个 >100，红了：错的是断言，不是代码。
    comments = sum(1 for ln in p["text"].splitlines() if ln.lstrip().startswith("#"))
    assert comments >= 40, "冻下来的文本只有 %d 个注释行，八成冻的是解析结果" % comments
    assert p["sha256"] and p["written_by"]
    # loaded（库里的投影是否同步）是**当时的活状态**，不是内容，不许冻进去
    assert "loaded" not in p and "in_sync" not in p


def test_派生资产也要冻():
    """事实模型 / 规则绑定 是派生物，但消费方要的是「这一版引擎吃的契约」。"""
    a = rp.collect()
    assert a["事实模型"]["payload"]["content"], "事实模型没冻进来"
    assert a["规则绑定"]["payload"]["content"], "规则绑定没冻进来"
    assert a["事实模型"]["source_kind"] == "文件"


# ─────────────────────────────────────────────────────────────────────────────
# 二、哈希口径
# ─────────────────────────────────────────────────────────────────────────────
def test_内容哈希与键序无关():
    """不规范化就白写：同一份内容两次 dumps 键序不同，哈希就不同。"""
    assert rp.sha_obj({"a": 1, "b": [2, 3]}) == rp.sha_obj({"b": [2, 3], "a": 1})
    assert rp.sha_obj({"a": 1}) != rp.sha_obj({"a": 2})


def test_规则快照不含created_at():
    """created_at 是簿记字段，重装就变 now()。

    进了哈希，「这一版和上一版内容一样」就永远判不出来——
    而那个判断正是 diff 与「要不要发新版」的依据。
    """
    _n, p = rp._snap_rules()
    assert p["rules"], "工作区一条规则都没有"
    for r in p["rules"][:5]:
        assert "created_at" not in r


def test_快照里的名字一律不叫name():
    """**这是个出口。** 快照会经 /api/pub 原样送出去。

    叫 name 的字段会被人名脱敏按字段名吃成「付**********」——
    上一轮规则本体页的「名称」整列就是这么变成星号的。
    所以快照从源头就用 rule_name / entity_name / concept_name / enum_name / node_name。
    """
    a = rp.collect()
    assert all("name" not in r for r in a["规则"]["payload"]["rules"])
    assert all(r.get("rule_name") for r in a["规则"]["payload"]["rules"])
    biz = a["业务本体"]["payload"]
    for key, want in (("entities", "entity_name"), ("concepts", "concept_name"),
                      ("enums", "enum_name")):
        assert biz[key], "业务本体 %s 是空的" % key
        for x in biz[key]:
            assert "name" not in x, "%s 里还有裸 name 键" % key
            assert x.get(want), "%s 缺 %s" % (key, want)
    for nd in a["流程本体"]["payload"]["nodes"]:
        assert "name" not in nd and nd.get("node_name")


def test_包级校验不是zip的哈希():
    """zip 里有时间戳，同样内容两次打包字节不同——包哈希必须算在内容上。"""
    a = rp.collect()
    b1 = rp.bundle_sha(a)
    b2 = rp.bundle_sha(dict(reversed(list(a.items()))))
    assert b1 == b2, "包级校验受层的字典顺序影响了"
    assert len(b1) == 64


# ─────────────────────────────────────────────────────────────────────────────
# 三、闸门
# ─────────────────────────────────────────────────────────────────────────────
def test_版本号不许复用(clean):
    _freeze("dup")
    with pytest.raises(rp.PackError) as e:
        _freeze("dup")
    assert "不许复用" in str(e.value)


def test_版本号和冻结人都必填(clean):
    with pytest.raises(rp.PackError):
        rp.freeze("", "测试")
    with pytest.raises(rp.PackError):
        rp.freeze(TEST_PREFIX + "noone", "")


def test_本体与库不同步时不许冻结(clean, monkeypatch):
    """页面上改过 YAML 还没重装时，库是旧投影、文件是新的。

    这时候冻结，说不清冻的是哪一份——所以挡住，并且把「怎么过」写进消息里。
    """
    real = oy.source_info()

    def fake():
        d = dict(real)
        d["loaded"] = dict(real["loaded"], in_sync=False, source_hash="deadbeef")
        return d

    monkeypatch.setattr(rp.onto_yaml, "source_info", fake)
    with pytest.raises(rp.PackError) as e:
        _freeze("nosync")
    assert "重装" in str(e.value)


def test_未签字不能发布(clean):
    r = _freeze("sign")
    with pytest.raises(rp.PackError) as e:
        rp.publish(r["release_id"], "")
    assert "签字人" in str(e.value)


def test_库里也挡着未签字发布(clean):
    """应用层挡一道，库里再挡一道。**确定性的闸失效而没人发现，比没有这道闸更危险。**"""
    import psycopg2
    r = _freeze("dbgate")
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.execute("UPDATE release SET status='published' WHERE release_id=%s",
                   (r["release_id"],))


def test_只有已发布的版本能切为当前(clean):
    r = _freeze("switch")
    with pytest.raises(rp.PackError) as e:
        rp.switch_current(r["release_id"], "测试")
    assert "已发布" in str(e.value)


def test_发布与生效是两件事(clean):
    """签字发布之后**不自动切为当前**：发布是入库，生效是切指针。"""
    before = rp.current_release()
    r = _freeze("twostep")
    rp.publish(r["release_id"], "签字人")
    after = rp.current_release()
    assert (before or {}).get("release_id") == (after or {}).get("release_id"), \
        "发布把当前指针悄悄改了"
    rp.switch_current(r["release_id"], "测试")
    assert rp.current_release()["release_id"] == r["release_id"]
    # 收尾：把当前指针还原回去，别把测试状态留在演示环境里
    if before:
        rp.switch_current(before["release_id"], "测试收尾")
    else:
        db.execute("DELETE FROM release_current")


def test_草稿能删已发布删不掉(clean):
    r1 = _freeze("discard-ok")
    rp.discard(r1["release_id"], "测试")
    assert not db.query_one("SELECT 1 FROM release WHERE release_id=%s", (r1["release_id"],))
    r2 = _freeze("discard-no")
    rp.publish(r2["release_id"], "签字人")
    with pytest.raises(rp.PackError) as e:
        rp.discard(r2["release_id"], "测试")
    assert "资产" in str(e.value)


def test_回滚要有原因并且指回的必须是已发布(clean):
    r = _freeze("rb")
    rp.publish(r["release_id"], "签字人")
    with pytest.raises(rp.PackError) as e:
        rp.rollback(r["release_id"], None, "测试", "")
    assert "原因" in str(e.value)
    draft = _freeze("rb-target-draft")
    with pytest.raises(rp.PackError):
        rp.rollback(r["release_id"], draft["release_id"], "测试", "指回草稿不行")
    with pytest.raises(rp.PackError) as e:
        rp.rollback(r["release_id"], r["release_id"], "测试", "指回自己")
    assert "自己" in str(e.value)


def test_回滚把当前指针指回旧版(clean):
    """回滚不是「取 published 里最新的一条」能表达的：旧版的 published_at 更早。"""
    before = rp.current_release()
    old = _freeze("rb-old")
    rp.publish(old["release_id"], "签字人")
    rp.switch_current(old["release_id"], "测试")
    new = _freeze("rb-new")
    rp.publish(new["release_id"], "签字人")
    rp.switch_current(new["release_id"], "测试")
    assert rp.current_release()["release_id"] == new["release_id"]
    rp.rollback(new["release_id"], old["release_id"], "测试", "新版判定口径有问题")
    assert rp.current_release()["release_id"] == old["release_id"]
    row = db.query_one("SELECT status, rollback_reason FROM release WHERE release_id=%s",
                       (new["release_id"],))
    assert row["status"] == "rolled_back" and row["rollback_reason"]
    if before:
        rp.switch_current(before["release_id"], "测试收尾")
    else:
        db.execute("DELETE FROM release_current")


# ─────────────────────────────────────────────────────────────────────────────
# 四、**核心口径：装载只动工作区**
# ─────────────────────────────────────────────────────────────────────────────
def test_装载动不了已发布的快照与当前指针(clean):
    """用户 2026-10-19 定的那一条，整个方案的地基。

    装载脚本会 DELETE 掉 created_by='asset_loader' 的规则再整批重建。
    原来它还会 `DELETE FROM release_current` 和整表清 release_item——
    于是「装载一次，生效版本就没了」。这条测试把这件事钉住。

    顺带验「哈希不含簿记字段」真的有用：重装之后 created_at 全变 now()，
    而工作区**重新采集**出来的规则层哈希必须和重装前一致。
    """
    from app.rules import loader

    before_cur = rp.current_release()
    r = _freeze("loader")
    rp.publish(r["release_id"], "签字人")
    rp.switch_current(r["release_id"], "测试")
    snap_before = db.query_one("""SELECT content_sha256, item_count FROM release_asset
                                   WHERE release_id=%s AND layer='规则'""", (r["release_id"],))
    ws_rule_sha_before = rp.collect()["规则"]["content_sha256"]
    n_rule_before = db.scalar("SELECT count(*) FROM rule WHERE version=1")

    loader.load_all(verbose=False)          # ← 真的跑一遍装载

    snap_after = db.query_one("""SELECT content_sha256, item_count FROM release_asset
                                  WHERE release_id=%s AND layer='规则'""", (r["release_id"],))
    assert snap_after == snap_before, "装载把已发布的快照改了"
    cur_after = rp.current_release()
    assert cur_after and cur_after["release_id"] == r["release_id"], \
        "装载清掉了 release_current——这正是用户否掉的行为"
    assert db.query_one("SELECT 1 FROM release WHERE release_id=%s AND status='published'",
                        (r["release_id"],)), "装载把已发布的版本删了"
    assert db.scalar("SELECT count(*) FROM release_item WHERE release_id=%s",
                     (r["release_id"],)) > 0, "装载清掉了已发布版本的差异清单"
    assert db.scalar("SELECT count(*) FROM rule WHERE version=1") == n_rule_before
    assert rp.collect()["规则"]["content_sha256"] == ws_rule_sha_before, \
        "重装后工作区规则层哈希变了——created_at 这类簿记字段又进哈希了"

    # 收尾：当前指针还原，装载脚本新建的工作区草稿留着（它每次装载都会自己重建）
    if before_cur:
        rp.switch_current(before_cur["release_id"], "测试收尾")
    else:
        db.execute("DELETE FROM release_current")


def test_工作区能看出哪几层变了(clean):
    """差异要按层给：改了 YAML 和改了限额表，影响面完全不同。"""
    w = rp.workspace()
    assert len(w["layers"]) == len(rp.LAYERS) == 9
    assert [x["layer"] for x in w["layers"] if x["is_triple"]] == list(rp.TRIPLE)
    assert all(len(x["content_sha256"]) == 64 for x in w["layers"])
    assert w["suggest_version"].startswith("rs-")
    assert all("how" in g for g in w["gates"]), "闸门没写「怎么过」"


def test_没有可比版本时整份算新增(clean):
    a = rp.collect()
    summary, items = rp.diff_against(a, None)
    assert summary["added"] == a["规则"]["item_count"]
    assert summary["modified"] == 0 and summary["retired"] == 0
    assert len(items) == a["规则"]["item_count"]


def test_冻结后改工作区不影响快照(clean):
    """直接改一条规则的名字，已发布快照必须一字不变。"""
    r = _freeze("frozen")
    rp.publish(r["release_id"], "签字人")
    sha0 = db.scalar("""SELECT content_sha256 FROM release_asset
                         WHERE release_id=%s AND layer='规则'""", (r["release_id"],))
    rid = db.scalar("SELECT rule_id FROM rule WHERE version=1 ORDER BY rule_id LIMIT 1")
    old_name = db.scalar("SELECT name FROM rule WHERE rule_id=%s AND version=1", (rid,))
    try:
        db.execute("UPDATE rule SET name=%s WHERE rule_id=%s AND version=1",
                   ("【测试】改过的名字", rid))
        assert rp.collect()["规则"]["content_sha256"] != sha0, "工作区改了但哈希没变，哈希是假的"
        assert db.scalar("""SELECT content_sha256 FROM release_asset
                             WHERE release_id=%s AND layer='规则'""",
                         (r["release_id"],)) == sha0, "改工作区把快照带着一起变了"
    finally:
        db.execute("UPDATE rule SET name=%s WHERE rule_id=%s AND version=1", (old_name, rid))


def test_快照是对外消费的唯一数据源(clean):
    """这条测试钉的是**写法**：/api/pub 不许读活表。

    读活表等于版本号是假的——活表随时在变。
    """
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "api", "pub.py"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app", "api", "pub.py")) else ""
    if not src:
        pytest.skip("消费面接口还没做（批 2）")
    for t in ("FROM rule", "FROM ontology_", "FROM process_", "FROM doc_lib_file"):
        assert t not in src, "消费面接口直接读了活表：%s" % t


def test_存储页把资产和工作区分开写():
    s = rp.storage()
    names = [x["table_name"] for x in s["items"]]
    assert "release_asset" in names and "rule" in names
    assert "工作区" in s["note"]
    for x in s["items"]:
        # 五列和全站对齐（前端 StorageRow 已经是这个形状，其他三页都在用）
        assert x["what"] and x["written_by"] and x["on_recalc"] and x["why"], x["table_name"]
    # 「重算时」这一列必须回答「装载会不会清」——这一栏的全部口径就在这
    cur = [x for x in s["items"] if x["table_name"] == "release_current"][0]
    assert "装载不碰" in cur["on_recalc"]


def test_哈希算在规范化文本上而不是repr():
    """用 repr/str 算哈希的话，dict 顺序一变哈希就变。"""
    obj = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
    assert rp.canon(obj) == '{"a":{"x":[3,4],"y":2},"b":1}'
    assert rp.sha_obj(obj) == hashlib.sha256(rp.canon(obj).encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 六、**生效版本只有一个定义**（2026-10-21）
#
# 这一组钉的是一次实测事故：同一个问题「哪一版在生效」有过三份实现——
#   engine.load_ruleset()  取 created_at 最新的**任意**版本
#   list_releases()        取 created_at 最新的**已发布**版本
#   workspace()/pub        只认 release_current 指针
# 于是库里有一个装载器刚建的草稿时，引擎按草稿的 rs-0.1 判，页面却把
# rs-20261021-01 标成「生效中（自动）」，页头还写着「现在没有生效版本」。
# 版本号要写进 cycle_log 复现历史结论，标错等于复现不了。
# ─────────────────────────────────────────────────────────────────────────────
def _no_pointer():
    """把当前指针摘掉并返回原值，收尾时还原。"""
    before = rp.current_release()
    db.execute("DELETE FROM release_current")
    return before


def _restore_pointer(before):
    if before:
        rp.switch_current(before["release_id"], "测试收尾")
    else:
        db.execute("DELETE FROM release_current")


def test_引擎不会把草稿当生效版(clean):
    """装载一次就多一个草稿，草稿永远是最新的——不能因此改掉生效版本。"""
    before = _no_pointer()
    try:
        pub = _freeze("eff-pub")
        rp.publish(pub["release_id"], "签字人")
        draft = _freeze("eff-draft")          # created_at 比已发布版更晚
        row, why = engine.effective_release()
        assert row["release_id"] != draft["release_id"], "草稿被当成了生效版"
        assert row["release_id"] == pub["release_id"] and row["status"] == "published"
        assert "已发布" in why
        assert engine.load_ruleset().release_id == pub["release_id"]
    finally:
        _restore_pointer(before)


def test_页面标的生效版就是引擎在用的那一版(clean):
    """发布管理页的 is_effective **不许自己算**，必须与引擎同源。"""
    before = _no_pointer()
    try:
        pub = _freeze("eff-ui")
        rp.publish(pub["release_id"], "签字人")
        _freeze("eff-ui-draft")
        lst = rp.list_releases()
        marked = [r for r in lst["items"] if r["is_effective"]]
        assert len(marked) == 1, "生效版必须且只能标一条，实际标了 %d 条" % len(marked)
        assert marked[0]["release_id"] == engine.effective_release()[0]["release_id"]
        assert lst["effective"]["release_id"] == marked[0]["release_id"]
        assert lst["effective"]["why"] and lst["policy"]["effective"]
        # 没点过「切为当前」时，要能看出高亮是自动认的，不是有人指定的
        assert marked[0]["is_current"] is False
        ws = rp.workspace()
        assert ws["current"]["release_id"] == marked[0]["release_id"]
        assert ws["current"]["is_pointer"] is False and ws["current"]["why"]
        # 页头原先在这种情况下写「现在没有生效版本」，而引擎正在按这一版判
        rp.switch_current(pub["release_id"], "测试")
        assert rp.workspace()["current"]["is_pointer"] is True
    finally:
        _restore_pointer(before)


def test_一版都没发布过时用草稿但要说清楚(monkeypatch):
    """新库里 loader 只建草稿（装载不等于发布），这一级不给就连启动自检都过不去。

    不动库：第三级阶梯要求「库里没有任何 published」，拿桩造这个局面。
    """
    def stub(sql, *a, **kw):
        if "release_current" in sql or "status = 'published'" in sql:
            return None
        return {"release_id": "rel-draft", "rule_set_version": "rs-0.1",
                "ontology_version": "0.1", "status": "draft"}

    monkeypatch.setattr(engine.db, "query_one", stub)
    row, why = engine.effective_release()
    assert row["release_id"] == "rel-draft"
    assert "草稿" in why, "用了草稿就必须在 why 里说明，否则没人知道结论上的版本号是草稿号"


def test_一条release都没有时直接报错(monkeypatch):
    monkeypatch.setattr(engine.db, "query_one", lambda *a, **kw: None)
    row, why = engine.effective_release()
    assert row is None and why
    with pytest.raises(RuntimeError) as e:
        engine.load_ruleset()
    assert "loader" in str(e.value)


def test_切为当前之后不重启也要生效(clean):
    """跑着的进程缓存了 RuleSet，切指针之后必须自己换掉。

    原先 /api/ai-review 的 _cache 全仓没有任何失效路径：页面显示切成功了，
    审核结果仍按旧版判，一直到重启才生效——「切为当前不好用」里最难发现的一半。
    """
    from app.api import ai_review
    before = _no_pointer()
    try:
        a = _freeze("cache-a")
        rp.publish(a["release_id"], "签字人")
        b = _freeze("cache-b")
        rp.publish(b["release_id"], "签字人")

        rp.switch_current(a["release_id"], "测试")
        ai_review._cache.clear()
        assert ai_review._load_engine()["release_id"] == a["release_id"]

        rp.switch_current(b["release_id"], "测试")          # 不重启
        c = ai_review._load_engine()
        assert c["release_id"] == b["release_id"], "缓存没跟着切 → 仍要重启才生效"
        assert c["ruleset"].release_id == b["release_id"]

        rp.switch_current(a["release_id"], "测试")          # 反向也要跟上
        assert ai_review._load_engine()["release_id"] == a["release_id"]
    finally:
        ai_review._cache.clear()
        _restore_pointer(before)
