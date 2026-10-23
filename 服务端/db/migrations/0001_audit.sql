-- ============================================================================
-- 0001 · Audit-by-Design 五张审计表
-- ----------------------------------------------------------------------------
-- SDAFI 的五个阶段各留一张表，一一对应，不合并：
--   S Sense    → data_pool_snapshot  当时看到的是什么（只读纯 IO 的产物）
--   D Decide   → cycle_log           据此判了什么（不碰库，只产出决策）
--   A Act      → agent_signal_log    实际写了什么（唯一允许写库的阶段）
--   F Feedback → feedback_log        与预期差多少（量化 deviation）
--   I Improve  → improve_log         据此调了哪个内部参数
--
-- 【为什么必须是五张，不能并成一张「日志表」】
--   合成一张之后，「模型看到的输入」和「模型造成的写入」就混在同一批行里，
--   出事后无法回答两个最关键的问题：
--     1) 当时它看到的数据是什么？（没有 S，就只能靠猜）
--     2) 它到底写了什么？（没有 A，就要去业务表反查，而业务表已经被覆盖了）
--   审计的价值全在「能分开追」。
--
-- 【时间列与 hypertable】
--   五张表都按时间分区。TimescaleDB 要求**任何唯一索引都必须包含分区列**，
--   所以主键一律写成 (id, <时间列>)，不是单独的 id。
--   这不是风格问题：写成单列主键会让 create_hypertable 直接报错。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- S · data_pool_snapshot —— Sense 阶段看到的数据快照
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS data_pool_snapshot (
    id              BIGSERIAL,
    taken_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    snapshot_id     TEXT         NOT NULL,              -- 业务语义上的快照号，被 cycle_log 引用
    agent_id        TEXT         NOT NULL,
    agent_version   TEXT         NOT NULL,
    subject_id      TEXT,                               -- 审核主体：报账单号，如 FJPG-BX2610132121
    source_tables   JSONB        NOT NULL,              -- 这次读了哪些表，以及各表读了几行
    payload         JSONB        NOT NULL,              -- 事实模型形态的输入（已脱敏，见 mask.py）
    payload_hash    TEXT         NOT NULL,              -- sha256(payload 的稳定序列化)，用于判「输入是否真的变了」
    masked          BOOLEAN      NOT NULL DEFAULT TRUE, -- 入库前是否过了脱敏。FALSE 不允许出现在生产
    row_counts      JSONB,
    notes           TEXT,
    PRIMARY KEY (id, taken_at)
);

COMMENT ON COLUMN data_pool_snapshot.payload_hash IS
  '输入指纹。F 阶段判「同样的输入是否得到同样的结论」、I 阶段判「调参有没有效果」都要靠它——'
  '没有指纹就只能比对整个 payload，而 payload 里有时间戳一类每次都变的字段，比不出来。';

COMMENT ON COLUMN data_pool_snapshot.masked IS
  '**脱敏开关不是配置，是审计字段。** 单据样本含真实姓名、银行账号、手机号；'
  '快照会被审计接口、调试查询、导出文件读到，这些都是旁路出口。'
  '这一列为 FALSE 的行一旦出现在生产库，说明有一条写入路径绕过了 mask.py。';

-- ─────────────────────────────────────────────────────────────────────────────
-- D · cycle_log —— Decide 阶段的一次完整周期
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cycle_log (
    id                  BIGSERIAL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    cycle_id            TEXT        NOT NULL,
    agent_id            TEXT        NOT NULL,
    agent_version       TEXT        NOT NULL,
    -- 下面四个「版本」字段缺一不可：复现一次结论要同时钉住规则集、模型、提示词、本体
    rule_set_version    TEXT        NOT NULL,
    ontology_version    TEXT        NOT NULL,
    model_version        TEXT,
    prompt_template_id   TEXT,
    subject_id          TEXT,
    snapshot_id         TEXT,                           -- → data_pool_snapshot.snapshot_id
    trigger_kind        TEXT        NOT NULL,           -- message | subscribe | heartbeat
    trigger_reason      TEXT,
    rule_candidates     INTEGER,                        -- 候选集条数
    rule_executed       INTEGER,                        -- 真正执行的条数（被契约校验剔除的不算）
    rule_excluded       JSONB,                          -- 被剔除的规则及原因，**必须记，不能静默跳过**
    rule_hits           JSONB,                          -- 命中的规则与各自的证据
    decision            JSONB,                          -- 结论：pass | review | reject + 理由
    phase_timings_ms    JSONB,                          -- {S:.., D:.., A:.., F:.., I:..}
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    status              TEXT        NOT NULL DEFAULT 'running',  -- running|ok|failed
    error               TEXT,
    PRIMARY KEY (id, started_at)
);

COMMENT ON COLUMN cycle_log.rule_excluded IS
  '**被剔除的规则必须逐条记下来。** 启动时契约校验会把「引用了取不到的字段」的规则剔出执行集，'
  '这是对的；但如果不记，一条本该拦截的闸就悄悄消失了，而且没人知道。'
  '确定性的闸失效而没人发现，比没有这道闸更危险。';

COMMENT ON COLUMN cycle_log.decision IS
  'AI 结论是参考，不是闸门。实测：FJPG-BX2610130821 在 2026-10-13 17:25:03 得到'
  '「AI审核不通过」，17:35:41 仍进入共享审核并由人工给出退回理由。'
  '所以这一列不参与任何流程阻断，只作为人工复核的输入。';

-- ─────────────────────────────────────────────────────────────────────────────
-- A · agent_signal_log —— Act 阶段实际做了什么（唯一允许写库的阶段）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_signal_log (
    id                BIGSERIAL,
    emitted_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal_id         TEXT        NOT NULL,
    cycle_id          TEXT        NOT NULL,
    agent_id          TEXT        NOT NULL,
    action            TEXT        NOT NULL,             -- write_review_result | emit_signal | notify | noop
    target            TEXT,                             -- 目标表或目标 Agent
    payload           JSONB,
    written_tables    JSONB,                            -- 这次动作实际写了哪些表、各几行
    idempotency_key   TEXT        NOT NULL,             -- 同一 key 只允许生效一次
    masked            BOOLEAN     NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id, emitted_at)
);

-- 幂等键唯一。重放一条消息不应该写出第二份结论——
-- 心跳触发与消息触发可能对同一主体同时醒来，没有这个约束就会产生重复审核结果。
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_signal_idem
    ON agent_signal_log (idempotency_key, emitted_at);

COMMENT ON TABLE agent_signal_log IS
  'A 是唯一允许写库的阶段。S 只读、D 不碰库、F 只算偏差、I 只调自己的参数。'
  '这条纪律的可验证形式就是本表：任何业务表的变更都应该能在这里找到对应的一行。'
  '对不上，说明有代码绕过了 Act。tests/test_audit_guard.py 用它做断言。';

-- ─────────────────────────────────────────────────────────────────────────────
-- F · feedback_log —— Feedback 阶段量化偏差
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feedback_log (
    id             BIGSERIAL,
    observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_id       TEXT        NOT NULL,
    agent_id       TEXT        NOT NULL,
    subject_id     TEXT,
    metric         TEXT        NOT NULL,                -- rule_hit_match | conclusion_match | field_read_accuracy | latency_ms ...
    expected       JSONB,
    actual         JSONB,
    deviation      NUMERIC,                             -- 必须是数，不是「差不多」
    deviation_kind TEXT,                                -- false_positive | false_negative | drift | timeout
    source         TEXT        NOT NULL,                -- 偏差从哪来：human_review | golden_set | self_check
    PRIMARY KEY (id, observed_at)
);

COMMENT ON COLUMN feedback_log.deviation_kind IS
  '**误报与漏报必须分开计。** 漏报放过风险；误报把正常单据判成问题件，'
  '审核岗很快就不再信任这套系统。两者合成一个「准确率」会把前者的代价藏起来。'
  '人工复核的退回理由是最重要的 source——实测 50 页审批记录里有真实退回理由可回流。';

-- ─────────────────────────────────────────────────────────────────────────────
-- I · improve_log —— Improve 阶段只调内部参数
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS improve_log (
    id            BIGSERIAL,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cycle_id      TEXT,
    agent_id      TEXT        NOT NULL,
    param         TEXT        NOT NULL,                 -- 只允许 Agent 自己的内部参数
    old_value     JSONB,
    new_value     JSONB,
    reason        TEXT        NOT NULL,
    bounded_by    JSONB       NOT NULL,                 -- 参数的允许区间，证明没越界
    rejected      BOOLEAN     NOT NULL DEFAULT FALSE,   -- 越界的调参请求也要记，记成 rejected
    rejected_why  TEXT,
    PRIMARY KEY (id, applied_at)
);

COMMENT ON TABLE improve_log IS
  'I 阶段**只能调 Agent 自己的内部参数**（如重试次数、批量大小、置信度门限），'
  '不能改规则、不能改阈值、不能改控制类型。规则是制度的投影，改规则是业务决策，'
  '要走发布管理的版本化流程并留人工签字，不是 Agent 自己能动的。'
  '越界的调参请求记成 rejected=TRUE，而不是直接丢掉——被拒绝的尝试同样是审计证据。';

COMMENT ON COLUMN improve_log.bounded_by IS
  '每次调参都要带上当时的允许区间。只记「从 3 调到 5」无法事后判断这个 5 是否合法，'
  '因为区间本身也会随版本变。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 索引：按审计的三种真实查法建，不是见列就建
--   1) 查一张单的全过程        → subject_id
--   2) 查一个周期的五个阶段    → cycle_id
--   3) 查某段时间的某个 Agent  → (agent_id, 时间)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS ix_snapshot_subject ON data_pool_snapshot (subject_id, taken_at DESC);
CREATE INDEX IF NOT EXISTS ix_snapshot_sid     ON data_pool_snapshot (snapshot_id);
CREATE INDEX IF NOT EXISTS ix_snapshot_agent   ON data_pool_snapshot (agent_id, taken_at DESC);

CREATE INDEX IF NOT EXISTS ix_cycle_subject    ON cycle_log (subject_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_cycle_cid        ON cycle_log (cycle_id);
CREATE INDEX IF NOT EXISTS ix_cycle_agent      ON cycle_log (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_cycle_status     ON cycle_log (status, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_signal_cycle     ON agent_signal_log (cycle_id);
CREATE INDEX IF NOT EXISTS ix_signal_agent     ON agent_signal_log (agent_id, emitted_at DESC);

CREATE INDEX IF NOT EXISTS ix_feedback_cycle   ON feedback_log (cycle_id);
CREATE INDEX IF NOT EXISTS ix_feedback_metric  ON feedback_log (metric, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_kind    ON feedback_log (deviation_kind, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_improve_agent    ON improve_log (agent_id, applied_at DESC);
