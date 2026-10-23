# -*- coding: utf-8 -*-
"""SDAFI 运行时测试：阶段约束、幂等、信号生命周期、I 阶段边界。

这组测试守的都是**失效时不报错**的东西：
  · 在 D 阶段偷偷写业务表 —— 代码评审看不出来，审计记录会对不上
  · 同一张单审两遍 —— 产出两份结论，报告上不知道该信哪份
  · 信号卡在 claimed —— 那张单永远不会被审，而且没有任何报错
  · I 阶段把参数调出界 —— 自优化变成自毁
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.audit import recorder as rec  # noqa: E402
from app.runtime import triggers, yaml_loader  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 阶段约束
# ─────────────────────────────────────────────────────────────────────────────
def test_非A阶段写业务表必须抛异常():
    for ph in ("S", "D", "F", "I"):
        with rec.phase(ph):
            with pytest.raises(rec.PhaseViolation) as e:
                rec.guard_write("review_run")
            assert "只有 A 阶段" in str(e.value)


def test_A阶段写业务表放行():
    with rec.phase("A"):
        rec.guard_write("review_run")
        rec.guard_write("agent_signal")


def test_没有阶段上下文时写业务表也要拦():
    """漏掉 with phase(...) 是最容易犯的错，不能默认放行。"""
    with pytest.raises(rec.PhaseViolation):
        rec.guard_write("expense_bill")


def test_审计表任何阶段都可写():
    """「只有 A 写库」管的是业务数据，审计表五个阶段都要写——那正是它们存在的理由。

    如果审计表也只许 A 写，S 看到的输入就留不下来，整套追溯就断了。
    """
    for ph in ("S", "D", "A", "F", "I"):
        with rec.phase(ph):
            for t in ("data_pool_snapshot", "cycle_log", "agent_signal_log",
                      "feedback_log", "improve_log"):
                rec.guard_write(t)      # 不抛异常即通过


def test_未归类的新表必须报错():
    """新表不归类就等于绕过了阶段约束，所以默认拒绝而不是默认放行。"""
    with rec.phase("A"):
        with pytest.raises(rec.PhaseViolation) as e:
            rec.guard_write("some_new_table_nobody_classified")
        assert "必须显式归类" in str(e.value)


# ─────────────────────────────────────────────────────────────────────────────
# I 阶段：区间内放行、越界拒绝，两者都要留痕
# ─────────────────────────────────────────────────────────────────────────────
def _agent_bounds():
    cfgs = yaml_loader.load(verbose=False)
    cfg = cfgs.agents["fj_l1_01_travel_review"]
    return cfg, cfg.param_bounds


def test_I阶段区间内调参生效且留痕():
    cfg, bounds = _agent_bounds()
    before = db.scalar("SELECT count(*) FROM improve_log")
    with rec.phase("I"):
        with db.tx() as conn:
            ok, why = rec.propose_param(conn, None, cfg.agent_id, "max_concurrent",
                                        4, "测试：区间内", bounds)
    assert ok, why
    assert db.scalar("SELECT count(*) FROM improve_log") == before + 1
    row = db.query_one("""SELECT param, new_value, rejected FROM improve_log
                          ORDER BY id DESC LIMIT 1""")
    assert row["param"] == "max_concurrent" and row["rejected"] is False
    cur = db.query_one("SELECT params FROM agent_state WHERE agent_id=%s", (cfg.agent_id,))
    assert cur["params"]["max_concurrent"] == 4
    # 还原
    with rec.phase("I"):
        with db.tx() as conn:
            rec.propose_param(conn, None, cfg.agent_id, "max_concurrent", 3, "还原", bounds)


def test_I阶段越界调参被拒并留痕():
    """**被拒绝的尝试同样是审计证据**，不能直接丢掉。

    「Agent 反复想把并发调到 20」这件事本身就是一条信息。
    """
    cfg, bounds = _agent_bounds()
    hi = bounds["max_concurrent"]["hi"]
    before_params = db.query_one("SELECT params FROM agent_state WHERE agent_id=%s",
                                 (cfg.agent_id,))["params"]
    with rec.phase("I"):
        with db.tx() as conn:
            ok, why = rec.propose_param(conn, None, cfg.agent_id, "max_concurrent",
                                        hi + 100, "测试：越界", bounds)
    assert not ok and why == "越界"
    row = db.query_one("""SELECT param, new_value, rejected, rejected_why, bounded_by
                          FROM improve_log ORDER BY id DESC LIMIT 1""")
    assert row["rejected"] is True
    assert "越界" in (row["rejected_why"] or "")
    assert row["bounded_by"]["hi"] == hi, "必须记下当时的区间——只记「从 3 调到 103」" \
                                          "事后无法判断这个值是否合法，因为区间本身也会变版"
    after = db.query_one("SELECT params FROM agent_state WHERE agent_id=%s",
                         (cfg.agent_id,))["params"]
    assert after == before_params, "越界的调参不许改动实际参数"


def test_I阶段不许调未声明的参数():
    """没有区间就等于允许无限自优化。"""
    cfg, bounds = _agent_bounds()
    with rec.phase("I"):
        with db.tx() as conn:
            ok, why = rec.propose_param(conn, None, cfg.agent_id,
                                        "rule_threshold", 999, "测试：未声明", bounds)
    assert not ok
    row = db.query_one("SELECT rejected, rejected_why FROM improve_log ORDER BY id DESC LIMIT 1")
    assert row["rejected"] is True and "未声明" in (row["rejected_why"] or "")


def test_Agent配置禁止清单覆盖规则与阈值():
    """规则是制度的投影，改规则是业务决策，不是 Agent 能动的。"""
    cfgs = yaml_loader.load(verbose=False)
    for a in cfgs.agents.values():
        fb = " ".join(str(x) for x in (a.improve.get("forbidden") or []))
        for must in ("规则", "阈值", "控制类型"):
            assert must in fb, "%s 的 forbidden 没覆盖「%s」" % (a.agent_id, must)


# ─────────────────────────────────────────────────────────────────────────────
# 信号生命周期
# ─────────────────────────────────────────────────────────────────────────────
def test_信号取用与完成():
    ch = "fjhy_test_channel"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))
    with rec.phase("A"):
        with db.tx() as conn:
            sid = rec.emit_signal(conn, ch, "test", "t", None, "BILL-X", {}, "idem-test-1")
    assert sid
    got = triggers.claim_signals(ch, "tester", 10)
    assert len(got) == 1 and got[0]["signal_id"] == sid
    assert triggers.finish_signal(sid, ok=True) == "done"
    assert db.scalar("SELECT status FROM agent_signal WHERE signal_id=%s", (sid,)) == "done"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))


def test_幂等键阻止重复投递():
    ch = "fjhy_test_channel2"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))
    with rec.phase("A"):
        with db.tx() as conn:
            a = rec.emit_signal(conn, ch, "test", "t", None, "BILL-Y", {}, "idem-dup")
        with db.tx() as conn:
            b = rec.emit_signal(conn, ch, "test", "t", None, "BILL-Y", {}, "idem-dup")
    assert a and b is None, "同一幂等键只许投一次：心跳与消息可能对同一主体同时醒来"
    assert db.scalar("SELECT count(*) FROM agent_signal WHERE channel=%s", (ch,)) == 1
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))


def test_孤儿信号会被回收():
    """claimed 了但进程崩掉的信号，不回收就永久卡住，而且没有任何报错。"""
    ch = "fjhy_test_channel3"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))
    with rec.phase("A"):
        with db.tx() as conn:
            sid = rec.emit_signal(conn, ch, "test", "t", None, "BILL-Z", {}, "idem-orphan")
    triggers.claim_signals(ch, "crashed-worker", 10)
    db.execute("UPDATE agent_signal SET claimed_at = now() - INTERVAL '2 hours' "
               "WHERE signal_id=%s", (sid,))
    n = triggers.reclaim_orphans(timeout_seconds=600)
    assert n >= 1
    assert db.scalar("SELECT status FROM agent_signal WHERE signal_id=%s", (sid,)) == "pending"
    assert db.scalar("SELECT count(*) FROM pool_maintenance_log WHERE task='orphan_reclaim'") >= 1, \
        "清理动作本身必须留痕，否则「某段时间的信号为什么是空的」说不清"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))


def test_重试到顶置dead而不是删掉():
    """死信留着才知道「这张单一直没审成」。"""
    ch = "fjhy_test_channel4"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))
    with rec.phase("A"):
        with db.tx() as conn:
            sid = rec.emit_signal(conn, ch, "test", "t", None, "BILL-W", {}, "idem-dead")
    for _ in range(3):
        triggers.claim_signals(ch, "tester", 10)
        st = triggers.finish_signal(sid, ok=False, error="测试失败")
        if st == "dead":
            break
        db.execute("UPDATE agent_signal SET not_before = now() WHERE signal_id=%s", (sid,))
    assert db.scalar("SELECT status FROM agent_signal WHERE signal_id=%s", (sid,)) == "dead"
    db.execute("DELETE FROM agent_signal WHERE channel=%s", (ch,))


# ─────────────────────────────────────────────────────────────────────────────
# 审计一致性
# ─────────────────────────────────────────────────────────────────────────────
def test_审计一致性自检():
    ok, info = rec.audit_consistency()
    assert ok, "审计不一致：%s" % info["problems"]


def test_快照全部已脱敏():
    n = db.scalar("SELECT count(*) FROM data_pool_snapshot WHERE NOT masked")
    assert n == 0, ("有 %d 行快照 masked=FALSE。快照会被审计接口、调试查询、"
                    "导出文件读到，这些都是旁路出口。" % n)


def test_每条审核结果都有对应的Act记录():
    """任何业务表的变更都应该能在 agent_signal_log 里找到对应的一行。

    **方向是单向的**：每条 review_run 必须有 Act 记录；
    反过来 Act 记录多于 review_run 是正常的——业务数据可以被清理
    （比如重新生成演示数据），而审计记录按设计不可删。
    第一版写成「两边条数相等」，清一次演示数据就红，
    而「没有道理的红」会让人开始忽略这个检查。
    """
    orphans = db.query("""SELECT r.run_id FROM review_run r
                          WHERE NOT EXISTS (SELECT 1 FROM agent_signal_log l
                                            WHERE l.cycle_id = r.cycle_id
                                              AND l.action='write_review_result')""")
    assert not orphans, "有 %d 条审核结果没有对应的 Act 记录：%s" % (
        len(orphans), [o["run_id"] for o in orphans[:5]])


def test_软规则命中必须要求人工复核():
    """库里有 CHECK 约束钉着，这里再从数据侧验一遍。"""
    n = db.scalar("""SELECT count(*) FROM review_finding
                     WHERE rule_class='soft' AND outcome='hit' AND NOT needs_human""")
    assert n == 0, "有 %d 条软规则命中没要求人工复核" % n


def test_待人工不得被算作通过():
    """「判不了」落到 review，不能算 pass——算进 pass 就等于声称复核了没复核的东西。"""
    bad = db.query("""SELECT run_id, conclusion, pending_count FROM review_run
                      WHERE conclusion='pass' AND pending_count > 0""")
    assert not bad, "有 %d 张单带着待人工项却给了 pass：%s" % (
        len(bad), [(b["run_id"], b["pending_count"]) for b in bad[:5]])


def test_被剔除的规则逐条记在周期日志里():
    """静默剔除会让一条本该拦截的闸悄悄消失，而且没人知道。"""
    rows = db.query("SELECT cycle_id, rule_excluded FROM cycle_log "
                    "WHERE status='ok' LIMIT 5")
    if not rows:
        pytest.skip("还没跑过周期")
    for r in rows:
        ex = r["rule_excluded"] or []
        assert ex, "周期 %s 没有记录被剔除的规则" % r["cycle_id"]
        assert all(x.get("why") for x in ex), "有被剔除的规则没写原因"
