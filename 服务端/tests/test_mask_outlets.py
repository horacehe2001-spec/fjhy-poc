# -*- coding: utf-8 -*-
"""**遍历每一条路由**，验证脱敏真的覆盖全部出口。

纪律第 8 条：判断「有没有泄露」要把每个对外接口的响应都过一遍同一套规则，
而不是看那个模块写没写。

所以这组测试不检查 mask.py 的代码，而是：
  1. 从 FastAPI 的路由表里枚举所有 GET 接口（不是手写一个清单——
     手写清单会漏掉后来新加的接口，而「后来加的那个接口忘了脱敏」
     正是最典型的泄露方式）
  2. 逐个调用，把响应体整个过 find_leaks
  3. 另外单独验几条**已知最容易漏**的旁路：审计接口、调试接口、
     配置接口、错误响应

已知的真实敏感值（取自客户样本，用来当探针）：
  银行账号 622908113255360583、姓名 乐知行/鄢岚/宦允文 等 15 人
"""
import io
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, mask  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

# 需要路径参数的接口，给一个真实存在的值。
#
# **只取真实单据（NOT is_demo）**，不取演示数据：
# 演示数据生成器会清掉演示单的 review_run，而测试之间的执行顺序不保证——
# test_detection 先跑的话，这里拿到的 run_id 就失效了，测试报 404。
# 那种红是「测试之间互相干扰」，不是「接口有问题」，排查起来最费时间。
def _sample_ids():
    # **取样 id 必须限定在当前实例内。**
    # 2026-11-01：822港航 建了第一个 release（rel-0001）之后，
    # 这里的 `SELECT release_id FROM release LIMIT 1` 取到了它，
    # 而接口是按**当前实例**查的，于是返回 400「本实例里没有这个发布」，
    # 用例报「接口坏了」——坏的是取样，不是接口。
    # rule_id / doc_id / table_id 同理：0041 之后这些编号只在实例内唯一，
    # 取到别家的就会得到一个本实例查不到的 id。
    from app import scope
    iid = scope.require(None)
    bill = db.scalar("SELECT bill_no FROM expense_bill WHERE NOT is_demo "
                     "AND instance_id=%s ORDER BY bill_no LIMIT 1", (iid,))
    run = db.scalar("""SELECT r.run_id FROM review_run r
                       JOIN expense_bill b ON b.bill_no = r.bill_no
                       WHERE NOT b.is_demo AND b.instance_id=%s
                       ORDER BY r.started_at DESC LIMIT 1""", (iid,))
    rule = db.scalar("""SELECT rule_id FROM rule WHERE version=1 AND instance_id=%s
                         ORDER BY rule_id LIMIT 1""", (iid,))
    doc = db.scalar("SELECT doc_id FROM policy_doc WHERE instance_id=%s LIMIT 1", (iid,))
    rel = db.scalar("SELECT release_id FROM release WHERE instance_id=%s LIMIT 1", (iid,))
    tbl = db.scalar("""SELECT table_id FROM lookup_table WHERE instance_id=%s
                        ORDER BY table_id LIMIT 1""", (iid,))
    return {"bill_no": bill, "subject_id": bill, "run_id": run, "rule_id": rule,
            "doc_id": doc or "none", "release_id": rel or "none",
            "table_id": tbl or "none", "task_id": "none", "config_id": "none"}


def _get_routes():
    """枚举所有 GET 出口。**必须穿透 include_router 的包装层。**

    FastAPI 0.141 起，`app.include_router(x)` 不再把 x 的路由摊平进
    `app.routes`，而是放一个 `_IncludedRouter` 包装对象进去。
    原来那版只遍历一层，于是**这套「全出口扫描」静默缩到只剩 2 条**
    （/health 和 /api/startup-report，那两条是直接挂在 app 上的）。
    实测：一层 2 条 → 穿透后 72 条。
    这正是纪律第 8 条说的那种漏法——做安全的那个模块看着还在，
    而它守的门从 60 多个缩成了 2 个，没有任何报错。
    救场的是下面那句 `len(ROUTES) >= 15` 的断言：
    **确定性的闸失效而没人发现，比没有这道闸更危险**，所以这道闸自己也要有闸。

    为什么不用 `app.openapi()["paths"]`：它**漏掉 include_in_schema=False
    的接口**，而那几条恰恰是旁路出口（/api/asset/llm-configs 就是），
    漏掉它们等于专门不扫最容易漏的那几个门。
    """
    out = []

    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:                 # include_router 的包装层
                walk(inner.routes)
                continue
            if "GET" not in (getattr(r, "methods", set()) or set()):
                continue
            path = getattr(r, "path", "")
            if path.startswith(("/api", "/health")):
                out.append(path)

    walk(app.routes)
    return sorted(set(out))


ROUTES = _get_routes()

# 每个已注册的路由前缀都必须有路由被枚举到。
# 只看总数不够：某一个 router 整体没被枚举（比如新加的 /api/onto 忘了注册，
# 或者下一个版本又换一种包装方式）时，总数仍然很大，而那一栏的出口一条没扫。
# 实测前缀分布（2026-10-19，68 条 GET）：
#   asset 17 · onto 15 · doclib 11 · extract 6 · audit 5 · sys 4
#   bills 3 · debug 3 · reviews 1 · deviation 1 · startup-report 1 · health 1
EXPECTED_PREFIXES = ("/api/asset", "/api/bills", "/api/reviews", "/api/audit",
                     "/api/debug", "/api/sys", "/api/doclib", "/api/extract",
                     "/api/onto", "/api/publish", "/api/pub/v1", "/api/agents")

_IDS_CACHE: dict = {}


def ids():
    """按需取、每次测试会话内缓存一次。

    模块导入时就取会把值固化在最早那一刻，而测试之间会改数据——
    固化的 ID 失效后报 404，看着像接口坏了。
    """
    if not _IDS_CACHE:
        _IDS_CACHE.update(_sample_ids())
    return _IDS_CACHE


def _fill(path):
    for k, v in ids().items():
        path = path.replace("{%s}" % k, str(v) if v is not None else "none")
    return path


def test_路由表不是空的():
    assert len(ROUTES) >= 15, "只发现 %d 条 GET 路由，枚举逻辑可能坏了：%s" % (len(ROUTES), ROUTES)


def test_每个路由前缀都被扫到():
    """总数够大不等于每一栏都扫到了。

    实测过一次「只剩 2 条」：那次总数断言救了场。但如果只是**某一个**
    router 没被枚举到，总数仍然有六十多条，而那一栏的出口一条没过脱敏，
    报告上看不出少扫了什么。
    """
    missing = [p for p in EXPECTED_PREFIXES
               if not any(r.startswith(p) for r in ROUTES)]
    assert not missing, ("这些前缀下一条 GET 路由都没枚举到：%s；"
                         "要么路由没注册，要么枚举方式又被框架改了" % missing)


@pytest.mark.parametrize("path", ROUTES)
def test_每条路由的响应都不得泄露(path):
    """逐条调用，整个响应体过 find_leaks。"""
    url = _fill(path)
    if "{" in url:
        pytest.skip("路径参数无法自动填充：%s" % path)
    resp = client.get(url)
    assert resp.status_code in (200, 404, 422), \
        "%s 返回 %s：%s" % (url, resp.status_code, resp.text[:200])
    if resp.status_code != 200:
        return
    try:
        data = resp.json()
    except ValueError:
        pytest.skip("非 JSON 响应")
    leaks = mask.find_leaks(data)
    assert not leaks, "接口 %s 泄露 %d 处：%s" % (url, len(leaks), leaks[:5])


@pytest.mark.parametrize("path", ROUTES)
def test_每条路由都带脱敏标记头(path):
    """响应头上的 x-fjhy-masked 是中间件生效的凭据。

    没有这个头说明响应绕过了中间件——比如某条路由返回了非 JSON、
    或者中间件没挂上。联调时靠它一眼看出来。
    """
    url = _fill(path)
    if "{" in url:
        pytest.skip("路径参数无法自动填充")
    resp = client.get(url)
    assert resp.headers.get("x-fjhy-masked") == "1", \
        "%s 的响应没有经过脱敏中间件" % url


# ─────────────────────────────────────────────────────────────────────────────
# 已知旁路 ①：资产包（application/zip）
# ─────────────────────────────────────────────────────────────────────────────
# 中间件只重写 application/json 的响应体，zip 原样放行——**它是个旁路出口**。
# 包内容在 app/api/pub.py:_build_bundle 打包时自己过了一遍按内容判定的脱敏。
# 这条测试把它登记在「全部出口」这张册子上，免得日后只有 pub 那边有测试，
# 而做安全审查的人翻这个文件翻不到。
def test_资产包是已知旁路并且包内容也过了脱敏():
    import json as _json
    import zipfile as _zip
    from app import release_pack as _rp

    tag = "rs-TEST-maskoutlet"
    db.execute("DELETE FROM release WHERE rule_set_version=%s", (tag,))
    try:
        r = _rp.freeze(tag, "出口审查")
        _rp.publish(r["release_id"], "出口审查")
        resp = client.get("/api/pub/v1/releases/%s/bundle" % tag)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.content[:2] == b"PK", "中间件把 zip 改坏了"
        z = _zip.ZipFile(io.BytesIO(resp.content))
        for n in z.namelist():
            if n.endswith(".json"):
                leaks = mask.find_leaks(_json.loads(z.read(n).decode("utf-8")))
                assert not leaks, "资产包成员 %s 泄露：%s" % (n, leaks[:3])
    finally:
        db.execute("DELETE FROM release WHERE rule_set_version=%s", (tag,))


# ─────────────────────────────────────────────────────────────────────────────
# 已知最容易漏的四条旁路，单独钉
# ─────────────────────────────────────────────────────────────────────────────
REAL_ACCOUNT = "622908113255360583"


def test_旁路一_审计快照接口不得泄露():
    """快照里存的是完整事实对象，含收款账号与姓名。"""
    r = client.get("/api/audit/snapshots?size=5")
    assert r.status_code == 200
    body = r.text
    assert REAL_ACCOUNT not in body, "审计快照接口泄露了真实银行账号"
    for n in mask.name_set():
        assert n not in body, "审计快照接口泄露了姓名 %s" % n


def test_旁路二_调试接口不得泄露():
    """/api/debug/fact 直接返回 S 阶段的原始事实对象，不经过报告生成——
    是最容易漏的一条。"""
    bn = ids()["bill_no"]
    if not bn:
        pytest.skip("库里没有单据")
    r = client.get("/api/debug/fact/%s" % bn)
    assert r.status_code == 200
    body = r.json()
    assert body["masked"] is True, "调试接口返回了未脱敏的事实：%s" % body.get("leaks")
    assert not mask.find_leaks(body["payload"]), mask.find_leaks(body["payload"])[:5]


def test_旁路三_大模型配置只回掩码():
    """写进去一个明文 Key，读出来必须是掩码。"""
    client.post("/api/asset/llm-configs", json={
        "config_id": "test-llm", "name": "测试", "model_name": "qwen3.5-122b-a10b",
        "api_key": "sk-TESTKEY1234567890abcdef", "purpose": "rule_judge"})
    r = client.get("/api/asset/llm-configs")
    assert r.status_code == 200
    body = r.text
    assert "sk-TESTKEY1234567890abcdef" not in body, "配置接口回了明文 Key"
    assert "api_key_cipher" not in body, "配置接口回了密文字段"
    db.execute("DELETE FROM llm_config WHERE config_id='test-llm'")


def test_旁路四_错误响应不得泄露():
    """错误信息里经常带着查询参数和数据片段。"""
    r = client.get("/api/bills/%s" % REAL_ACCOUNT)      # 用账号当单号，必然 404
    assert r.status_code in (404, 500)
    assert REAL_ACCOUNT not in r.text, "错误响应把请求里的账号原样回显了"


def test_审计追溯接口能分阶段取到五张表():
    bn = ids()["bill_no"]
    if not bn:
        pytest.skip("库里没有单据")
    r = client.get("/api/audit/trace/%s" % bn)
    assert r.status_code == 200
    d = r.json()
    for k in ("S_snapshots", "D_cycles", "A_actions", "F_feedback", "I_improvements"):
        assert k in d, "追溯接口缺 %s" % k
    assert not mask.find_leaks(d)


def test_审核报告必须并列四个数():
    rid = ids()["run_id"]
    if not rid:
        pytest.skip("库里还没有审核记录")
    r = client.get("/api/reviews/%s" % rid)
    assert r.status_code == 200
    cov = r.json()["coverage"]
    for k in ("candidates", "executed", "excluded", "hit", "pending", "passed"):
        assert k in cov, "覆盖面里缺 %s" % k
    assert cov["executed"] + cov["excluded"] == cov["candidates"], \
        "执行 %s + 剔除 %s 应当等于候选 %s" % (cov["executed"], cov["excluded"], cov["candidates"])


def test_被剔除的规则在报告里单列():
    rid = ids()["run_id"]
    if not rid:
        pytest.skip("库里还没有审核记录")
    d = client.get("/api/reviews/%s" % rid).json()
    assert d["excluded_rules"], "报告里没有列出被剔除的规则"
    assert all(x.get("why") for x in d["excluded_rules"]), "有被剔除的规则没写原因"

# ─────────────────────────────────────────────────────────────────────────────
# 本体元素名与规则名**不是人名**：不许被脱敏吃掉
# ─────────────────────────────────────────────────────────────────────────────
# 这一组是补的。实测（2026-10-19）这些出口全被人名策略吃掉过：
#   /api/asset/rules        规则名 → 付**********、住***********
#   /api/asset/ontology     实体名/枚举名 → 报**、单***
#   /api/asset/process      节点名 → 提*、部******
#   /api/onto/source        YAML 段落名 → m***、b*******
# 「规则本体」那一页的「名称」整列、流程本体的节点链，一直显示的都是星号。
# **误报和漏报同样致命**：漏报让真人名流出去，误报让产品正中间那张表变成星号。
# 所以这一组测两个方向：该脱的照样脱，不该脱的一个星号都不许有。
def _nostar(label, values):
    bad = [v for v in values if isinstance(v, str) and "*" in v]
    assert not bad, "%s 被脱敏吃掉了：%s —— 它们不是人名，应当换字段名而不是改脱敏策略"                     % (label, bad[:5])


def test_规则名不许被脱敏():
    r = client.get("/api/asset/rules?size=8")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    _nostar("规则名", [x.get("rule_name") for x in items])
    assert all("name" not in x for x in items),         "规则清单里又出现了叫 name 的字段，它会被人名脱敏吃掉"


def test_规则详情与引用概念名不许被脱敏():
    rid = db.scalar("SELECT rule_id FROM rule WHERE version=1 ORDER BY rule_id LIMIT 1")
    d = client.get("/api/asset/rules/%s" % rid).json()
    _nostar("规则详情名", [d.get("rule_name")])
    _nostar("引用概念名", [c.get("concept_name") for c in d.get("concepts") or []])


def test_本体实体与枚举名不许被脱敏():
    d = client.get("/api/asset/ontology").json()
    _nostar("实体名", [e.get("entity_name") for e in d["entities"]])
    _nostar("枚举名", [e.get("enum_name") for e in d["enums"]])


def test_流程节点名不许被脱敏():
    d = client.get("/api/asset/process").json()
    _nostar("流程节点名", [n.get("node_name") for n in d["nodes"]])
    g = client.get("/api/onto/process/graph").json()
    _nostar("图谱节点名", [n.get("node_name") for n in g["nodes"]])


def test_本体源文件段落名不许被脱敏():
    d = client.get("/api/onto/source").json()
    _nostar("YAML 段落名", [s.get("section_name") for s in d["sections"]])
    _nostar("谁写的", [d.get("written_by")])


def test_候选名与智能体名不许被脱敏():
    d = client.get("/api/onto/candidates").json()
    _nostar("候选名", [c.get("cand_name") for c in d["items"]])
    p = client.get("/api/onto/build/progress").json()
    _nostar("智能体名", [a.get("agent_name") for a in p["agents"]])


def test_真人名该脱的照样脱():
    """**反方向也要测。** 上面那一组是为了不误伤，这一条是为了别把闸拆了。"""
    bill = db.scalar("SELECT bill_no FROM expense_bill WHERE NOT is_demo "
                     "ORDER BY bill_no LIMIT 1")
    d = client.get("/api/bills/%s" % bill).json()
    names = [t.get("name") for t in d.get("travelers") or []]
    if not names:
        pytest.skip("这张单没有同行人数据")
    assert all("*" in n for n in names if n),         "同行人姓名没被脱敏：%s —— 这是真人名，必须脱" % names
