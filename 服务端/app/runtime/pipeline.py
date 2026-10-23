# -*- coding: utf-8 -*-
"""S→D→A→F→I 五阶段管道。

每个阶段的边界是硬的，由 app/audit/recorder.PhaseGuard 执行：
  S Sense    只读纯 IO。捞快照、算指纹、脱敏入库。
  D Decide   不碰库。跑规则，产出结论与证据。
  A Act      **唯一允许写业务数据的阶段。** 写审核结果、投信号。
  F Feedback 只算偏差。拿人工结论比对，分别计误报与漏报。
  I Improve  只调自己的内部参数，且必须在 YAML 声明的区间内。

【为什么 D 不许碰库】
  D 里再查一次库，就可能在同一个周期内读到两份不同的数据（中间有人改了单据）。
  而 data_pool_snapshot 记的是 S 阶段那一份，事后复查时
  「审计记录里的输入」和「当时真正用的输入」对不上，整套审计就没有意义了。

【A 阶段的事务边界】
  写审核结果、投信号、记 agent_signal_log 必须在**同一个事务**里。
  分成两个事务会出现「业务数据变了但审计记录没留下」，
  而 Audit-by-Design 的全部价值就在这两者一致。
"""
import json
import time

from .. import db, mask
from ..audit import recorder as rec
from ..perception import reader
from ..rules import engine
from ..rules import impl  # noqa: F401  导入即注册规则实现


class CycleResult:
    def __init__(self, cycle_id, subject_id):
        self.cycle_id = cycle_id
        self.subject_id = subject_id
        self.conclusion = None
        self.stats = {}
        self.timings = {}
        self.findings = []
        self.status = "ok"
        self.error = None
        self.skipped = None

    def as_dict(self):
        return {"cycle_id": self.cycle_id, "subject_id": self.subject_id,
                "conclusion": self.conclusion, "stats": self.stats,
                "timings_ms": self.timings, "status": self.status,
                "error": self.error, "skipped": self.skipped}


def run_cycle(agent_cfg, ruleset, subject_id, trigger_kind, trigger_reason,
              standards=None, tier_map=None, versions=None, do_feedback=True):
    """跑一张单的完整周期。返回 CycleResult。"""
    cycle_id = rec.new_cycle_id()
    res = CycleResult(cycle_id, subject_id)
    versions = dict(versions or {})
    versions.setdefault("rule_set", ruleset.rule_set_version)
    versions.setdefault("ontology", ruleset.ontology_version)
    t_all = time.time()

    # ── S ───────────────────────────────────────────────────────────────────
    t = time.time()
    with rec.phase("S"):
        fact, meta = reader.snapshot_fact(subject_id)
        if fact is None:
            res.status = "failed"
            res.error = "单据 %s 不存在" % subject_id
            return res
        payload, masked, leaks = reader.masked_payload(fact)
        with db.tx() as conn:
            rec.record_snapshot(conn, agent_cfg.agent_id, agent_cfg.agent_version,
                                meta, payload, masked, leaks)
            rec.start_cycle(conn, cycle_id, agent_cfg.agent_id,
                            agent_cfg.agent_version, versions, subject_id,
                            meta["snapshot_id"], trigger_kind, trigger_reason)
    res.timings["S"] = int((time.time() - t) * 1000)
    if not masked:
        # 脱敏自检没过就不往下走。快照已经入库并标了 masked=FALSE，
        # 这件事必须能被发现——继续跑只会让泄露的数据流进更多出口。
        res.status = "failed"
        res.error = "脱敏自检发现 %d 处泄露，拒绝继续：%s" % (len(leaks), leaks[:3])
        with db.tx() as conn:
            rec.finish_cycle(conn, cycle_id, {}, {}, [], [], res.timings,
                             status="failed", error=res.error)
        return res

    # ── D ───────────────────────────────────────────────────────────────────
    t = time.time()
    try:
        with rec.phase("D"):
            findings, stats = engine.evaluate(ruleset, fact, standards, tier_map)
            conclusion = engine.conclude(findings, ruleset)
    except Exception as exc:
        res.status = "failed"
        res.error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        res.timings["D"] = int((time.time() - t) * 1000)
        with db.tx() as conn:
            rec.finish_cycle(conn, cycle_id, {}, {}, [], [], res.timings,
                             status="failed", error=res.error)
        return res
    res.timings["D"] = int((time.time() - t) * 1000)
    res.findings = findings
    res.stats = stats
    res.conclusion = conclusion

    excluded = [{"rule_id": e["rule_id"], "name": e["name"],
                 "control_type": e["control_type"],
                 "why": (e["demo_blocker"] or e["compiled_reason"] or "")[:300]}
                for e in ruleset.excluded]
    rule_hits = [{"rule_id": f.rule_id, "outcome": f.outcome,
                  "message": f.message, "evidence": f.evidence}
                 for f in findings if f.outcome in ("hit", "pending")]

    # ── A ───────────────────────────────────────────────────────────────────
    t = time.time()
    snap_hash = meta["payload_hash"][:16]
    idem = "%s:%s:%s" % (subject_id, versions["rule_set"], snap_hash)
    with rec.phase("A"):
        with db.tx() as conn:
            cur = conn.cursor()
            # 幂等：同一张单 + 同一规则集 + 同一快照，只产出一份结论
            cur.execute("""SELECT run_id FROM review_run
                           WHERE bill_no=%s AND rule_set_version=%s
                             AND cycle_id IS NOT NULL
                             AND run_id = %s""", (subject_id, versions["rule_set"], idem))
            if cur.fetchone():
                res.skipped = "幂等命中：同一快照已有结论（%s）" % idem
                rec.finish_cycle(conn, cycle_id, {"conclusion": conclusion, "skipped": True},
                                 stats, excluded, rule_hits, res.timings)
                return res
            written = _write_review(conn, idem, subject_id, cycle_id, agent_cfg,
                                    versions, conclusion, stats, excluded, findings)
            rec.record_action(conn, cycle_id, agent_cfg.agent_id,
                              "write_review_result", "review_run",
                              {"conclusion": conclusion, "bill_no": subject_id},
                              written, idem)
            sig = (agent_cfg.act.get("signal") or {})
            if sig.get("channel"):
                sid = rec.emit_signal(conn, sig["channel"], sig.get("kind") or "review_done",
                                      agent_cfg.agent_id, sig.get("to_agent"),
                                      subject_id, {"conclusion": conclusion}, idem + ":done")
                rec.record_action(conn, cycle_id, agent_cfg.agent_id, "emit_signal",
                                  sig["channel"], {"signal_id": sid},
                                  {"agent_signal": 1 if sid else 0}, idem + ":done")
            rec.finish_cycle(conn, cycle_id, {"conclusion": conclusion}, stats,
                             excluded, rule_hits, res.timings)
    res.timings["A"] = int((time.time() - t) * 1000)

    # ── F ───────────────────────────────────────────────────────────────────
    if do_feedback:
        t = time.time()
        with rec.phase("F"):
            _feedback(cycle_id, agent_cfg, subject_id, conclusion, stats, findings)
        res.timings["F"] = int((time.time() - t) * 1000)

    res.timings["total"] = int((time.time() - t_all) * 1000)
    with db.tx() as conn:
        conn.cursor().execute(
            "UPDATE cycle_log SET phase_timings_ms = %s WHERE cycle_id = %s",
            (json.dumps(res.timings), cycle_id))
    return res


def _write_review(conn, run_id, bill_no, cycle_id, cfg, versions, conclusion,
                  stats, excluded, findings):
    """写审核结果。**必须经 guard_write**，证明是在 A 阶段。"""
    rec.guard_write("review_run")
    cur = conn.cursor()
    cur.execute("""INSERT INTO review_run
        (run_id, bill_no, cycle_id, rule_set_version, ontology_version, agent_id,
         conclusion, rule_candidates, rule_executed, rule_excluded, hit_count,
         blocked_count, warned_count, pending_count, duration_ms, is_demo, finished_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                (SELECT is_demo FROM expense_bill WHERE bill_no=%s), now())""",
        (run_id, bill_no, cycle_id, versions["rule_set"], versions["ontology"],
         cfg.agent_id, conclusion, stats["candidates"], stats["executed"],
         json.dumps(excluded, ensure_ascii=False), stats["hit"],
         stats["blocked"], stats["warned"], stats["pending"],
         stats.get("duration_ms"), bill_no))
    n_find = 0
    rec.guard_write("review_finding")
    for f in findings:
        rule = None
        cur.execute("SELECT rule_class, control_type FROM rule WHERE rule_id=%s AND version=1",
                    (f.rule_id,))
        rule = cur.fetchone()
        rule_class = rule[0] if rule else "hard"
        control = rule[1] if rule else "警告"
        # 软规则命中必须要求人工复核。库里有 CHECK 约束钉着（ck_soft_needs_human），
        # 所以这里不能只靠 Finding 自己带的标记。
        needs_human = bool(f.needs_human) or (rule_class == "soft" and f.outcome == "hit")
        cur.execute("""INSERT INTO review_finding
            (run_id, rule_id, rule_version, rule_class, control_type, outcome,
             message, evidence, clause_ref, concepts_used, matched_text,
             pending_reason, llm_involved, llm_rationale, needs_human)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, f.rule_id, rule_class, control, f.outcome,
             mask.mask_text(f.message),
             json.dumps(mask.scrub(f.evidence or {}), ensure_ascii=False, default=str),
             json.dumps(f.clause_ref or {}, ensure_ascii=False, default=str),
             json.dumps([], ensure_ascii=False),
             mask.mask_text(f.matched_text) if f.matched_text else None,
             mask.mask_text(f.pending_reason) if f.pending_reason else None,
             bool(f.llm_involved), f.llm_rationale, needs_human))
        n_find += 1
    return {"review_run": 1, "review_finding": n_find}


def _feedback(cycle_id, cfg, bill_no, conclusion, stats, findings):
    """F 阶段：拿人工结论比对，**分别**计误报与漏报。

    人工真值来自每张单最后一条共享审核意见。
    没有人工结论的单（还没审到）不记偏差——那不是一致也不是不一致，是没结果。
    """
    row = db.query_one("""SELECT opinion FROM approval_record
                          WHERE bill_no=%s AND node='共享审核'
                          ORDER BY seq DESC LIMIT 1""", (bill_no,))
    with db.tx() as conn:
        # 待人工占比：这个数高不一定坏，但一直不降说明缺口没在补
        total = max(stats["executed"], 1)
        rec.record_feedback(conn, cycle_id, cfg.agent_id, bill_no,
                            "pending_ratio", {"target": "< 0.3"},
                            {"pending": stats["pending"], "executed": stats["executed"]},
                            round(stats["pending"] / total, 4), None, "self_check")
        if row is None:
            rec.record_feedback(conn, cycle_id, cfg.agent_id, bill_no,
                                "conclusion_vs_human", {"human": None},
                                {"ai": conclusion}, None, None, "human_review")
            return
        human_pass = (row["opinion"] or "").strip() == "同意"
        n_hit = stats["hit"]
        kind = None
        dev = 0.0
        # **「判不了」不是「判错了」，两者必须分开。**
        #   AI 命中了规则、人工却放行      → false_positive（真误报，代价最高）
        #   AI 只是有待人工项、人工放行    → needs_review_only（不是误报，是没把握）
        #   AI 放行、人工退回              → false_negative（漏报）
        # 第一版把 review 一律算成 false_positive，于是 40 张真实单里
        # 9 张「只有待人工」的被记成误报——虚报了误报率，
        # 而误报率恰恰是审核岗决定信不信任这套系统的依据。
        if human_pass and n_hit > 0:
            kind, dev = "false_positive", 1.0
        elif human_pass and conclusion == "review":
            kind, dev = "needs_review_only", 0.0
        elif (not human_pass) and conclusion == "pass":
            kind, dev = "false_negative", 1.0
        rec.record_feedback(conn, cycle_id, cfg.agent_id, bill_no,
                            "conclusion_vs_human",
                            {"human": "同意" if human_pass else "退回/补充"},
                            {"ai": conclusion}, dev, kind, "human_review")
        # 逐条命中也记：人工放行时，每一条命中都是一次待核的疑似误报
        if human_pass:
            for f in findings:
                if f.outcome == "hit":
                    rec.record_feedback(conn, cycle_id, cfg.agent_id, bill_no,
                                        "finding_vs_human", {"human": "同意"},
                                        {"rule_id": f.rule_id, "message": f.message[:120]},
                                        1.0, "false_positive", "human_review")
