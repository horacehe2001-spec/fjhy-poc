-- ============================================================================
-- 0038 · 附件字段抽取：发票/住宿/交通/出差审批单的文字字段台账
-- ----------------------------------------------------------------------------
-- 0037 解决了「这张图是什么类型」；本迁移解决「这张图上写了什么字段值」。
-- 两者故意分两次调用、两张台账：判类型用的提示词只问类型，字段抽取用的
-- 提示词只问字段，混在一次问里，模型会顾此失彼——而且类型判完就能用，
-- 不必等字段抽取也跑完才解锁 TR-D6-01。
--
-- 【用户决策 2026-10-22】这一轮只抽文字/日期/布尔类字段，
-- **金额、税额这类数字先不落明文**——发票抬头、开票单位、住宿晚数、
-- 往返与否、出差起止日期/地点可以读，价税合计/税额/税率这一次提示词
-- 里都不问，列也不建。要不要做金额类抽取、怎么落（脱敏/加密/留痕不落值），
-- 是另一次决策。
--
-- 和 attachment_vision 同样的纪律：
--   1. 判不出就是 NULL，不猜
--   2. 把握不到「高」不回写到 invoice / hotel_invoice / transport_voucher /
--      trip_application 这些业务表
--   3. 成败都落本表，永不覆盖（同一张图重读是新 attempt）
-- ============================================================================

CREATE TABLE IF NOT EXISTS attachment_extract (
    id            BIGSERIAL   PRIMARY KEY,
    attachment_id BIGINT      NOT NULL REFERENCES attachment(id) ON DELETE CASCADE,
    attempt       INTEGER     NOT NULL,
    config_id     TEXT,
    model_name    TEXT,
    model_echo    TEXT,
    ok            BOOLEAN     NOT NULL,
    err_kind      TEXT,
    message       TEXT,
    content       TEXT,                       -- 模型原样返回的内容，不解析就不丢
    type_code     TEXT        NOT NULL,        -- 抽取时用的是哪个细类的提示词（A01/A03/A04/A05）
    fields        JSONB,                       -- 解析出来的字段，键名见各 apply_* 函数
    confidence    TEXT,                        -- 高 | 中 | 低，模型自报，整次调用一个值
    applied       BOOLEAN     NOT NULL DEFAULT FALSE,  -- 是否回写进了业务表
    applied_to    TEXT,                        -- 回写进了哪张业务表 + 主键，便于溯源
    latency_ms    INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ae_conf CHECK (confidence IS NULL OR confidence IN ('高','中','低')),
    UNIQUE (attachment_id, model_name, attempt)
);

CREATE INDEX IF NOT EXISTS ix_ae_att ON attachment_extract (attachment_id, created_at DESC);

COMMENT ON TABLE attachment_extract IS
  '附件字段抽取的台账。**每次调用一行，成败都落，永不覆盖**。'
  '只问文字/日期/布尔字段，不问金额税额（2026-10-22 用户决策）——'
  '价税合计/税额/税率这一次提示词里都不出现，模型答了也不解析不落库。';

COMMENT ON COLUMN attachment_extract.fields IS
  '解析出来的字段，不含任何金额/税额类键。字段名随 type_code 变化：'
  'A01→start_date/end_date/place；A03→title/issuer/detail_text/vehicle/'
  'seat_class/from_place/to_place/ride_date/has_round_trip；'
  'A04→title/issuer/check_in/check_out/nights；A05→title/issuer/detail_text。';

COMMENT ON COLUMN attachment_extract.applied IS
  '把握是否到「高」且已回写进业务表。「中」「低」留在本表供人看，不采信。';

-- ─────────────────────────────────────────────────────────────────────────────
-- trip_application：加一个「这行是读图读出来的，不是 OA 同步」的来源标记
-- ─────────────────────────────────────────────────────────────────────────────
-- synced_from_oa 专指「OA 系统同步」这一条真实集成链路，不能把「读图抽取」
-- 也标成 True——那会让审计查不清这行数据到底是哪条链路给的。
-- 两者在 _travel_day_count()（app/rules/impl.py）里的可信度不同：
-- 只有 synced_from_oa 才会被当「单次连续出差」的依据，读图抽取的暂时不会
-- （要不要让读图抽取的 start_date/end_date 也支持单次判定，是另一次决策，
--  本迁移只建列、不改规则实现）。
ALTER TABLE trip_application
    ADD COLUMN IF NOT EXISTS extracted_from TEXT;

DO $$ BEGIN
    ALTER TABLE trip_application ADD CONSTRAINT ck_ta_extracted_from
        CHECK (extracted_from IS NULL OR extracted_from IN ('审批单读图'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN trip_application.extracted_from IS
  '审批单读图 = 本行由 attachment_extract 读出差审批单图片写入，'
  '不是 OA 同步。NULL 且 synced_from_oa=TRUE 才是真正的 OA 同步行。';

-- ─────────────────────────────────────────────────────────────────────────────
-- hotel_invoice / transport_voucher：补上 invoice 早就有的「来源看得见」两列
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE hotel_invoice
    ADD COLUMN IF NOT EXISTS ocr_source TEXT,
    ADD COLUMN IF NOT EXISTS ocr_confidence TEXT;

ALTER TABLE transport_voucher
    ADD COLUMN IF NOT EXISTS ocr_source TEXT,
    ADD COLUMN IF NOT EXISTS ocr_confidence TEXT;

COMMENT ON COLUMN hotel_invoice.ocr_source IS
  '这行数据哪来的，如 vision:qwen3-vl-plus:attachment=2144。NULL 多半是演示合成。';
COMMENT ON COLUMN transport_voucher.ocr_source IS
  '同 hotel_invoice.ocr_source。';
