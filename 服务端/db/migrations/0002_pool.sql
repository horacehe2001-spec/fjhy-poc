-- ============================================================================
-- 0002 · 数据池（唯一介质）
-- ----------------------------------------------------------------------------
-- SDAFI 规定数据池是 Agent 之间唯一的通信介质：**没有 Redis、没有 MQ**。
-- 全部靠 PostgreSQL 表 + LISTEN/NOTIFY。
--
-- 【为什么不上 MQ】
--   上了 MQ，消息就离开了数据库的事务边界：A 阶段写业务表成功、发消息失败，
--   或者反过来，都会产生「审计记录与实际发生的事不一致」。
--   而 Audit-by-Design 的全部价值就在于这两者一致。
--   用表装信号，写信号和写业务表在同一个事务里，要么都成要么都不成。
--
-- 【三种触发对应到这里】
--   message   毫秒~秒   → agent_signal 插入后由触发器 NOTIFY，订阅方立刻醒
--   subscribe 秒~分     → Agent LISTEN 自己关心的 channel
--   heartbeat 30秒~5分  → 不依赖本表，定时扫 agent_signal 的积压 + 扫业务表的待处理
--   心跳是兜底：NOTIFY 是「尽力而为」的，连接断开期间发的通知收不到。
--   只靠 NOTIFY 会在重连窗口丢任务且无人察觉，所以心跳不是可选项。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 信号队列
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_signal (
    id                BIGSERIAL PRIMARY KEY,
    signal_id         TEXT        NOT NULL UNIQUE,
    channel           TEXT        NOT NULL,            -- NOTIFY 的 channel，也是订阅的粒度
    from_agent        TEXT,
    to_agent          TEXT,                            -- NULL 表示广播给该 channel 的所有订阅方
    subject_id        TEXT,
    kind              TEXT        NOT NULL,            -- bill_submitted | review_requested | review_done | rule_published ...
    payload           JSONB       NOT NULL DEFAULT '{}'::JSONB,
    priority          SMALLINT    NOT NULL DEFAULT 100,
    status            TEXT        NOT NULL DEFAULT 'pending',  -- pending|claimed|done|dead
    claimed_by        TEXT,
    claimed_at        TIMESTAMPTZ,
    attempts          SMALLINT    NOT NULL DEFAULT 0,
    max_attempts      SMALLINT    NOT NULL DEFAULT 3,
    last_error        TEXT,
    not_before        TIMESTAMPTZ NOT NULL DEFAULT now(),       -- 重试退避
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at           TIMESTAMPTZ,
    idempotency_key   TEXT,
    CONSTRAINT ck_signal_status CHECK (status IN ('pending','claimed','done','dead'))
);

-- 同一个幂等键只许有一条「活着」的信号。
-- 心跳和 NOTIFY 可能对同一张单同时投递，没有这个约束就会审两遍、写两份结论。
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_idem_alive
    ON agent_signal (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND status IN ('pending','claimed');

-- 取任务的那条查询：按 channel + 可见时间 + 优先级。只给 pending 建部分索引，
-- 因为 done 的行会越积越多，而取任务永远不看它们。
CREATE INDEX IF NOT EXISTS ix_signal_pending
    ON agent_signal (channel, not_before, priority, id)
    WHERE status = 'pending';

-- 心跳要查「claimed 了但很久没完成」的（进程崩了留下的孤儿）
CREATE INDEX IF NOT EXISTS ix_signal_claimed
    ON agent_signal (claimed_at)
    WHERE status = 'claimed';

-- ─────────────────────────────────────────────────────────────────────────────
-- 插入信号即 NOTIFY
-- ─────────────────────────────────────────────────────────────────────────────
-- 用触发器而不是在应用层发 NOTIFY，理由：触发器在事务内执行，
-- 事务回滚则通知不会发出。应用层发通知做不到这一点——
-- 代码里「先 commit 再 notify」会在两步之间崩掉丢通知，
-- 「先 notify 再 commit」会在回滚后留下一个指向不存在的行的通知。
CREATE OR REPLACE FUNCTION fn_notify_agent_signal() RETURNS TRIGGER AS $$
BEGIN
    -- payload 只带 signal_id 与主体号，**不带业务内容**。
    -- 两个理由：一是 NOTIFY 的 payload 上限 8000 字节，带业务内容会超；
    -- 二是通知内容会进 PostgreSQL 的通知队列，那是又一个出口，
    -- 带了姓名账号就等于绕过了脱敏。订阅方拿到 id 自己回表读。
    PERFORM pg_notify(NEW.channel,
                      json_build_object('signal_id', NEW.signal_id,
                                        'kind',      NEW.kind,
                                        'subject_id', NEW.subject_id)::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_notify_agent_signal ON agent_signal;
CREATE TRIGGER tr_notify_agent_signal
    AFTER INSERT ON agent_signal
    FOR EACH ROW EXECUTE FUNCTION fn_notify_agent_signal();

-- ─────────────────────────────────────────────────────────────────────────────
-- Agent 运行态与内部参数（I 阶段写这里，不写别处）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_state (
    agent_id       TEXT        PRIMARY KEY,
    agent_version  TEXT        NOT NULL,
    params         JSONB       NOT NULL DEFAULT '{}'::JSONB,   -- 只放内部参数
    param_bounds   JSONB       NOT NULL DEFAULT '{}'::JSONB,   -- 每个参数的允许区间，启动时从 YAML 载入
    last_cycle_id  TEXT,
    last_beat_at   TIMESTAMPTZ,
    enabled        BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN agent_state.param_bounds IS
  '区间来自 Agent 的 YAML 配置，启动时载入、运行期不变。I 阶段调参前先查这里，'
  '越界就记 improve_log(rejected=TRUE) 并保持原值。'
  '把区间也放进 JSONB 而不是写死在代码里，是为了让「允许自优化到什么程度」'
  '成为一个可审阅的配置项，而不是埋在代码里的魔数。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 保留策略相关：信号表会无限增长，清理动作本身也要可审计
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pool_maintenance_log (
    id           BIGSERIAL PRIMARY KEY,
    ran_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    task         TEXT        NOT NULL,      -- signal_cleanup | retention | orphan_reclaim
    affected     INTEGER     NOT NULL,
    detail       JSONB,
    duration_ms  INTEGER
);

COMMENT ON TABLE pool_maintenance_log IS
  '清理动作删掉的是审计相关的数据，所以清理本身必须留痕。'
  '「某段时间的审计记录为什么是空的」——答案要么在这张表里，要么就说不清了。';
