# -*- coding: utf-8 -*-
"""智能体工作台 + **信号落库的回归测试**。

这个文件里最重要的一条是 test_信号必须真的落进台账:
2026-10-19 实测发现 app/agent_job.py:emit 写的是 channel / kind / from_agent
这些**库里不存在的列**（0001_audit.sql 里是 signal_id / cycle_id / agent_id /
action / target / payload / written_tables / idempotency_key / masked），
两个 NOT NULL 列也没给值，所以那条 INSERT **从来没成功过一次**；
而 `except Exception: pass` 把它静默了整整一天——
三条链跑了几十次，台账里来自它们的记录是 0 行，页面上却显示「投了 N 条信号」。

「A 是唯一允许写库的阶段，每次写库都要有对应的一行」这条纪律，
正是靠这张台账来验证的。台账写不进去，这条纪律就只是一句口号。
所以这里既钉**行为**（真的多了一行）也钉**写法**（列名和 recorder 一致）。
"""
import io
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent_job, db  # noqa: E402
from app.main import app  # noqa: E402
from app.runtime import yaml_loader  # noqa: E402

client = TestClient(app)
APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


# ─────────────────────────────────────────────────────────────────────────────
# 一、信号落库（回归）
# ─────────────────────────────────────────────────────────────────────────────
def test_信号必须真的落进台账():
    """**修过的 bug，不许回去。** 列名写错 + except: pass = 静默丢台账。"""
    job_id = "job-TEST-" + str(int(time.time()))
    job = {"job_id": job_id, "started_at": time.time(), "signals": [], "log_errors": []}
    j = agent_job.AgentJob([], {}, "test_baseline.json", "测试", "test_bus", "测试总线")
    try:
        j.emit(job, {"id": "fj_l0_04_material_perception"}, "fjhy_test_channel", {"n": 1})
        assert not job["log_errors"], "落库失败了：%s" % job["log_errors"]
        row = db.query_one("""SELECT agent_id, action, target, payload, written_tables,
                                     idempotency_key, masked
                                FROM agent_signal_log WHERE cycle_id=%s""", (job_id,))
        assert row, "信号没落库——emit 的列名又和 agent_signal_log 对不上了"
        assert row["agent_id"] == "fj_l0_04_material_perception"
        assert row["action"] == "emit_signal"
        assert row["target"] == "fjhy_test_channel"
        assert row["payload"] == {"n": 1}
        assert row["written_tables"] == {"agent_signal_log": 1}
        assert job_id in row["idempotency_key"], "幂等键里没有作业号，重跑会撞唯一索引"
    finally:
        db.execute("DELETE FROM agent_signal_log WHERE cycle_id=%s", (job_id,))


def test_落库失败不许静默():
    """失败可以不中断链，但**必须在作业快照里看得见**。

    上一版是 `except: pass`，于是「一次都没成功」藏了一天。
    """
    src = io.open(os.path.join(APP_DIR, "agent_job.py"), encoding="utf-8").read()
    emit = src[src.index("def emit(self"):src.index("def _work(self")]
    assert "log_errors" in emit, "落库失败没有记进 log_errors"
    assert "except Exception:\n            # 信号落库失败" not in emit
    # 列名必须和 recorder 那套一致：一张表两套写法，审计就对不上。
    # **只看 INSERT 那段 SQL**：第一版拿整个函数体去找「channel,」，
    # 结果命中了函数签名 `def emit(self, job, agent, channel, ...)` —— 错的是断言。
    sql = emit[emit.index("INSERT INTO agent_signal_log"):]
    sql = sql[:sql.index('"""')]
    for col in ("signal_id", "cycle_id", "agent_id", "action", "target",
                "idempotency_key"):
        assert col in sql, "emit 的 INSERT 少写了 %s" % col
    for gone in ("channel", "from_agent", "to_agent", "subject_id"):
        assert gone not in sql, "emit 的 INSERT 里还有库里不存在的列：%s" % gone


def test_三条链的信号现在真能落库():
    """实测口径：修之前来自三条链的台账是 0 行。这条测试防止它退回 0。"""
    n = db.scalar("SELECT count(*) FROM agent_signal_log WHERE agent_id <> %s",
                  ("fj_l1_01_travel_review",))
    assert n > 0, ("来自三条编排链的信号台账又变成 0 行了——"
                   "agent_job.emit 的列名八成和 agent_signal_log 对不上了")


# ─────────────────────────────────────────────────────────────────────────────
# 二、总览
# ─────────────────────────────────────────────────────────────────────────────
def test_总览覆盖全部契约():
    r = client.get("/api/agents")
    assert r.status_code == 200
    d = r.json()
    cfg = yaml_loader.load(verbose=False)
    # 13 = config/agents/ 下的契约数（2026-10-19 加了 fj_l1_06_rule_detail
    # 「规则明细成形」之后从 12 变 13）。**钉住数字**：少一个契约意味着
    # 某个 Agent 从总览里消失了，而页面不会报错，只会少一行。
    assert d["totals"]["agents"] == len(cfg.agents) == 13
    assert sum(d["totals"]["by_layer"].values()) == 13
    assert d["totals"]["failed"] == 0, "契约校验有失败项，后端本来就该拒绝启动"
    assert {x["agent_id"] for x in d["items"]} == set(cfg.agents)


def test_每个智能体都有所属链():
    """**链的成员以编排代码里的 AGENTS 为准**，不在接口里另记一份。

    另记一份的下场是改了编排忘了这里，而页面还一本正经地显示旧的归属。
    """
    d = client.get("/api/agents").json()
    orphans = [x["agent_id"] for x in d["items"] if not x["chain_id"]]
    assert not orphans, "这些 Agent 不属于任何一条链，页面上会显示「—」：%s" % orphans


def test_智能体名字不许被脱敏吃掉():
    """**同族坑的第四次预防。** 字段叫 name 就会走人名脱敏。"""
    d = client.get("/api/agents").json()
    bad = [x["agent_name"] for x in d["items"] if "*" in (x["agent_name"] or "")]
    assert not bad, "智能体名字被脱敏吃了：%s" % bad
    for x in d["items"]:
        assert "name" not in x, "%s 里有裸 name 键" % x["agent_id"]


def test_实话必须带原因():
    """页面上那三条「实话」不许只摆数字——数字不带口径就会被误读。

    比如「心跳一次没跳过」看着像坏了，实际是没有常驻调度器在跑。
    """
    d = client.get("/api/agents").json()
    assert len(d["facts"]) >= 3
    for f in d["facts"]:
        assert f["fact"] and f["why"]
    joined = " ".join(f["why"] for f in d["facts"])
    assert "常驻" in joined, "没有解释「心跳为什么没跳过」"


# ─────────────────────────────────────────────────────────────────────────────
# 三、契约详情
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("aid", sorted(yaml_loader.load(verbose=False).agents))
def test_每个契约的五段都取得到(aid):
    d = client.get("/api/agents/" + aid).json()
    s = d["sdafi"]
    assert s["S"]["tables"], "%s 没声明 sense.tables" % aid
    assert s["A"]["writes"], "%s 没声明 act.writes" % aid
    assert s["I"]["forbidden"], "%s 没声明禁止自调项" % aid
    assert s["F"]["metrics"], "%s 没声明反馈指标" % aid
    # 契约原文必须带注释：注释就是口径本身
    assert d["contract_file"]["comment_lines"] >= 5, "%s 的契约几乎没有注释" % aid
    assert "agent_id:" in d["contract_file"]["text"]
    # 指标名与参数名也不许叫 name
    for m in s["F"]["metrics"]:
        assert m["metric_name"] and "*" not in m["metric_name"]
    for p in s["I"]["params"]:
        assert p["param_name"] and p["lo"] is not None and p["hi"] is not None


def test_取不存在的智能体给404():
    r = client.get("/api/agents/fj_l9_99_nope")
    assert r.status_code == 404


def test_校验接口和启动时跑的是同一套():
    d = client.get("/api/agents/checks").json()
    cfg = yaml_loader.load(verbose=False)
    assert d["total"] == len(cfg.checks)
    assert d["failed"] == len(cfg.errors) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 四、链
# ─────────────────────────────────────────────────────────────────────────────
def test_四条链都在():
    d = client.get("/api/agents/chains/list").json()
    ids = [c["chain_id"] for c in d["items"]]
    assert ids == ["extract", "onto_align", "onto_build", "review"]
    for c in d["items"]:
        assert c["agents"], "%s 没有成员" % c["chain_id"]
        assert c["note"] and c["page"]


def test_审核链不许从页面启停():
    """它是常驻进程，启停属于**部署动作**，不该挂在一个页面按钮上。"""
    r = client.post("/api/agents/chains/review/run")
    assert r.status_code == 400
    assert "常驻" in r.json()["detail"]


def test_没有这条链时给404():
    assert client.post("/api/agents/chains/nope/run").status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 五、这一页是只读的
# ─────────────────────────────────────────────────────────────────────────────
def test_工作台不写库():
    """**这一页只读。**

    改参数要走 improve 的 bounds 校验与 improve_log，不能从页面绕过去；
    手工投信号会产生一条「没有上游」的协同记录，审计时说不清是谁触发的。
    这条测试钉的是写法：接口模块里不许出现写库语句。
    """
    src = io.open(os.path.join(APP_DIR, "api", "agents.py"), encoding="utf-8").read()
    for bad in ("INSERT INTO", "UPDATE ", "DELETE FROM", "db.execute("):
        assert bad not in src, "智能体工作台的接口里出现了写库动作：%s" % bad
    # 唯一的 POST 是「启动已有的那三条链」，启动的是同一个编排，不是新造能力
    assert src.count("@router.post") == 1


def test_存储表说清了每张表的角色():
    d = client.get("/api/agents/storage/tables").json()
    names = [x["table_name"] for x in d["items"]]
    assert "agent_signal_log" in names and "config/agents/*.yaml" in names
    for x in d["items"]:
        assert x["what"] and x["written_by"] and x["on_recalc"] and x["why"]
    log = [x for x in d["items"] if x["table_name"] == "agent_signal_log"][0]
    assert "永不清" in log["on_recalc"], "台账被标成可清，那它就不是台账了"
    yml = [x for x in d["items"] if x["table_name"] == "config/agents/*.yaml"][0]
    assert yml["rows"] == 13          # 同上：契约数从 12 变 13


def test_信号与周期接口可用():
    s = client.get("/api/agents/signals/recent?limit=5").json()
    assert len(s["items"]) <= 5
    assert "backlog" in s and s["facts"]
    for f in s["facts"]:
        assert f["fact"] and f["why"]
    c = client.get("/api/agents/cycles/recent?limit=5").json()
    assert len(c["items"]) <= 5
    assert c["by_trigger"]


def test_台账里的载荷也过脱敏():
    """信号台账会经 HTTP 出去，payload 里可能带主体信息——它是个出口。"""
    from app import mask
    s = client.get("/api/agents/signals/recent?limit=100").json()
    leaks = mask.find_leaks(s["items"])
    assert not leaks, "信号台账泄露 %d 处：%s" % (len(leaks), leaks[:3])
    c = client.get("/api/agents/cycles/recent?limit=100").json()
    leaks2 = mask.find_leaks(c["items"])
    assert not leaks2, "周期台账泄露 %d 处：%s" % (len(leaks2), leaks2[:3])


def test_载荷是合法json不是字符串():
    """payload 存的是 JSONB。回成字符串说明某处把它 dumps 了两次。"""
    s = client.get("/api/agents/signals/recent?limit=20").json()
    for x in s["items"]:
        assert x["payload"] is None or isinstance(x["payload"], (dict, list)), \
            "payload 回成了 %s：%r" % (type(x["payload"]).__name__, x["payload"])
        assert x["written_tables"] is None or isinstance(x["written_tables"], dict)
    assert json.dumps(s["items"][:1])      # 能序列化回去
