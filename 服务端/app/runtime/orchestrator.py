# -*- coding: utf-8 -*-
"""AgentOrchestrator：把 Agent 配置、触发器、管道接起来。

职责：
  启动时  契约校验（规则资产）+ Agent 配置校验 → 任一不过就不启动（Fail Fast）
  运行期  心跳 / 消息 / 订阅三条路驱动周期；跑完一批做 I 阶段
  停机    关掉 LISTEN 连接，把 claimed 的信号放回 pending

【I 阶段放在批次末尾，不是每个周期一次】
  调参要看一批的统计（P90 耗时、待人工占比），单个周期的数字噪声太大，
  按单个周期调会让参数抖来抖去。
"""
import asyncio
import json
import statistics
import time

from .. import config, db
from ..audit import recorder as rec
from ..rules import engine
from ..perception import reader
from . import contract, pipeline, triggers, yaml_loader


class Orchestrator:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.agents = {}
        self.ruleset = None
        self.standards = []
        self.tier_map = []
        self.listeners = {}
        self.running = False
        self.stats = {"cycles": 0, "ok": 0, "failed": 0, "skipped": 0,
                      "by_trigger": {}, "heartbeat_found": 0, "notify_found": 0}

    # ── 启动 ────────────────────────────────────────────────────────────────
    def startup(self):
        """Fail Fast。两道校验都过才算启动成功。"""
        if self.verbose:
            print("=" * 72)
            print("启动自检")
        cv = contract.verify(verbose=False)
        if self.verbose:
            print("  规则资产契约：%d 项，失败 %d，警告 %d；执行集 %d / 剔除 %d"
                  % (len(cv.checks), len(cv.errors), len(cv.warnings),
                     len(cv.executable), len(cv.excluded)))
        if not cv.ok and config.CONTRACT_MODE == "fail_fast":
            raise SystemExit("** 规则资产契约校验失败，拒绝启动：\n%s"
                             % "\n".join("   - " + e for e in cv.errors))
        ac = yaml_loader.load(verbose=False)
        if self.verbose:
            print("  Agent 配置：%d 个，检查项 %d，失败 %d"
                  % (len(ac.agents), len(ac.checks), len(ac.errors)))
        if not ac.ok:
            raise SystemExit("** Agent 配置校验失败，拒绝启动：\n%s"
                             % "\n".join("   - " + e for e in ac.errors))
        self.agents = ac.agents
        self._init_agent_state()
        self.ruleset = engine.load_ruleset()
        self.standards = reader.standards()
        self.tier_map = reader.tier_map()
        missing = self.ruleset.missing_impl
        if missing:
            raise SystemExit("** 执行集里有 %d 条规则没有实现，拒绝启动：%s\n"
                             "   契约校验说它该跑，而代码里没有对应实现——"
                             "它既不在剔除名单里、也不会产生任何结论，报告上看不出少了一条。"
                             % (len(missing), missing))
        if self.verbose:
            print("  规则集 %s / 本体 %s；执行集 %d 条，全部有实现"
                  % (self.ruleset.rule_set_version, self.ruleset.ontology_version,
                     len(self.ruleset)))
            print("=" * 72)
        return self

    def _init_agent_state(self):
        with db.tx() as conn:
            cur = conn.cursor()
            for a in self.agents.values():
                cur.execute("""INSERT INTO agent_state
                        (agent_id, agent_version, params, param_bounds, last_beat_at)
                        VALUES (%s,%s,%s,%s,now())
                        ON CONFLICT (agent_id) DO UPDATE SET
                            agent_version = EXCLUDED.agent_version,
                            param_bounds  = EXCLUDED.param_bounds,
                            updated_at    = now()""",
                    (a.agent_id, a.agent_version,
                     json.dumps(a.params, ensure_ascii=False),
                     json.dumps(a.param_bounds, ensure_ascii=False)))

    def param(self, agent_id, name, default=None):
        row = db.query_one("SELECT params FROM agent_state WHERE agent_id=%s", (agent_id,))
        return ((row or {}).get("params") or {}).get(name, default)

    # ── 一批 ────────────────────────────────────────────────────────────────
    def run_batch(self, subject_ids, trigger_kind, trigger_reason, agent_id=None):
        cfg = self.agents[agent_id or "fj_l1_01_travel_review"]
        results = []
        for sid in subject_ids:
            r = pipeline.run_cycle(cfg, self.ruleset, sid, trigger_kind,
                                   trigger_reason, self.standards, self.tier_map)
            results.append(r)
            self.stats["cycles"] += 1
            self.stats["by_trigger"][trigger_kind] = \
                self.stats["by_trigger"].get(trigger_kind, 0) + 1
            if r.status != "ok":
                self.stats["failed"] += 1
            elif r.skipped:
                self.stats["skipped"] += 1
            else:
                self.stats["ok"] += 1
        if results:
            self.improve(cfg, results)
        return results

    # ── I 阶段 ──────────────────────────────────────────────────────────────
    def improve(self, cfg, results):
        """只调自己的内部参数，且必须在 YAML 声明的区间内。

        依据是一批的统计，不是单个周期——单周期的数字噪声太大，
        按它调会让参数抖来抖去。
        """
        done = [r for r in results if r.status == "ok" and not r.skipped]
        if not done:
            return
        durations = sorted(r.timings.get("total", 0) for r in done)
        p90 = durations[min(len(durations) - 1, int(len(durations) * 0.9))]
        bounds = cfg.param_bounds
        cur_conc = self.param(cfg.agent_id, "max_concurrent", 3)
        with rec.phase("I"):
            with db.tx() as conn:
                # P90 超过 3 秒就降并发；低于 1 秒且没到上限就升。
                # 这是 Agent 唯一能动的东西：自己跑多快。
                if p90 > 3000 and cur_conc > bounds.get("max_concurrent", {}).get("lo", 1):
                    rec.propose_param(conn, None, cfg.agent_id, "max_concurrent",
                                      cur_conc - 1,
                                      "P90 周期耗时 %d ms 超过 3000 ms" % p90, bounds)
                elif p90 < 1000:
                    rec.propose_param(conn, None, cfg.agent_id, "max_concurrent",
                                      cur_conc + 1,
                                      "P90 周期耗时 %d ms 低于 1000 ms，可提并发" % p90,
                                      bounds)

    # ── 心跳一轮 ────────────────────────────────────────────────────────────
    def heartbeat_once(self, limit=None):
        n_orphan = triggers.reclaim_orphans()
        limit = limit or self.param("fj_l0_01_bill_perception", "max_batch", 50)
        found = triggers.heartbeat_scan(self.ruleset.rule_set_version, limit)
        self.stats["heartbeat_found"] += len(found)
        if self.verbose and (found or n_orphan):
            print("  心跳：回收孤儿 %d，发现未审 %d" % (n_orphan, len(found)))
        return self.run_batch(found, "heartbeat", "心跳扫描") if found else []

    # ── 异步主循环 ──────────────────────────────────────────────────────────
    async def serve(self, max_seconds=None, heartbeat_seconds=None):
        """跑主循环。max_seconds 给演示和测试用；不传就一直跑。"""
        self.running = True
        hb = heartbeat_seconds or config.HEARTBEAT_SECONDS
        chans = set()
        for a in self.agents.values():
            for k in ("message", "subscribe"):
                t = a.trigger(k) or {}
                chans.update(t.get("channels") or [])
        lis = triggers.Listener(sorted(chans))
        await lis.start()
        if self.verbose:
            print("已 LISTEN：%s" % ", ".join(sorted(chans)))
        t0 = time.time()
        last_hb = 0.0
        try:
            while self.running:
                if max_seconds and (time.time() - t0) > max_seconds:
                    break
                msgs = await lis.drain(timeout=0.5)
                if msgs:
                    subjects, seen = [], set()
                    for m in msgs:
                        sid = (m["payload"] or {}).get("bill_no") or \
                              (m["payload"] or {}).get("subject_id")
                        if sid and sid not in seen:
                            seen.add(sid)
                            subjects.append(sid)
                    self.stats["notify_found"] += len(subjects)
                    if subjects:
                        if self.verbose:
                            print("  通知：%d 条 → %d 个主体" % (len(msgs), len(subjects)))
                        await asyncio.to_thread(self.run_batch, subjects,
                                                "message", "LISTEN/NOTIFY")
                if time.time() - last_hb >= hb:
                    last_hb = time.time()
                    await asyncio.to_thread(self.heartbeat_once)
                    db.execute("UPDATE agent_state SET last_beat_at = now()")
                await asyncio.sleep(0.1)
        finally:
            await lis.close()
            self.stats["notify_received"] = lis.received
            # 停机时把 claimed 放回 pending，别留下孤儿
            back = db.execute("UPDATE agent_signal SET status='pending', claimed_by=NULL "
                              "WHERE status='claimed'")
            if self.verbose and back:
                print("停机：%d 条 claimed 信号已放回 pending" % back)
        return self.stats

    def stop(self):
        self.running = False
