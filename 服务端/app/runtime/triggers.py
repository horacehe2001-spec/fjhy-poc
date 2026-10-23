# -*- coding: utf-8 -*-
"""三种触发：心跳 / 消息 / 订阅。载体只有 PostgreSQL，没有 Redis、没有 MQ。

  message   毫秒~秒   LISTEN 到通知后立刻取信号处理
  subscribe 秒~分     LISTEN 自己关心的 channel（与 message 同机制，语义不同：
                      message 是点对点派给我的任务，subscribe 是广播的事实变更）
  heartbeat 30秒~5分  定时扫数据池

【心跳为什么不是可选项】
  LISTEN/NOTIFY 是**尽力而为**的：连接断开期间发出的通知收不到，
  而且不报错、不重发。只靠通知，一次重连就会静默丢任务，
  表现为「这张单永远没被审，也没人知道」。
  心跳做三件事兜底：
    1. 扫出没有成功审核记录的单据（补漏）
    2. 回收孤儿信号——claimed 了但处理进程崩掉的，超时放回 pending
    3. 统计「心跳发现而订阅没发现」的数量，这个数 > 0 就说明通知丢过

【为什么不上 MQ】
  上了 MQ，消息就离开了数据库的事务边界：写业务表成功、发消息失败，
  或者反过来，都会产生「审计记录与实际发生的事不一致」，
  而 Audit-by-Design 的全部价值就在这两者一致。
  用表装信号，写信号和写业务表在同一个事务里，要么都成要么都不成。
"""
import asyncio
import json
import time

from .. import config, db
from ..audit import recorder as rec


# ─────────────────────────────────────────────────────────────────────────────
# 信号的取用：claim → done / dead
# ─────────────────────────────────────────────────────────────────────────────
def claim_signals(channel, agent_id, limit=10):
    """原子地取一批信号。

    用 `FOR UPDATE SKIP LOCKED`：多个消费者并发取时互不阻塞，
    也不会取到同一条。没有 SKIP LOCKED 的话，两个进程会在同一行上排队，
    并发就退化成串行。
    """
    rows = db.query("""
        UPDATE agent_signal SET status='claimed', claimed_by=%s, claimed_at=now(),
                                attempts = attempts + 1
        WHERE id IN (
            SELECT id FROM agent_signal
            WHERE channel = %s AND status = 'pending' AND not_before <= now()
            ORDER BY priority, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s)
        RETURNING signal_id, channel, kind, subject_id, payload, attempts, max_attempts
    """, (agent_id, channel, limit))
    return rows


def finish_signal(signal_id, ok=True, error=None):
    if ok:
        db.execute("UPDATE agent_signal SET status='done', done_at=now() "
                   "WHERE signal_id=%s", (signal_id,))
        return "done"
    row = db.query_one("SELECT attempts, max_attempts FROM agent_signal "
                       "WHERE signal_id=%s", (signal_id,))
    if row and row["attempts"] >= row["max_attempts"]:
        # 重试到顶就置 dead，**不是删掉**。死信留着才知道「这张单一直没审成」。
        db.execute("UPDATE agent_signal SET status='dead', last_error=%s "
                   "WHERE signal_id=%s", (error, signal_id))
        return "dead"
    # 指数退避：1 分、2 分、4 分
    backoff = 60 * (2 ** max(0, (row["attempts"] if row else 1) - 1))
    db.execute("UPDATE agent_signal SET status='pending', last_error=%s, "
               "not_before = now() + (%s || ' seconds')::interval WHERE signal_id=%s",
               (error, backoff, signal_id))
    return "retry"


def reclaim_orphans(timeout_seconds=None):
    """回收孤儿信号：claimed 了但很久没完成的。

    不回收的后果：处理进程崩一次，那条信号就永久停在 claimed，
    对应的单据永远不会被审——**而且没有任何报错**。
    """
    t = timeout_seconds or config.CLAIM_TIMEOUT_SECONDS
    t0 = time.time()
    n = db.execute("""UPDATE agent_signal SET status='pending', claimed_by=NULL,
                      last_error = coalesce(last_error,'') || ' [孤儿回收]'
                      WHERE status='claimed'
                        AND claimed_at < now() - (%s || ' seconds')::interval""", (t,))
    if n:
        db.execute("""INSERT INTO pool_maintenance_log (task, affected, detail, duration_ms)
                      VALUES ('orphan_reclaim', %s, %s, %s)""",
                   (n, json.dumps({"timeout_seconds": t}),
                    int((time.time() - t0) * 1000)))
    return n


def cleanup_signals(keep_days=7):
    """清掉久远的 done 信号。**清理动作本身要留痕。**

    「某段时间的信号为什么是空的」——答案要么在 pool_maintenance_log 里，
    要么就说不清了。
    """
    t0 = time.time()
    n = db.execute("""DELETE FROM agent_signal
                      WHERE status='done' AND done_at < now() - (%s || ' days')::interval""",
                   (keep_days,))
    db.execute("""INSERT INTO pool_maintenance_log (task, affected, detail, duration_ms)
                  VALUES ('signal_cleanup', %s, %s, %s)""",
               (n, json.dumps({"keep_days": keep_days}), int((time.time() - t0) * 1000)))
    return n


# ─────────────────────────────────────────────────────────────────────────────
# 心跳：扫数据池找漏
# ─────────────────────────────────────────────────────────────────────────────
def heartbeat_scan(rule_set_version, limit=50):
    """找出「没有成功审核记录」的单据。

    判据是「没有 status 为 ok 的 review_run」，不是「没有 review_run」——
    失败的审核记录留着是为了可追溯，但不能因此认为这张单审过了。
    """
    return [r["bill_no"] for r in db.query("""
        SELECT b.bill_no FROM expense_bill b
        WHERE NOT EXISTS (
            SELECT 1 FROM review_run r
            WHERE r.bill_no = b.bill_no AND r.rule_set_version = %s
              AND r.conclusion IS NOT NULL)
        ORDER BY b.doc_date NULLS LAST, b.bill_no
        LIMIT %s""", (rule_set_version, limit))]


# ─────────────────────────────────────────────────────────────────────────────
# LISTEN：消息与订阅
# ─────────────────────────────────────────────────────────────────────────────
class Listener:
    """独立连接上的 LISTEN。**不从连接池拿连接。**

    池会在空闲时回收连接，而 LISTEN 注册是连接级的——
    连接一回收订阅就静默失效：没有报错、没有日志，只是从此收不到通知。
    这正是心跳必须存在的原因之一。
    """

    def __init__(self, channels):
        self.channels = list(channels)
        self.conn = None
        self.queue = asyncio.Queue()
        self.received = 0

    async def start(self):
        self.conn = await db.listen_connection()
        for ch in self.channels:
            await self.conn.add_listener(ch, self._on_notify)
        return self

    def _on_notify(self, conn, pid, channel, payload):
        self.received += 1
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {"raw": payload}
        self.queue.put_nowait({"channel": channel, "payload": data})

    async def drain(self, timeout=0.5):
        """取出当前排队的通知。超时返回空列表，不阻塞主循环。"""
        out = []
        try:
            while True:
                out.append(await asyncio.wait_for(self.queue.get(), timeout))
                timeout = 0.01            # 第一条等久一点，后面立刻收完
        except asyncio.TimeoutError:
            pass
        return out

    async def close(self):
        if self.conn:
            for ch in self.channels:
                try:
                    await self.conn.remove_listener(ch, self._on_notify)
                except Exception:
                    pass
            await self.conn.close()
            self.conn = None
