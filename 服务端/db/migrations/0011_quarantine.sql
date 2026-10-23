-- ============================================================================
-- 0011 · 装载隔离区
-- ----------------------------------------------------------------------------
-- 给「引用完整性对不上」的行一个去处。实测踩到的那一例：
--   多人样本里 FJPG-BX2609041860 有 8 条费用明细，但「2多人差旅报账单」主表里
--   没有这张单，报销附件表也没有。
--
-- 三种处置里只有一种是对的：
--   静默丢弃  → 这 8 行费用从此不被任何规则看到，而且没人知道。错。
--   凭空造单  → 后面所有按核算主体/业务类型判的规则会拿到编出来的值。更错。
--   进隔离区  → 数据留着、原因写明、计数可查，由人决定怎么补。对。
--
-- 缺口 G-30。
-- ============================================================================

CREATE TABLE IF NOT EXISTS load_quarantine (
    id          BIGSERIAL PRIMARY KEY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source      TEXT    NOT NULL,          -- 源文件 + sheet
    target      TEXT    NOT NULL,          -- 本该进哪张表
    reason      TEXT    NOT NULL,          -- 为什么进不去
    gap_id      TEXT,
    row_data    JSONB   NOT NULL,          -- 原始行，一字不改地留着
    resolved    BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_quarantine_unresolved
    ON load_quarantine (source, loaded_at DESC) WHERE NOT resolved;

COMMENT ON TABLE load_quarantine IS
  '装载失败的行。**隔离区不为空就是一个要处理的事实**，不是日志噪音：'
  '里面每一行都代表一笔业务数据当前不被任何规则审核。'
  '审核报告的覆盖面声明里必须把它算进去——'
  '「本批 40 张单全部审核完毕」和「40 张单 + 8 行游离明细未审」是两句话。';

COMMENT ON COLUMN load_quarantine.row_data IS
  '原始行原样保留。不做任何清洗——清洗过的数据没法用来判断「当初到底是什么样」。';
