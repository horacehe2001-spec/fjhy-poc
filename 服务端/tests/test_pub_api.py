# -*- coding: utf-8 -*-
"""对外消费面：版本可见性、包的可验证性，以及**旁路出口的脱敏**。

CLAUDE.md 第 8 条：安全审查要覆盖全部出口，不能只看主输出路径。
资产包是 application/zip，全局脱敏中间件**按 content-type 放行**——
它是个不经过主路径的出口，所以这里逐条验包内容。

第 9 条：误报和漏报同样致命。两个方向都测：
  漏报 → 包里出现真卡号 / 真手机号 / 闭集合里的真人名
  误报 → 规则名与本体元素名被打成星号（上一轮规则本体页整列星号就是这么来的）

测试数据一律 rs-TEST- 前缀，跑完删掉。
"""
import io
import json
import os
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, mask, release_pack as rp  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
TEST_PREFIX = "rs-TEST-"


def _cleanup():
    db.execute("DELETE FROM release WHERE rule_set_version LIKE %s", (TEST_PREFIX + "%",))


@pytest.fixture
def published():
    """一个已发布的测试版本。**不切为当前**——切指针会影响演示环境。"""
    _cleanup()
    r = rp.freeze(TEST_PREFIX + "pub", "测试冻结人")
    rp.publish(r["release_id"], "测试签字人")
    yield db.query_one("SELECT * FROM release WHERE release_id=%s", (r["release_id"],))
    _cleanup()


@pytest.fixture
def draft():
    _cleanup()
    r = rp.freeze(TEST_PREFIX + "draft", "测试冻结人")
    yield db.query_one("SELECT * FROM release WHERE release_id=%s", (r["release_id"],))
    _cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# 一、可见性
# ─────────────────────────────────────────────────────────────────────────────
def test_自描述接口把规矩都写出来():
    r = client.get("/api/pub/v1")
    assert r.status_code == 200
    d = r.json()
    # **实测踩过**：这个字段原来叫 name，经 HTTP 出去变成「福****…」。
    # 第一版这条测试只验了层和规矩，没验服务名本身，所以没拦住。
    assert "*" not in d["service_name"], "服务名被人名脱敏吃了：%s" % d["service_name"]
    assert d["triple_ontology"] == list(rp.TRIPLE)
    assert len(d["layers"]) == len(rp.LAYERS)
    # 没鉴权这件事必须摆在明面上，不许藏着
    assert "鉴权" in d["rules_of_the_road"]
    assert "没有鉴权" in d["rules_of_the_road"]["鉴权"]


def test_草稿不出门(draft):
    r = client.get("/api/pub/v1/releases/" + draft["rule_set_version"])
    assert r.status_code == 404
    assert "草稿不出门" in r.json()["detail"]
    r2 = client.get("/api/pub/v1/releases/%s/layers/rules" % draft["rule_set_version"])
    assert r2.status_code == 404


def test_已发布的版本能按版本号和发布号取(published):
    for key in (published["rule_set_version"], published["release_id"]):
        r = client.get("/api/pub/v1/releases/" + key)
        assert r.status_code == 200, key
        d = r.json()
        assert d["version"] == published["rule_set_version"]
        assert len(d["layers"]) == len(rp.LAYERS)
        assert [x["layer"] for x in d["layers"] if x["is_triple"]] == list(rp.TRIPLE)


def test_撤回的版本仍然给但要带标记(published):
    """404 掉只会让已集成的系统以为是网络问题，它需要知道「这一版被撤了」。"""
    rp.rollback(published["release_id"], None, "测试", "测试用：验证撤回标记")
    r = client.get("/api/pub/v1/releases/" + published["rule_set_version"])
    assert r.status_code == 200
    d = r.json()
    assert d["withdrawn"] is True and d["withdrawn_reason"]


def test_层名用中文或别名都取得到(published):
    v = published["rule_set_version"]
    a = client.get("/api/pub/v1/releases/%s/layers/rules" % v).json()
    b = client.get("/api/pub/v1/releases/%s/layers/规则" % v).json()
    assert a["content_sha256"] == b["content_sha256"]
    assert a["layer"] == "规则"
    bad = client.get("/api/pub/v1/releases/%s/layers/nope" % v)
    assert bad.status_code == 404


def test_三元本体三层都取得到(published):
    v = published["rule_set_version"]
    for alias, layer in (("ontology-business", "业务本体"), ("ontology-rule", "规则本体"),
                         ("ontology-process", "流程本体")):
        d = client.get("/api/pub/v1/releases/%s/layers/%s" % (v, alias)).json()
        assert d["layer"] == layer and d["item_count"] > 0


def test_本体源文件带注释原文(published):
    """投影只剩结构，注释才是本体资产最贵的部分。"""
    d = client.get("/api/pub/v1/releases/%s/layers/onto-source"
                   % published["rule_set_version"]).json()
    text = d["payload"]["text"]
    assert "#" in text, "源文件原文里一条注释都没有，八成冻的是解析结果"
    assert d["payload"]["lines"] > 800
    assert d["payload"]["written_by"], "「谁写的」不许含糊过去"


def test_消费有台账(published):
    before = db.scalar("SELECT count(*) FROM release_access_log")
    # **X-Client 必须是 ASCII**：HTTP 头按 ISO-8859-1 传，塞中文到不了对端——
    # 第一版这条测试就是写了中文，httpx 直接拒绝编码。写在自描述接口里了。
    client.get("/api/pub/v1/releases/" + published["rule_set_version"],
               headers={"x-client": "fjhy-test-consumer"})
    # **不用 ORDER BY at DESC**：now() = 事务开始时刻，长事务晚提交 at 反而更早，
    # 于是「取最后插入的一行」就会取到别的测试的行。
    # 实测（2026-10-20）600 行里有 12 对 id 递增但 at 倒挂，差值 ~2min 45sec。
    # 换成 id 序列排序：bigint sequence 是真单调递增，和事务时长无关。
    # 同时过滤 client：别的测试也会打这个接口（走 user-agent 记成 testclient），
    # 在同一组合里跑会多一行，两重保险都要。
    rows = db.query("""SELECT release_id, resource, client FROM release_access_log
                        WHERE client = 'fjhy-test-consumer'
                        ORDER BY id DESC LIMIT 1""")
    assert db.scalar("SELECT count(*) FROM release_access_log") == before + 1
    assert rows[0]["client"] == "fjhy-test-consumer"
    # **只记元数据**：不许出现请求参数或响应内容的列
    cols = {c["column_name"] for c in db.query(
        """SELECT column_name FROM information_schema.columns
            WHERE table_name='release_access_log'""")}
    assert not (cols & {"ip", "query", "params", "body", "response"})


# ─────────────────────────────────────────────────────────────────────────────
# 二、资产包
# ─────────────────────────────────────────────────────────────────────────────
def _zip_of(version):
    r = client.get("/api/pub/v1/releases/%s/bundle" % version)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    return r.content, zipfile.ZipFile(io.BytesIO(r.content))


def test_包是完好的zip而且中间件没把它改坏(published):
    """全局脱敏中间件只重写 application/json。它要是动了 zip，这里会直接崩。"""
    raw, z = _zip_of(published["rule_set_version"])
    assert raw[:2] == b"PK"
    assert z.testzip() is None
    names = z.namelist()
    assert "MANIFEST.json" in names and "README.md" in names
    assert len(names) == len(rp.LAYERS) + 2


def test_清单里每个文件的哈希都对得上(published):
    """自己给的校验值自己先得对——不然这个字段只是装饰。"""
    _raw, z = _zip_of(published["rule_set_version"])
    m = json.loads(z.read("MANIFEST.json").decode("utf-8"))
    import hashlib
    for f in m["files"]:
        data = z.read(f["file_name"])
        assert hashlib.sha256(data).hexdigest() == f["sha256"], f["file_name"]
        assert len(data) == f["bytes"], f["file_name"]


def test_清单走JSON出口时文件名不许被脱敏(published):
    """**zip 里和 JSON 里必须是同一份清单。**

    zip 不经脱敏中间件，JSON 经——所以同一个字段在两条路上可能一个完整
    一个变星号。那种不一致最难查：集成方照文档写的代码，走下载能跑，
    走接口就报「找不到这个文件」。
    """
    v = published["rule_set_version"]
    api_m = client.get("/api/pub/v1/releases/%s/bundle-manifest" % v).json()
    _raw, z = _zip_of(v)
    zip_m = json.loads(z.read("MANIFEST.json").decode("utf-8"))
    names_api = [f["file_name"] for f in api_m["files"]]
    names_zip = [f["file_name"] for f in zip_m["files"]]
    assert names_api == names_zip
    assert not [n for n in names_api if "*" in n], "清单文件名被脱敏吃了：%s" % names_api


def test_包级校验算在内容上不是zip字节上(published):
    """zip 带时间戳，同样内容两次打包字节可能不同——所以包级校验不能算在 zip 上。"""
    v = published["rule_set_version"]
    raw1, z1 = _zip_of(v)
    raw2, _z2 = _zip_of(v)
    m = json.loads(z1.read("MANIFEST.json").decode("utf-8"))
    assert m["release"]["bundle_sha256"] == published["bundle_sha256"]
    # 时间戳钉死了，所以字节其实也稳定；但口径仍然是「校验算在内容上」
    assert len(raw1) == len(raw2)


def test_包里没有客户原始文档与业务数据(published):
    """发布的是口径资产。一张单据都不许进去。"""
    _raw, z = _zip_of(published["rule_set_version"])
    files = json.loads(z.read("01_文件清单.json").decode("utf-8"))
    assert files["files"], "文件清单是空的"
    for f in files["files"]:
        assert "body" not in f and "abs_path" not in f
    all_text = "".join(z.read(n).decode("utf-8") for n in z.namelist())
    assert "bill_no" not in all_text, "业务单据字段进了发布包"


def test_包里的readme说清了没有什么(published):
    _raw, z = _zip_of(published["rule_set_version"])
    readme = z.read("README.md").decode("utf-8")
    for must in ("三元本体", "没有", "原始文档", "bundle_sha256", "鉴权"):
        assert must in readme, must


# ─────────────────────────────────────────────────────────────────────────────
# 三、**旁路出口的脱敏**：漏报与误报两个方向都测
# ─────────────────────────────────────────────────────────────────────────────
def test_包里不许漏出真人名与卡号(published):
    """漏报方向。闭集合人名来自库里的真实数据，由 mask.find_leaks 判。"""
    _raw, z = _zip_of(published["rule_set_version"])
    for n in z.namelist():
        if not n.endswith(".json"):
            continue
        obj = json.loads(z.read(n).decode("utf-8"))
        leaks = mask.find_leaks(obj)
        assert not leaks, "%s 里漏了：%s" % (n, leaks[:3])


def test_包里的规则名与本体名不许被打成星号(published):
    """误报方向。**这是上一轮刚踩过的坑**：

    按字段名选策略的那套会把叫 name 的字段一律当人名，
    于是规则本体页「名称」整列变成「付**********」。
    包里用的是按内容判定的 mask_text，规则名与本体名必须完整。
    """
    _raw, z = _zip_of(published["rule_set_version"])
    rules = json.loads(z.read("02_规则.json").decode("utf-8"))["rules"]
    assert rules
    starred = [r["rule_name"] for r in rules if "*" in (r["rule_name"] or "")]
    assert not starred, "规则名被脱敏吃了：%s" % starred[:3]
    biz = json.loads(z.read("05_业务本体.json").decode("utf-8"))
    for key, field in (("entities", "entity_name"), ("concepts", "concept_name"),
                       ("enums", "enum_name")):
        bad = [x[field] for x in biz[key] if "*" in (x[field] or "")]
        assert not bad, "%s 被脱敏吃了：%s" % (field, bad[:3])
    proc = json.loads(z.read("07_流程本体.json").decode("utf-8"))
    assert not [n["node_name"] for n in proc["nodes"] if "*" in (n["node_name"] or "")]


def test_包里的脱敏对真卡号真的生效(published):
    """正向验一次脱敏函数本身，别让「包里没星号」变成「脱敏根本没跑」。"""
    from app.api import pub
    obj = {"rule_name": "付款账号一致性", "remark": "收款账号 6222621234567896181"}
    out = pub._scrub_for_bundle(obj)
    assert out["rule_name"] == "付款账号一致性", "规则名被误伤"
    assert "6222621234567896181" not in out["remark"], "卡号没脱"
    assert "*" in out["remark"]


def test_消费面不读活表():
    """读活表等于版本号是假的。这条钉的是**写法**，不是行为。"""
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "app", "api", "pub.py"), encoding="utf-8").read()
    for t in ("FROM rule ", "FROM rule\n", "FROM ontology_", "FROM process_",
              "FROM doc_lib_file"):
        assert t not in src, "消费面接口直接读了活表：%s" % t
    assert "FROM release_asset" in src


def test_没有生效版本时current给404而不是猜一个(published):
    """「取 published 里最新的一条」是错的：回滚后它会选到被回滚掉的那一版。"""
    had = rp.current_release()
    if had:
        db.execute("DELETE FROM release_current")
    try:
        r = client.get("/api/pub/v1/current")
        assert r.status_code == 404
    finally:
        if had:
            rp.switch_current(had["release_id"], "测试收尾")


# ── 下载头：非 ASCII 的名字不许把响应打成 500 ───────────────────────────────
# **实测踩到（2026-10-20）**：用带中文的 release 名拉 /bundle，
# 响应直接 500 —— Starlette 按 latin-1 编响应头，中文字符抛
# UnicodeEncodeError。报出来是「服务器内部错误」，看不出是文件名的事。
# 这是个旁路出口：主路径（JSON 响应）全是 ASCII 头，永远测不到它。
def test_下载头对非ASCII名字不炸():
    from app.api import pub
    cd = pub._attachment("fjhy-闽港财智20号-v1.zip")
    cd.encode("latin-1")                        # 炸了就是 500
    assert "filename*=UTF-8''" in cd, cd        # 真名用 RFC 5987 给
    assert 'filename="' in cd, cd               # 老客户端还要一个 ASCII 退化名


def test_纯ASCII名字原样保留():
    """**误报方向。** 正常的 rs-0.1 不许被改名——改了就是把
    能用的下载名弄坏，而没人会去核对下载下来的文件叫什么。"""
    from app.api import pub
    cd = pub._attachment("fjhy-rs-0.1.zip")
    assert 'filename="fjhy-rs-0.1.zip"' in cd, cd


def test_自定义头也过一遍():
    """release_id 同样来自人起的名字，同样会让整个响应 500。
    **只修文件名是只修了一半。**"""
    from app.api import pub
    for v in ["rel-中文-001", 'has"quote', None, ""]:
        pub._header_safe(v, "release").encode("latin-1")
    assert pub._header_safe("rel-0007", "release") == "rel-0007"
    assert pub._header_safe("", "release") == "release"


def test_下载接口真的用了这两个函数():
    """行为测试测不到「哪个头用了哪个函数」，而漏掉一个头就又是 500。"""
    import inspect
    from app.api import pub
    src = inspect.getsource(pub.bundle)
    assert "_attachment(" in src, src
    assert src.count("_header_safe(") >= 2, src
    assert "filename=" not in src.replace("_attachment(", ""), "又有人手拼文件名了"
