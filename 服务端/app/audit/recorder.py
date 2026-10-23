# -*- coding: utf-8 -*-
"""Audit-by-Design 的写入端。五个阶段各写自己那张表。

【「只有 A 阶段写库」要说准确】
  这条纪律管的是**业务数据与数据池**：业务表（expense_bill 等）、
  信号队列（agent_signal）、审核结果（review_run / review_finding）
  只许 A 阶段写。
  而**审计表本身五个阶段都要写**——那正是它们存在的理由：
  S 要记「当时看到什么」、D 要记「据此判了什么」、F 要记「偏差多少」、
  I 要记「调了哪个参数」。如果审计表也只许 A 写，那 S 看到的输入就留不下来，
  整套追溯就断了。
  所以 PhaseGuard 守的是「业务表白名单」，不是「所有写操作」。

【为什么要有 PhaseGuard，而不是靠代码自觉】
  靠自觉的结果是：某次改动里顺手在 D 阶段更新了一行业务数据，
  代码评审看不出来，而审计表里不会有对应的 agent_signal_log，
  于是「业务数据变了但审计记录没留下」。
  Audit-by-Design 的全部价值就在于这两者一致，所以这个约束必须可执行。
"""
import json
import threading
import uuid

from .. import db, mask

# 只许 A 阶段写的表。审计表不在这里面——它们每个阶段都要写。
BUSINESS_TABLES = {
    "expense_bill", "expense_detail", "approval_record", "attachment",
    "bill_traveler", "trip_application", "trip_calendar", "invoice",
    "hotel_invoice", "transport_voucher", "authority_approval", "meeting_notice",
    "expense_allocation", "employee", "org", "budget", "org_special_rule",
    "agent_signal", "review_run", "review_finding", "manual_review",
}
AUDIT_TABLES = {"data_pool_snapshot", "cycle_log", "agent_signal_log",
                "feedback_log", "improve_log"}

_local = threading.local()


class PhaseViolation(RuntimeError):
    pass


def current_phase():
    return getattr(_local, "phase", None)


class phase:
    """阶段上下文。进入后 guard_write 会据此判断这次写是否合法。"""

    def __init__(self, name):
        self.name = name
        self.prev = None

    def __enter__(self):
        self.prev = current_phase()
        _local.phase = self.name
        return self

    def __exit__(self, *a):
        _local.phase = self.prev
        return False


def guard_write(table):
    """写业务表之前必须过这一关。

    不在 A 阶段写业务表 → 直接抛异常，不是打日志。
    打日志的结果是没人看；抛异常会让这次周期失败并记进 cycle_log.error，
    那是能被发现的。
    """
    if table in AUDIT_TABLES:
        return
    if table not in BUSINESS_TABLES:
        raise PhaseViolation(
            "表 %s 既不在业务表白名单也不在审计表里。新表必须显式归类——"
            "不归类就等于绕过了阶段约束。" % table)
    ph = current_phase()
    if ph != "A":
        raise PhaseViolation(
            "在 %s 阶段写业务表 %s。只有 A 阶段允许写业务数据："
            "S 只读、D 不碰库、F 只算偏差、I 只调自己的参数。"
            "这条纪律的可验证形式是 agent_signal_log——"
            "任何业务表的变更都应该能在那里找到对应的一行；"
            "对不上就说明有代码绕过了 Act。" % (ph or "无阶段上下文", table))


def _short_id(prefix):
    """生成标识。**中间插分隔符，避免产生 16 位以上的连续数字。**

    uuid4().hex 有约 1/6000 的概率出现 16 位纯数字串，
    那样的标识会被脱敏自检的银行卡号正则命中，报出一条假泄露。
    实测踩到过：signal_id 里的 6873475042104470 被判成卡号。
    假泄露的代价不比真泄露小——它会让人开始忽略泄露告警。
    插一个短横就彻底排除了这种碰撞。
    """
    h = uuid.uuid4().hex
    return "%s-%s-%s" % (prefix, h[:10], h[10:20])


def new_cycle_id():
    return _short_id("cyc")


def new_signal_id():
    return _short_id("sig")


# ─────────────────────────────────────────────────────────────────────────────
# S · 快照
# ─────────────────────────────────────────────────────────────────────────────
def record_snapshot(conn, agent_id, agent_version, meta, payload, masked, leaks=None):
    """写 data_pool_snapshot。

    payload 必须是**已脱敏**的。masked=False 的行不允许出现在生产——
    快照会被审计接口、调试查询、导出文件读到，这些都是旁路出口。
    """
    if not masked:
        # 不是悄悄降级，而是把泄露项一起记下来，让它可查
        payload = dict(payload or {})
        payload["_mask_leaks"] = leaks or []
    cur = conn.cursor()
    cur.execute("""INSERT INTO data_pool_snapshot
        (snapshot_id, agent_id, agent_version, subject_id, source_tables,
         payload, payload_hash, masked, row_counts, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (meta["snapshot_id"], agent_id, agent_version, meta.get("subject_id"),
         json.dumps(meta.get("source_tables") or {}, ensure_ascii=False),
         json.dumps(payload, ensure_ascii=False, default=str),
         meta["payload_hash"], bool(masked),
         json.dumps(meta.get("row_counts") or {}, ensure_ascii=False),
         None if masked else "脱敏自检发现 %d 处泄露" % len(leaks or [])))
    return meta["snapshot_id"]


# ─────────────────────────────────────────────────────────────────────────────
# D · 周期
# ─────────────────────────────────────────────────────────────────────────────
def start_cycle(conn, cycle_id, agent_id, agent_version, versions, subject_id,
                snapshot_id, trigger_kind, trigger_reason):
    cur = conn.cursor()
    cur.execute("""INSERT INTO cycle_log
        (cycle_id, agent_id, agent_version, rule_set_version, ontology_version,
         model_version, prompt_template_id, subject_id, snapshot_id,
         trigger_kind, trigger_reason, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running')""",
        (cycle_id, agent_id, agent_version, versions.get("rule_set"),
         versions.get("ontology"), versions.get("model"),
         versions.get("prompt_template"), subject_id, snapshot_id,
         trigger_kind, trigger_reason))
    return cycle_id


def finish_cycle(conn, cycle_id, decision, stats, excluded, rule_hits,
                 timings, tokens=None, status="ok", error=None):
    cur = conn.cursor()
    cur.execute("""UPDATE cycle_log SET
            finished_at = now(), rule_candidates = %s, rule_executed = %s,
            rule_excluded = %s, rule_hits = %s, decision = %s,
            phase_timings_ms = %s, prompt_tokens = %s, completion_tokens = %s,
            status = %s, error = %s
        WHERE cycle_id = %s""",
        (stats.get("candidates"), stats.get("executed"),
         json.dumps(excluded, ensure_ascii=False),
         json.dumps(rule_hits, ensure_ascii=False, default=str),
         json.dumps(decision, ensure_ascii=False, default=str),
         json.dumps(timings, ensure_ascii=False),
         (tokens or {}).get("prompt"), (tokens or {}).get("completion"),
         status, error, cycle_id))


# ─────────────────────────────────────────────────────────────────────────────
# A · 动作
# ─────────────────────────────────────────────────────────────────────────────
def record_action(conn, cycle_id, agent_id, action, target, payload,
                  written_tables, idempotency_key):
    """写 agent_signal_log。**每一次业务表写入都要有对应的一行。**

    written_tables 记的是「这次动作实际写了哪些表、各几行」。
    tests/test_audit_guard.py 用它做断言：业务表的变更数应该与这里记的对得上。
    """
    cur = conn.cursor()
    cur.execute("""INSERT INTO agent_signal_log
        (signal_id, cycle_id, agent_id, action, target, payload,
         written_tables, idempotency_key, masked)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE)""",
        (new_signal_id(), cycle_id, agent_id, action, target,
         json.dumps(mask.scrub(payload or {}), ensure_ascii=False, default=str),
         json.dumps(written_tables or {}, ensure_ascii=False),
         idempotency_key))


def emit_signal(conn, channel, kind, from_agent, to_agent, subject_id,
                payload, idempotency_key, priority=100):
    """往数据池投信号。**这是 A 阶段的动作，受 PhaseGuard 约束。**

    写信号与写业务表在同一个事务里——要么都成要么都不成。
    这是不上 MQ 的理由：MQ 在数据库事务之外，会产生
    「审计记录与实际发生的事不一致」，而 Audit-by-Design 的价值全在二者一致。
    """
    guard_write("agent_signal")
    cur = conn.cursor()
    sid = new_signal_id()
    cur.execute("""INSERT INTO agent_signal
        (signal_id, channel, from_agent, to_agent, subject_id, kind, payload,
         priority, idempotency_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
             AND status IN ('pending','claimed') DO NOTHING""",
        (sid, channel, from_agent, to_agent, subject_id, kind,
         json.dumps(mask.scrub(payload or {}), ensure_ascii=False, default=str),
         priority, idempotency_key))
    return sid if cur.rowcount else None


# ─────────────────────────────────────────────────────────────────────────────
# F · 反馈
# ─────────────────────────────────────────────────────────────────────────────
def record_feedback(conn, cycle_id, agent_id, subject_id, metric, expected,
                    actual, deviation, deviation_kind, source):
    """写 feedback_log。

    deviation_kind 必须区分 false_positive / false_negative——
    漏报放过风险；误报把正常单据判成问题件，审核岗很快就不再信任这套系统。
    合成一个「准确率」会把误报的代价藏起来。
    """
    cur = conn.cursor()
    cur.execute("""INSERT INTO feedback_log
        (cycle_id, agent_id, subject_id, metric, expected, actual,
         deviation, deviation_kind, source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (cycle_id, agent_id, subject_id, metric,
         json.dumps(expected, ensure_ascii=False, default=str),
         json.dumps(actual, ensure_ascii=False, default=str),
         deviation, deviation_kind, source))


# ─────────────────────────────────────────────────────────────────────────────
# I · 改进
# ─────────────────────────────────────────────────────────────────────────────
def propose_param(conn, cycle_id, agent_id, param, new_value, reason, bounds):
    """调参请求。越界就记成 rejected，并保持原值。

    **被拒绝的尝试同样是审计证据**，不能直接丢掉——
    「Agent 反复想把并发调到 20」这件事本身就是一条信息。
    """
    cur = conn.cursor()
    cur.execute("SELECT params FROM agent_state WHERE agent_id = %s", (agent_id,))
    row = cur.fetchone()
    params = dict((row[0] or {}) if row else {})
    old = params.get(param)
    b = bounds.get(param)
    if b is None:
        _log_improve(conn, cycle_id, agent_id, param, old, new_value, reason,
                     {"error": "参数未在 improve.params 里声明"}, True,
                     "未声明的参数不许调——没有区间就等于允许无限自优化")
        return False, "参数 %s 未声明" % param
    if not (b["lo"] <= new_value <= b["hi"]):
        _log_improve(conn, cycle_id, agent_id, param, old, new_value, reason,
                     b, True, "越界：不在 [%s, %s] 内" % (b["lo"], b["hi"]))
        return False, "越界"
    params[param] = new_value
    cur.execute("""UPDATE agent_state SET params = %s, updated_at = now()
                   WHERE agent_id = %s""",
                (json.dumps(params, ensure_ascii=False), agent_id))
    _log_improve(conn, cycle_id, agent_id, param, old, new_value, reason, b, False, None)
    return True, None


def _log_improve(conn, cycle_id, agent_id, param, old, new, reason, bounds,
                 rejected, rejected_why):
    cur = conn.cursor()
    cur.execute("""INSERT INTO improve_log
        (cycle_id, agent_id, param, old_value, new_value, reason, bounded_by,
         rejected, rejected_why)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (cycle_id, agent_id, param,
         json.dumps(old, ensure_ascii=False, default=str),
         json.dumps(new, ensure_ascii=False, default=str),
         reason, json.dumps(bounds, ensure_ascii=False, default=str),
         rejected, rejected_why))


# ─────────────────────────────────────────────────────────────────────────────
# 自检：业务表变更与 agent_signal_log 对不对得上
# ─────────────────────────────────────────────────────────────────────────────
def audit_consistency():
    """返回 (ok, 详情)。给 /health 与运行时测试用。

    **不变式的方向很重要：**
      「每条 review_run 都要有对应的 Act 记录」—— 这是硬约束，
        违反说明有代码绕过了 Act 阶段。
      「每条 Act 记录都要有对应的 review_run」—— **不是约束**。
        业务数据可以被清理（比如重新生成演示数据），而审计记录按设计不可删。
        所以 Act 记录多于 review_run 是正常的，只作为信息报出来。

    第一版写成「两边条数必须相等」，于是一清演示数据测试就红——
    红得没有道理，而「没有道理的红」会让人开始忽略这个检查。
    """
    rows = db.query("""
        SELECT
          (SELECT count(*) FROM review_run)                         AS runs,
          (SELECT count(*) FROM agent_signal_log
            WHERE action = 'write_review_result')                   AS logged_runs,
          (SELECT count(*) FROM review_run r
            WHERE NOT EXISTS (SELECT 1 FROM agent_signal_log l
                              WHERE l.cycle_id = r.cycle_id
                                AND l.action = 'write_review_result')) AS runs_without_act,
          (SELECT count(*) FROM agent_signal)                       AS signals,
          (SELECT count(*) FROM agent_signal_log
            WHERE action = 'emit_signal')                           AS logged_signals,
          (SELECT count(*) FROM data_pool_snapshot WHERE NOT masked) AS unmasked,
          (SELECT count(*) FROM cycle_log WHERE status = 'running'
             AND started_at < now() - INTERVAL '1 hour')            AS stuck
    """)[0]
    rows["orphan_act_records"] = max(0, rows["logged_runs"] - rows["runs"])
    problems = []
    if rows["runs_without_act"]:
        problems.append("有 %d 条 review_run 在 agent_signal_log 里找不到对应的 Act 记录 —— "
                        "说明有代码绕过了 Act 阶段" % rows["runs_without_act"])
    if rows["unmasked"]:
        problems.append("data_pool_snapshot 有 %d 行 masked=FALSE —— "
                        "有一条写入路径绕过了 mask.py" % rows["unmasked"])
    if rows["stuck"]:
        problems.append("有 %d 个周期 running 超过 1 小时 —— 可能是进程崩了留下的孤儿"
                        % rows["stuck"])
    return (not problems), {"counts": rows, "problems": problems}
