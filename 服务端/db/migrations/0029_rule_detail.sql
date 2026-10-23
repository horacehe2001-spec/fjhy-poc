-- ============================================================================
-- 0029 · 规则明细：这一栏的产物终于是「规则」，不是「条款」
-- ----------------------------------------------------------------------------
-- 为什么要这张表（实测出来的三件事）：
--
-- ① **抽出来的是条款，不是规则。** 库里 185 条 doclib_clause 是文本切片，
--    而「规则抽取」这一栏该交付的是**一行一条规则、要素成列**的明细。
--    实测这条链对规则库的净贡献是 0 条：82 条规则全是 created_by='asset_loader'，
--    onto_adoption 一条都没有。
--
-- ② **三份 xlsx 本来就是规则明细，被降维成一行文本，又花钱让模型升维回来。**
--    《差旅费审核规则.xlsx》表头是
--      序号 | 校验类型 | 校验规则 | 实现途径 | 校验方式 | 是否已有 | 评估共享是否可实现
--    那条唯一成功的付费草稿（58 秒）给出的「规则名称=交通费标准控制」
--    和「处置动作=禁止」，**原本就躺在第 2 列和第 5 列**；
--    而模型给不出的「实现途径=系统规则」「是否已有=是」反倒被丢掉了。
--    按列映射：确定性、0 元、0 幻觉，还多拿 4 个字段。
--
-- ③ **rule 表的列早就是按这些列设计的**：algo_code / algo_type / control_type /
--    configurator_status / impl_path / feasibility。缺的只是中间这一张明细台账。
--
-- 三条口径：
--
--   **列映射和 AI 草稿进同一张表，用 origin 分开。**
--   页面上必须是**一张**产物清单：两张清单会让人问「哪张才是抽出来的规则」。
--   但每一行必须说清自己是哪一路来的——确定性列映射和模型草稿的可信度不是一档。
--
--   **列数不齐、闭集合对不上的行，不映射，但要留着。**
--   实扫《系统审核算法.xlsx》表头 15 列、数据行 14 列（「审核算法规则」列是空的），
--   按位置硬映射的话「控制类型」会读到「自定义」，「状态」会读到更新时间。
--   场景表 1714 行里列数分布是 7列423 / 8列652 / 9列624 / 10列2，
--   还有一行把审核要点文本串到了「系统单据」列上。
--   **静默丢掉是漏报，硬映射是误报，两者同样致命**——所以标出来交人看。
--
--   **采纳只是标记，不写 rule 表。** 和 doclib_draft 同一条纪律：
--   写进本体要填维度、优先级、绑定节点这些明细里没有的东西，
--   那是「规则本体」那一栏的事。这里自动写进去，等于让没人填过的字段
--   带着默认值进了规则清单。
-- ============================================================================

CREATE TABLE IF NOT EXISTS doclib_rule_detail (
    detail_id   TEXT        PRIMARY KEY,
    clause_id   TEXT        NOT NULL REFERENCES doclib_clause(clause_id)
                                ON DELETE CASCADE ON UPDATE CASCADE,
    file_id     TEXT        NOT NULL REFERENCES doc_lib_file(file_id) ON DELETE CASCADE,
    origin      TEXT        NOT NULL,          -- 列映射 | AI草稿
    source_kind TEXT        NOT NULL,          -- 规则表 | 配置器算法 | 场景表 | 制度条款

    -- 规则要素。**列名对齐 rule 表**，采纳时不用再翻译一遍。
    -- 注意 rule_name 不叫 name：叫 name 会被按字段名选策略的脱敏当成人名打星号
    -- （这个坑在本项目里踩过三次：规则名、本体名、服务名）。
    rule_name   TEXT        NOT NULL,
    dimension   TEXT,                           -- D1..D10，映射给不出就留空
    rule_class  TEXT,                           -- hard | mixed | soft
    control_type TEXT,                          -- 禁止 | 警告
    scope_text  TEXT,
    logic_text  TEXT,
    exception_text TEXT,
    risk_text   TEXT,
    impl_path   TEXT,                           -- 系统规则 | 配置器 | 人工
    feasibility TEXT,                           -- 是否已有
    algo_code   TEXT,                           -- ITSH0006
    algo_type   TEXT,
    configurator_status TEXT,                   -- 有效 | 无效 | 建议新增
    numbers     TEXT[]      NOT NULL DEFAULT '{}',
    note        TEXT,

    -- 映射留痕
    col_map     JSONB,                          -- 表头名 → 原值，逐列留痕
    mapped_ok   BOOLEAN     NOT NULL DEFAULT TRUE,
    map_warning TEXT,
    confidence  TEXT        NOT NULL,           -- 原生文本 | OCR
    model       TEXT,                           -- origin='AI草稿' 才有
    draft_id    TEXT,

    review_state  TEXT      NOT NULL DEFAULT '待审',
    reviewer      TEXT,
    reviewed_at   TIMESTAMPTZ,
    reject_reason TEXT,
    adopted_rule_id TEXT,                       -- 本体那一栏采纳后回填
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_drd_origin CHECK (origin IN ('列映射', 'AI草稿')),
    CONSTRAINT ck_drd_state  CHECK (review_state IN ('待审', '采纳', '驳回')),
    CONSTRAINT ck_drd_ctrl   CHECK (control_type IS NULL OR control_type IN ('禁止', '警告')),
    CONSTRAINT ck_drd_class  CHECK (rule_class IS NULL OR rule_class IN ('hard', 'mixed', 'soft')),
    -- 没映射成功的必须说清为什么。**「判不出」和「忘了写」在库里必须长得不一样。**
    CONSTRAINT ck_drd_warn   CHECK (mapped_ok OR map_warning IS NOT NULL),
    -- 驳回必须写原因：不写原因的驳回，下次抽取还会抽出同一条
    CONSTRAINT ck_drd_reject CHECK (review_state <> '驳回' OR reject_reason IS NOT NULL),
    -- 模型来的那一路必须留下型号：「这条是哪个模型出的」是评估质量的前提
    CONSTRAINT ck_drd_model  CHECK (origin <> 'AI草稿' OR model IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_drd_file   ON doclib_rule_detail (file_id);
CREATE INDEX IF NOT EXISTS ix_drd_clause ON doclib_rule_detail (clause_id);
CREATE INDEX IF NOT EXISTS ix_drd_state  ON doclib_rule_detail (review_state, origin);

COMMENT ON TABLE doclib_rule_detail IS
  '规则抽取的**产物台账**：一行一条规则明细，要素成列。'
  '两路来源进同一张表（origin），因为页面上必须是一张清单——'
  '两张清单会让人问「哪张才是抽出来的规则」。但可信度不是一档：'
  '列映射是确定性的，AI 草稿要逐条核数值。'
  '**采纳只是标记，不写 rule 表**：写进本体要填维度、优先级、绑定节点，'
  '那是「规则本体」那一栏的事。';

COMMENT ON COLUMN doclib_rule_detail.origin IS
  '列映射 = 从表格行按**表头名**映射出来的，确定性、不出网、不计费。'
  'AI草稿 = 大模型从条款原文抽的八要素，要过数值幻觉核对。'
  '实测对照：《差旅费审核规则.xlsx》第 5 行，列映射一次拿到 '
  '规则名称/判定逻辑/控制类型/实现途径/是否已有 五项；'
  '模型花 58 秒把前三项「猜」了一遍（值对），后两项丢了，风险等级和例外给的是 null。';

COMMENT ON COLUMN doclib_rule_detail.mapped_ok IS
  'FALSE = 这一行**没有按列映射**，只留了原文等人看。'
  '两种情形：列数与表头不一致（实测《系统审核算法.xlsx》表头 15 列、数据行 14 列，'
  '「审核算法规则」是空列，按位置硬映射会让「控制类型」读到「自定义」）；'
  '或者闭集合列对不上（控制类型不是禁止/警告、状态不是有效/无效）。'
  '**静默丢掉是漏报，硬映射是误报，两者同样致命。**';

COMMENT ON COLUMN doclib_rule_detail.col_map IS
  '逐列留痕：表头名 → 这一行的原值。映射对不对，不用回去翻 xlsx 就能核。'
  '**这是「有据可查」的那个据**——没有它，一条明细就只是一句话。';

COMMENT ON COLUMN doclib_rule_detail.configurator_status IS
  '配置器里的状态。实测《系统审核算法.xlsx》6 条算法里有 3 条是「无效」——'
  '把无效算法当成待实现规则抽出来是错的，所以这一列必须跟着明细走。';

COMMENT ON COLUMN doclib_rule_detail.adopted_rule_id IS
  '本体那一栏采纳成规则之后回填的规则号。'
  '**这里不写 rule 表**，这一列是给「这条明细最后变成了哪条规则」留的链子。';

-- 0028 建的那个唯一索引里带了 clause_id，而 clause_id 就是主键，
-- 所以它除了当个查询索引没别的作用。**说清楚比留着让人猜好**：
-- 换成按 (file_id, 策略, 条款号) 的普通索引，那才是实际的查法。
-- 不能做成唯一：实扫有 3 处同份文件条款号重复（靠 id 里的「同号第几次」区分）。
DROP INDEX IF EXISTS ux_doclib_clause_ident;
CREATE INDEX IF NOT EXISTS ix_doclib_clause_ident
    ON doclib_clause (file_id, split_strategy, clause_no);
