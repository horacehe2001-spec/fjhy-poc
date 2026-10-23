-- ============================================================================
-- 0037 · 附件读图判类型：分类台账 + 细类编码
-- ----------------------------------------------------------------------------
-- 0035 把 219 张内嵌附件抽进来了，但 attach_type 全部留空——
-- 从一张 PNG 判它是出差审批单还是住宿费发票必须读图。本迁移为那一步建台账。
--
-- 【用户决策 2026-10-21】走**多模态读图（出网计费）**，不走本机 OCR。
--
-- 【这一步比以往的读图都敏感，必须写在库注释里】
--   以前 doc_page_vision 发出去的是客户的**制度文件**页；
--   这次发出去的是**真实发票与出差申请单**，里面有姓名、身份证号片段、金额。
--   所以本表逐条记「哪一张附件、发给哪个型号、花了多少 token」——
--   事后必须答得出「哪些客户凭证离开过本系统」。
--
-- ─────────────────────────────────────────────────────────────────────────────
-- 【最要紧的一条：EN17 枚举与决策表的类型对不上，不是 1:1】
-- ─────────────────────────────────────────────────────────────────────────────
--   enum EN17（attachment.attach_type 的外键）12 个值，把三组合并了：
--       「出差审批单/差旅费明细表」   ← 决策表的 A01 + A02
--       「会议或培训通知/报名回执」   ← 决策表的 A06 + A07
--       「安全资金确认表/使用表」     ← 决策表的 A10 + A11
--   而 EN17 里的「情况说明」「预算调整审批材料」决策表里没有对应条目。
--
--   后果：模型答「出差审批单」**写不进 attach_type**（外键拒绝）。
--   所以分类同时产出两样：
--       type_code    细类编码 A01..A15，**给规则用**（决策表按它匹配）
--       attach_type  EN17 值，**给字段用**（过外键，也是页面上显示的那个）
--   两者由代码里的 TYPES 表一次性映射，**不许在别处再写第二份映射**。
--
--   斜杠的语义仍然未定（决策表头部的 G-25：「且」还是「或」没确认）。
--   本迁移不替它做决定——细类编码留着，等口径定了再决定怎么用。
-- ============================================================================

CREATE TABLE IF NOT EXISTS attachment_vision (
    id            BIGSERIAL   PRIMARY KEY,
    attachment_id BIGINT      NOT NULL REFERENCES attachment(id) ON DELETE CASCADE,
    attempt       INTEGER     NOT NULL,
    config_id     TEXT,
    model_name    TEXT,
    model_echo    TEXT,                       -- 厂商实际回的型号，和请求的未必一致
    ok            BOOLEAN     NOT NULL,
    err_kind      TEXT,
    message       TEXT,
    content       TEXT,                       -- 模型原样返回的内容，**不解析就不丢**
    type_code     TEXT,                       -- A01..A15；判不出为 NULL
    type_label    TEXT,                       -- 细类中文名
    en17_value    TEXT,                       -- 映射到的 EN17 值
    confidence    TEXT,                       -- 高 | 中 | 低，模型自报
    reason        TEXT,                       -- 模型给的判断依据（票面上看到了什么）
    latency_ms    INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_av_conf CHECK (confidence IS NULL OR confidence IN ('高','中','低')),
    UNIQUE (attachment_id, model_name, attempt)
);

CREATE INDEX IF NOT EXISTS ix_av_att ON attachment_vision (attachment_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_av_code ON attachment_vision (type_code);

COMMENT ON TABLE attachment_vision IS
  '附件读图判类型的台账。**每次调用一行，成败都落，永不覆盖**（同一张图重读 attempt+1）。'
  '这是本系统第二个会把客户数据发到外部的出口，而且比第一个（doc_page_vision 发制度文件页）'
  '敏感得多：**发出去的是真实发票与出差申请单，含姓名、身份证号片段、金额**。'
  '逐条留痕是为了事后答得出「哪些客户凭证离开过本系统、发给了谁」。';

COMMENT ON COLUMN attachment_vision.content IS
  '模型原样返回的内容。**解析失败也要把原文留下**——'
  '只存解析结果的话，解析器写错时就再也查不出模型当时到底答了什么，'
  '而重读一次是要再花一次钱的。';

COMMENT ON COLUMN attachment_vision.type_code IS
  'A01..A15 细类编码，**判不出就是 NULL，不猜**。'
  '与 EN17 不是 1:1：EN17 把 A01+A02、A06+A07、A10+A11 各合成了一项（见本迁移头部）。'
  '规则按细类编码匹配，字段按 en17_value 存。';

COMMENT ON COLUMN attachment_vision.confidence IS
  '模型自报的把握。**「低」一律不写进 attachment.attach_type**——'
  '一个不确定的类型比空值危险：空值会让 TR-D6-01 报「判不出」，'
  '而错的类型会让它报「缺件」或「齐全」，两种都是错的结论且看不出来。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 附件表：类型是怎么来的
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE attachment
    ADD COLUMN IF NOT EXISTS type_code    TEXT,
    ADD COLUMN IF NOT EXISTS type_source  TEXT,
    ADD COLUMN IF NOT EXISTS type_conf    TEXT,
    ADD COLUMN IF NOT EXISTS typed_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS typed_by     BIGINT REFERENCES attachment_vision(id)
                                          ON DELETE SET NULL;

DO $$ BEGIN
    ALTER TABLE attachment ADD CONSTRAINT ck_att_type_source
        CHECK (type_source IS NULL OR type_source IN ('读图判定', '人工指定', '演示合成'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 已有的 54 条演示附件：类型是生成器直接编的，标清楚来源。
UPDATE attachment SET type_source = '演示合成'
 WHERE source_kind = '演示合成' AND attach_type IS NOT NULL AND type_source IS NULL;

COMMENT ON COLUMN attachment.type_source IS
  '读图判定 | 人工指定 | 演示合成。**类型是怎么来的必须看得见。**'
  '读图判定的可信度低于人工指定——模型会把看不清的票面编出来，而且编得通顺'
  '（本项目在制度表格读图上已经实测过这件事）。'
  'TR-D6-01 真要拦单之前，读图判定的那些该不该先过一遍人工，是另一个决策。';

COMMENT ON COLUMN attachment.typed_by IS
  '定下这个类型的那次读图（attachment_vision.id）。'
  '**不是冗余**：台账里同一张图可能读过多次（失败重试、换模型对照），'
  '「页面上显示的这个类型是哪一次读出来的」必须指得出来。';
