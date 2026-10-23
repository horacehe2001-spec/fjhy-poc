# -*- coding: utf-8 -*-
"""大模型配置：加密可逆性、默认模型唯一性、测试结果分类、出口不漏 Key。

这组测试**一次都不出网**。
出网的那两个接口（/test 和 /llm-probe）在这里用两种方式验：
  · 没填 Key 时必须在发请求之前就返回 no_key（验「不出网」本身）
  · 其余路径用假 httpx 客户端喂构造好的响应（验解析与分类）
理由：真出网的测试会因为别人的网络、别人的额度而红，
而「因为环境而红的测试」会让人养成忽略红灯的习惯——那比没有测试坏。

测试数据一律用 FJHY-TEST-LLM- 前缀，跑完删掉（纪律第 12 条：
验证动作产生的数据要一眼可辨、事后好清理）。
"""
import os
import sys

import psycopg2
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, mask                      # noqa: E402
from app.llm import client as llmc            # noqa: E402
from app.llm import crypto, presets           # noqa: E402
from app.main import app                      # noqa: E402

api = TestClient(app, raise_server_exceptions=False)

PREFIX = "FJHY-TEST-LLM-"


@pytest.fixture()
def tmp_cfg():
    """造几条测试配置，用完删干净。"""
    made = []

    def make(cid_suffix, purpose=None, base_url="http://127.0.0.1:9/v1",
             model="test-model", api_key=None, enabled=True):
        cid = PREFIX + cid_suffix
        db.execute("""INSERT INTO llm_config
            (config_id, name, provider, base_url, model_name, api_key_cipher,
             api_key_mask, purpose, enabled)
            VALUES (%s,%s,'openai_compatible',%s,%s,%s,%s,%s,%s)
            ON CONFLICT (config_id) DO UPDATE SET
              base_url=EXCLUDED.base_url, model_name=EXCLUDED.model_name,
              purpose=EXCLUDED.purpose, enabled=EXCLUDED.enabled,
              api_key_cipher=EXCLUDED.api_key_cipher,
              api_key_mask=EXCLUDED.api_key_mask, is_default=FALSE""",
            (cid, cid, base_url, model,
             crypto.encrypt(api_key) if api_key else None,
             mask.key_mask(api_key) if api_key else None, purpose, enabled))
        made.append(cid)
        return cid

    yield make
    for cid in made:
        db.execute("DELETE FROM llm_config WHERE config_id=%s", (cid,))


# ─────────────────────────────────────────────────────────────────────────────
# 一 · 厂商预置
# ─────────────────────────────────────────────────────────────────────────────
def test_预置装载完整():
    _, items = presets.read_yaml()
    n = db.scalar("SELECT count(*) FROM llm_provider_preset") or 0
    assert n == len(items), "库里 %d 条，文件 %d 条" % (n, len(items))


def test_核对过的和没核对的必须分得开():
    """verified_on 为空 = 地址没对过官方文档。

    这件事必须能从接口上读出来。混在一个下拉里不作区分，
    用户配不通时会先怀疑自己的 Key、再怀疑网络，最后才想到是地址错——
    那段时间全是白花的。
    """
    r = api.get("/api/sys/llm-presets")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verified"] + d["unverified"] == len(d["items"])
    assert d["verified"] >= 1, "一条都没核对过？那这份预置不该交付"
    for it in d["items"]:
        if not it["verified_on"]:
            assert it["caveat"], \
                "%s 没核对过却没写原因，用户无从判断可信度" % it["preset_id"]


def test_预置的型号名只是建议不是承诺():
    """型号漂移很快（核对当天实测：智谱已到 glm-5.3、Kimi 已到 kimi-k3）。

    把型号写死在预置里，半年后这张表就是一张**错的**下拉菜单，
    而错的下拉比空的下拉坏：用户会以为自己选了个有效型号。
    所以接口必须明说 suggest_model 是建议值。
    """
    d = api.get("/api/sys/llm-presets").json()
    assert "建议" in d["note"] and "拉取型号" in d["note"]


def test_火山方舟的坑写在明面上():
    """它的 model 要填推理接入点 ID，不是型号名。

    不写出来，用户会反复试型号名、一直 404，而错误信息不会告诉他原因。
    """
    row = db.query_one("SELECT caveat, model_list FROM llm_provider_preset "
                       "WHERE preset_id='volcengine_ark'")
    assert row, "火山方舟预置没了？"
    assert "ep-" in row["caveat"] and "接入点" in row["caveat"]
    assert row["model_list"] is False, \
        "方舟拉回来的型号列表不是能直接填的 model 值，不该把这个按钮给用户"


# ─────────────────────────────────────────────────────────────────────────────
# 二 · Key 的加密必须可逆（原设计是 sha256，做不到）
# ─────────────────────────────────────────────────────────────────────────────
def test_Key_加密可逆():
    """这一条是整个「测试连通」功能的前提。

    原来存的是 "enc:" + sha256(plain)，那是摘要不是密文——取不回明文，
    调厂商就带不了 Authorization。所以这一条红了，测试按钮必然是摆设。
    """
    plain = "sk-test-" + "a" * 24
    c = crypto.encrypt(plain)
    assert c and c.startswith("fer1:")
    assert plain not in c, "密文里能看到明文，那不叫加密"
    got, why = crypto.decrypt(c)
    assert why is None and got == plain


def test_旧的哈希格式给出可操作的原因而不是崩掉():
    got, why = crypto.decrypt("enc:" + "0" * 64)
    assert got is None
    assert "重新填" in why, "原因得告诉人下一步干什么，而不是只说解密失败：%s" % why


def test_解不开时也要给出可操作的原因():
    from cryptography.fernet import Fernet
    alien = "fer1:" + Fernet(Fernet.generate_key()).encrypt(b"x").decode()
    got, why = crypto.decrypt(alien)
    assert got is None and "重新填" in why


def test_没填Key时返回原因而不是空串():
    """返回空串会让调用方带着空 Authorization 去请求，厂商回 401，
    用户看到「Key 错了」——而真实原因是这条压根没填 Key。"""
    got, why = crypto.decrypt(None)
    assert got is None and why


def test_密钥来源如实回报():
    """POC 默认把密钥写在 var/llm_secret.key，和密文同一台机器。

    这个前提必须在接口上说出来，否则交付时没人知道。
    """
    d = api.get("/api/sys/llm-key-status").json()
    assert d["ok"] is True
    if not d["production_ready"]:
        assert d["note"], "不够生产用却不说明，等于默认它够用"


# ─────────────────────────────────────────────────────────────────────────────
# 三 · 默认模型：按用途各一个
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def keep_real_defaults():
    """跑这组测试期间，**把库里真实的默认模型设置护住**，跑完原样放回去。

    这条护栏是实测逼出来的，而且根因不在新加的测试，在两条老测试：
    `test_设为默认会把同用途的旧默认清掉` 建两条 extract 配置、调接口设默认，
    接口**按设计**清掉了库里那条真的；测试结束删掉自己的行，
    于是 extract 用途就没有默认模型了。rule_judge 同理。
    这个副作用一直存在，只是以前库里没有真配置，看不出来——
    直到有人在界面上配好了模型，再跑一次测试，配置就没了，而且没有任何提示。

    **测试不许改坏被测系统的真实状态。** 做成 autouse 是故意的：
    靠每条测试自己记得挂夹具，迟早有人忘，而忘的那次就是配置被清掉的那次。
    """
    saved = [r["config_id"] for r in
             db.query("SELECT config_id FROM llm_config WHERE is_default")]
    yield
    db.execute("UPDATE llm_config SET is_default=FALSE WHERE is_default")
    restored, failed = [], []
    for cid in saved:
        try:
            if db.execute("UPDATE llm_config SET is_default=TRUE WHERE config_id=%s",
                          (cid,)):
                restored.append(cid)
        except Exception as e:                                 # noqa: BLE001
            failed.append("%s（%s）" % (cid, e))
    # 恢复不了要**吵**，不能静默——静默意味着下次有人在界面上发现配置没了，
    # 却完全不知道是测试干的。
    assert not failed, "默认模型没能恢复：%s" % "；".join(failed)


@pytest.fixture
def free_slots():
    """把「某用途的默认模型」这个位置腾空，让下面两条测试能直接 UPDATE。

    它们验的是**库级唯一约束**，必须直接写库——走接口就变成在测应用层
    那段「先清旧的再设新的」，不是在测约束本身。
    收尾由 keep_real_defaults（module 级）统一负责，这里只管腾位置。
    """
    db.execute("UPDATE llm_config SET is_default=FALSE WHERE is_default")
    yield


def test_同一用途不许有两条默认(tmp_cfg, free_slots):
    """库级约束，不靠应用层自觉。

    应用层的「先清旧的再设新的」只要有一条路径忘了清，
    就会出现两条默认，而取默认的 SQL 用 LIMIT 1——
    于是行为变成「看哪条先被扫到」，表现为间歇性地用错模型。
    """
    a = tmp_cfg("dup-a", purpose="rule_judge")
    b = tmp_cfg("dup-b", purpose="rule_judge")
    db.execute("UPDATE llm_config SET is_default=TRUE WHERE config_id=%s", (a,))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.execute("UPDATE llm_config SET is_default=TRUE WHERE config_id=%s", (b,))


def test_不同用途可以各有一条默认(tmp_cfg, free_slots):
    """读图要多模态、判规则要长文本推理，全局只允许一个默认就会
    出现拿纯文本模型去读发票图——它不报错，会编一段看起来合理的内容。"""
    a = tmp_cfg("pur-vision", purpose="vision_read")
    b = tmp_cfg("pur-judge", purpose="rule_judge")
    db.execute("UPDATE llm_config SET is_default=TRUE WHERE config_id IN (%s,%s)", (a, b))
    n = db.scalar("SELECT count(*) FROM llm_config WHERE is_default "
                  "AND config_id LIKE %s", (PREFIX + "pur-%",))
    assert n == 2


def test_设为默认会把同用途的旧默认清掉(tmp_cfg):
    a = tmp_cfg("sw-a", purpose="extract")
    b = tmp_cfg("sw-b", purpose="extract")
    assert api.post("/api/sys/llm-configs/%s/default" % a).status_code == 200
    r = api.post("/api/sys/llm-configs/%s/default" % b)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["replaced"] and d["replaced"]["config_id"] == a, \
        "没说清换掉了谁。默认模型是会影响所有判定结果的开关，换了谁必须讲出来"
    assert db.scalar("SELECT is_default FROM llm_config WHERE config_id=%s", (a,)) is False
    assert db.scalar("SELECT is_default FROM llm_config WHERE config_id=%s", (b,)) is True


def test_没测过就设为默认要给出警告(tmp_cfg):
    """设成默认不等于能用。不提醒，用户会以为配完就通了。"""
    cid = tmp_cfg("warn-untested", purpose="rule_judge")
    d = api.post("/api/sys/llm-configs/%s/default" % cid).json()
    assert "还没测试过" in (d["warning"] or "")


def test_没填用途的配置不能设为默认(tmp_cfg):
    cid = tmp_cfg("nopurpose", purpose=None)
    r = api.post("/api/sys/llm-configs/%s/default" % cid)
    assert r.status_code == 400 and "用途" in r.json()["detail"]


def test_停用的配置不能设为默认(tmp_cfg):
    """设成默认又停用，等于默认模型不可用，而界面上看着是有的。"""
    cid = tmp_cfg("disabled", purpose="rule_judge", enabled=False)
    r = api.post("/api/sys/llm-configs/%s/default" % cid)
    assert r.status_code == 400 and "停用" in r.json()["detail"]


def test_缺默认模型要显式报出来(tmp_cfg):
    """没有默认模型时，该用途的规则只输出「待人工：未调用模型」，不报错。

    不在列表接口里把这件事说出来，看起来就像模型判过了。
    """
    d = api.get("/api/sys/llm-configs").json()
    assert "missing_defaults" in d
    assert set(d["missing_defaults"]) <= {"vision_read", "rule_judge", "extract"}
    for p in d["purposes"]:
        assert "key" in p and "default" in p


# ─────────────────────────────────────────────────────────────────────────────
# 四 · 出口：Key 一个字符都不许出去
# ─────────────────────────────────────────────────────────────────────────────
def test_读接口不把密文当字段回出来(tmp_cfg):
    """不是「查询时记得去掉」——那种约定迟早有人忘，而忘的那次就是泄露。

    这一条查的是**响应里的字段名**，不是源码里的字符串。
    CFG_COLS 里确实出现了 api_key_cipher，但它只在一个 CASE 表达式里
    用来派生 key_format（Key 属于哪一类格式），并不作为输出列。
    查字符串会把这种正当用法判成泄露——**误报和漏报一样致命**：
    一条红掉的安全测试如果来由讲不通，下一次真红的时候就没人当真了。
    """
    from app.api import sys_llm
    cid = tmp_cfg("colcheck", api_key="sk-" + "9" * 30)
    row = db.query_one("SELECT %s FROM llm_config WHERE config_id=%%s"
                       % sys_llm.CFG_COLS, (cid,))
    assert row is not None
    assert "api_key_cipher" not in row, "密文被当成输出列回出来了"
    assert row["key_format"] == "ok", "派生字段没工作"
    # 响应层面再确认一次（接口会不会自己又加回来）
    body = api.get("/api/sys/llm-configs").json()
    for it in body["items"]:
        assert "api_key_cipher" not in it


def test_配置列表不回密文也不回明文(tmp_cfg):
    tmp_cfg("leak-check", purpose="rule_judge", api_key="sk-" + "b" * 32)
    body = api.get("/api/sys/llm-configs").text
    assert "api_key_cipher" not in body
    assert "fer1:" not in body, "密文串本身也不该出接口"
    assert "sk-bbbb" not in body


def test_厂商回显的Key会被抹掉():
    """厂商 4xx 报文**经常把 Authorization 头原样回显**。

    这段错误原文要显示给用户看（不显示就没法排错），
    所以它是又一个出口，必须过同一套规则。
    """
    key = "sk-" + "c" * 30
    raw = 'invalid api key: "Bearer %s" please check' % key
    out = llmc._scrub(raw, key)
    assert key not in out and "sk-cccc" not in out


def test_库里有拒绝写入含Key文本的最后防线(tmp_cfg):
    cid = tmp_cfg("ck-nokey", purpose="rule_judge")
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.execute("""INSERT INTO llm_test_log (config_id, kind, ok, message)
                      VALUES (%s,'chat',FALSE,%s)""",
                   (cid, "boom sk-" + "d" * 20))


# ─────────────────────────────────────────────────────────────────────────────
# 五 · 测试结果：既不许误报"通了"，也不许把能说清的失败说成"调用失败"
# ─────────────────────────────────────────────────────────────────────────────
def test_没填Key时不出网():
    """这一条验的是「没发请求」本身：如果它真出了网，
    对 127.0.0.1:9 的连接会报 connect 失败，err_kind 就不是 no_key。"""
    res = llmc.chat_ping({"base_url": "https://example.invalid/v1",
                          "model_name": "m", "timeout_s": 2})
    assert res.ok is False and res.err_kind == "no_key"
    assert res.latency_ms is None, "有耗时说明真发了请求"


def test_失败分类按下一步该干什么分():
    cases = [
        (401, "", "auth"),          # 去厂商控制台
        (403, "", "auth"),
        (429, "", "quota"),
        (200, "insufficient balance", "quota"),
        (404, "", "model_not_found"),
        (200, "model not found", "model_not_found"),
        (400, "", "http"),
        (500, "", "http"),
    ]
    for status, body, want in cases:
        kind, msg = llmc._classify_http(status, body)
        assert kind == want, "HTTP %s / %r 分到了 %s，期望 %s" % (status, body, kind, want)
        assert msg, "分了类却没给话说"


def test_超时和DNS要分开():
    import httpx
    k1, _ = llmc._classify_transport(httpx.ConnectTimeout("timed out"))
    k2, _ = llmc._classify_transport(httpx.ConnectError("getaddrinfo failed"))
    assert k1 == "timeout" and k2 == "dns", \
        "超时是网络慢，DNS 是地址错，混在一起用户不知道该改哪个"


class _FakeResp(object):
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient(object):
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self._resp

    def post(self, url, json=None):
        return self._resp


def _patch(monkeypatch, resp):
    monkeypatch.setattr(llmc, "_client", lambda cfg, key: _FakeClient(resp))


CFG = {"base_url": "https://x.example/v1", "model_name": "glm-5.3",
       "api_key": "sk-" + "e" * 30, "timeout_s": 5}


def test_HTTP200但模型没说话算失败(monkeypatch):
    """网关在 Key 无效时也可能回 200，body 里放一个错误对象。

    只看状态码的测试会显示「连通正常」，然后跑批时全部失败——
    那种假的连通性检查比没有检查更坏。
    """
    _patch(monkeypatch, _FakeResp(200, {"choices": [{"message": {"content": ""}}]},
                                  text='{"error":"bad key"}'))
    res = llmc.chat_ping(CFG)
    assert res.ok is False and res.err_kind == "parse"
    assert "200" in res.message


def test_通了要带回模型真说的话(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"model": "glm-5.3",
                                        "choices": [{"message": {"content": "收到"}}]}))
    res = llmc.chat_ping(CFG)
    assert res.ok is True and res.reply_excerpt == "收到", \
        "要看见模型真的说了话，那段文字就是证据"


def test_型号被静默降级要提示(monkeypatch):
    """配的 glm-5.3，厂商回的是 glm-4-flash。

    不比对就会拿一个降级后的结果去评估配置型号的能力。
    """
    _patch(monkeypatch, _FakeResp(200, {"model": "glm-4-flash",
                                        "choices": [{"message": {"content": "收到"}}]}))
    res = llmc.chat_ping(CFG)
    assert res.ok is True
    assert "glm-4-flash" in res.message and "不一致" in res.message


def test_分段数组形式的回复也能解(monkeypatch):
    """有的厂商回的 content 是分段数组而不是字符串。

    解不出来会被判成「HTTP 200 但回复为空」——
    那是**误报**：明明通了却说没通，用户会去改根本没问题的配置。
    """
    _patch(monkeypatch, _FakeResp(200, {"model": "glm-5.3", "choices": [
        {"message": {"content": [{"type": "text", "text": "收"},
                                 {"type": "text", "text": "到"}]}}]}))
    res = llmc.chat_ping(CFG)
    assert res.ok is True and res.reply_excerpt == "收到"


def test_列型号返回排好序的ID(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"data": [{"id": "b"}, {"id": "a"}]}))
    res = llmc.list_models(CFG)
    assert res.ok is True and res.models == ["a", "b"]


def test_不支持列型号的家要说清楚而不是报个解析错(monkeypatch):
    _patch(monkeypatch, _FakeResp(200, {"object": "list"}, text='{"object":"list"}'))
    res = llmc.list_models(CFG)
    assert res.ok is False and res.err_kind == "parse"
    assert "不支持" in res.message


def test_测试结果会落历史(tmp_cfg, monkeypatch):
    """只存「最近一次」只能回答「现在通不通」。
    排错要回答的是「从哪天开始不通的」。"""
    cid = tmp_cfg("hist", purpose="rule_judge", api_key="sk-" + "f" * 30)
    _patch(monkeypatch, _FakeResp(200, {"model": "test-model",
                                        "choices": [{"message": {"content": "收到"}}]}))
    r = api.post("/api/sys/llm-configs/%s/test?kind=chat" % cid)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    h = api.get("/api/sys/llm-configs/%s/tests" % cid).json()
    assert h["total"] >= 1 and h["items"][0]["reply_excerpt"] == "收到"
    assert db.scalar("SELECT last_test_ok FROM llm_config WHERE config_id=%s", (cid,)) is True


def test_测试请求里不带任何业务数据(tmp_cfg, monkeypatch):
    """不拿真实报账单去试一个还没验证过的地址。"""
    cid = tmp_cfg("nobiz", purpose="rule_judge", api_key="sk-" + "0" * 30)
    sent = {}

    class _Cap(_FakeClient):
        def post(self, url, json=None):
            sent.update(json or {})
            return self._resp

    monkeypatch.setattr(llmc, "_client", lambda cfg, key: _Cap(
        _FakeResp(200, {"model": "test-model",
                        "choices": [{"message": {"content": "收到"}}]})))
    api.post("/api/sys/llm-configs/%s/test?kind=chat" % cid)
    body = str(sent)
    assert llmc.PING_PROMPT in body
    for bad in ("FJPG-BX", "差旅", "报账", "住宿"):
        assert bad not in body, "测试请求里出现了业务内容：%s" % bad


# ─────────────────────────────────────────────────────────────────────────────
# 六 · 用途与模型能力的交叉校验（整个模块里最重要的一条）
# ─────────────────────────────────────────────────────────────────────────────
def test_读票据图配纯文本模型必须警告():
    """纯文本模型拿到图**不会报错**。

    它会按 prompt 里的文字线索编一段看起来合理的内容：
    HTTP 200、有回复、格式正确、读起来像真的，没有任何技术信号。
    下游拿它当「已核验的票面信息」，就成了「AI 看过发票了」而其实它没看见图。
    """
    from app.api.sys_llm import modality_warning
    w = modality_warning("vision_read", "deepseek")        # DeepSeek 预置是 text
    assert w, "纯文本模型用于读图却没有警告"
    assert "编" in w, "文案必须点明「会编」这个后果，不然会被当成装饰：%s" % w


def test_读图配多模态不该报警告():
    from app.api.sys_llm import modality_warning
    assert modality_warning("vision_read", "aliyun_bailian_vision") == ""


def test_判不出能力时也要提醒():
    """手动填地址时判不出来。静默放过等于把「不知道」当成「没问题」。"""
    from app.api.sys_llm import modality_warning
    assert modality_warning("vision_read", None)
    assert modality_warning("vision_read", "不存在的预置")


def test_其它用途不受这条校验影响():
    from app.api.sys_llm import modality_warning
    assert modality_warning("rule_judge", "deepseek") == ""
    assert modality_warning("extract", "deepseek") == ""


def test_保存时就要给出能力不匹配的警告(tmp_cfg):
    """等到跑批时才发现就太晚了——那时已经产出了一批编造的读图结论。"""
    r = api.post("/api/sys/llm-configs", json={
        "config_id": PREFIX + "mismatch", "name": "x",
        "preset_id": "deepseek", "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat", "purpose": "vision_read"})
    assert r.status_code == 200, r.text
    try:
        assert r.json()["modality_warning"], "保存一个纯文本模型做读图用途，却没有警告"
    finally:
        db.execute("DELETE FROM llm_config WHERE config_id=%s", (PREFIX + "mismatch",))


# ─────────────────────────────────────────────────────────────────────────────
# 七 · Key 格式要逐条可见
# ─────────────────────────────────────────────────────────────────────────────
def test_每条配置都回Key的存储格式(tmp_cfg):
    """key-status 只给一个总数「有 N 条是旧格式」，

    用户看得见数量却看不出是哪几条，等于知道有问题但不知道去改谁。
    """
    a = tmp_cfg("kf-none")                                   # 没填 Key
    b = tmp_cfg("kf-ok", api_key="sk-" + "1" * 30)           # 新格式
    cid = PREFIX + "kf-legacy"
    db.execute("""INSERT INTO llm_config
        (config_id, name, provider, base_url, model_name, api_key_cipher, purpose)
        VALUES (%s,%s,'openai_compatible','http://127.0.0.1:9/v1','m',%s,'rule_judge')
        ON CONFLICT (config_id) DO UPDATE SET api_key_cipher=EXCLUDED.api_key_cipher""",
        (cid, cid, "enc:" + "0" * 64))
    try:
        rows = {r["config_id"]: r["key_format"]
                for r in api.get("/api/sys/llm-configs").json()["items"]}
        assert rows[a] == "none"
        assert rows[b] == "ok"
        assert rows[cid] == "legacy"
    finally:
        db.execute("DELETE FROM llm_config WHERE config_id=%s", (cid,))


# ─────────────────────────────────────────────────────────────────────────────
# 八 · 启用 / 停用
# ─────────────────────────────────────────────────────────────────────────────
def test_停用默认模型要说清后果(tmp_cfg):
    """「默认模型被停用」在界面上看起来仍然是有默认模型的。"""
    cid = tmp_cfg("toggle", purpose="extract")
    api.post("/api/sys/llm-configs/%s/default" % cid)
    r = api.post("/api/sys/llm-configs/%s/enabled?on=false" % cid)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enabled"] is False
    assert "待人工" in d["warning"], "停用默认模型却不提后果：%s" % d["warning"]


def test_启停不会顺带改掉别的字段(tmp_cfg):
    """开关就该是开关。

    走 upsert 翻开关要把整条配置重发一遍，中间任何一个字段被前端默认值覆盖
    都会悄悄改掉配置——而「配置被悄悄改了」这种事没有任何报错。
    """
    cid = tmp_cfg("keepfields", purpose="rule_judge", api_key="sk-" + "2" * 30)
    db.execute("UPDATE llm_config SET timeout_s=97, concurrency=7, temperature=0.3 "
               "WHERE config_id=%s", (cid,))
    before = db.query_one("""SELECT timeout_s, concurrency, temperature, model_name,
                                    base_url, api_key_mask FROM llm_config
                             WHERE config_id=%s""", (cid,))
    api.post("/api/sys/llm-configs/%s/enabled?on=false" % cid)
    after = db.query_one("""SELECT timeout_s, concurrency, temperature, model_name,
                                   base_url, api_key_mask FROM llm_config
                            WHERE config_id=%s""", (cid,))
    assert before == after, "翻开关改掉了别的字段：%s -> %s" % (before, after)
