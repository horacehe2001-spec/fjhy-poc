-- ============================================================================
-- 0020 · 规则抽取：切分策略 + 多源印证与冲突
-- ----------------------------------------------------------------------------
-- 两件事：
--
-- 一、补 doclib_clause 的类别约束。0019 给 doc_lib_file 加了「单据」一档，
--     这张表的 CHECK 没跟着改，于是切到报账单时直接 CheckViolation 500。
--     **同一个口径分散在两张表的约束里，改一处漏一处是迟早的事**——
--     所以这里同时把两张表的取值写成同一套，并在注释里互相指认。
--
-- 二、L2 协同 Agent 要用的多源台账。
--     同一条规则在客户资料里**确实有多个来源**：
--       差旅费审核规则.xlsx 第5行「交通费标准控制：按照各单位制度要求校验
--       报销人交通坐席等级」
--       vs 差旅管理实施细则里关于交通工具等级的具体条款
--     两者口径不一定一致。**不自动选边**（2026-10-18 决策）：
--     自动选边的风险很具体——选错了就是一条「看起来有制度依据」的错规则，
--     而审定的人未必回查两个原文。所以两个来源都留着，标「口径不一致」交人判。
-- ============================================================================

-- ── 一、类别口径对齐 ────────────────────────────────────────────────────────
ALTER TABLE doclib_clause DROP CONSTRAINT IF EXISTS ck_doclib_clause_kind;
ALTER TABLE doclib_clause ADD CONSTRAINT ck_doclib_clause_kind
    CHECK (doc_kind IN ('制度', '范本', '单据', '其他'));

COMMENT ON COLUMN doclib_clause.doc_kind IS
  '与 doc_lib_file.doc_kind 同一套取值（制度/范本/单据/其他），由 doclib.classify 判。'
  '**单据不参与切条款**：报账单明细表里是业务数据（单据编号、金额），'
  '切进规则资产表等于把业务数据复制一份进来，比噪声更糟。'
  '范本参与但不计入候选：文书模板里的「第X条」是文书条款，不是制度条款。';

-- 切分策略：条款体 / 层级编号 / 表格行
ALTER TABLE doclib_clause
    ADD COLUMN IF NOT EXISTS split_strategy TEXT;

COMMENT ON COLUMN doclib_clause.split_strategy IS
  '这条是用哪种策略切出来的：条款体（第X条）/ 层级编号（1.3.9.1）/ 表格行（xlsx 每行一条）。'
  '**要记下来**：同一份文件换了策略，切出来的条款号口径就变了'
  '（「第十条」变成「第5行」），下游拿旧条款号去溯源会对不上，'
  '而没有这一列就查不出是策略换了还是文件改了。';

-- ── 二、多源印证与冲突台账 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doclib_rule_source (
    id          BIGSERIAL   PRIMARY KEY,
    group_key   TEXT        NOT NULL,          -- 同一条规则的归并键
    clause_id   TEXT        NOT NULL REFERENCES doclib_clause(clause_id) ON DELETE CASCADE,
    file_id     TEXT        NOT NULL,
    source_kind TEXT        NOT NULL,          -- 制度条款 | 规则表 | 配置器算法
    excerpt     TEXT        NOT NULL,
    confidence  TEXT        NOT NULL,          -- 原生文本 | OCR
    noted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_key, clause_id),
    CONSTRAINT ck_dls_kind CHECK (source_kind IN ('制度条款', '规则表', '配置器算法')),
    CONSTRAINT ck_dls_conf CHECK (confidence IN ('原生文本', 'OCR'))
);

CREATE INDEX IF NOT EXISTS ix_dls_group ON doclib_rule_source (group_key);

COMMENT ON TABLE doclib_rule_source IS
  '一条规则的多个来源。group_key 相同的行是「同一条规则的不同出处」。'
  '**多源不是冗余，是证据强度**：制度条款 + 规则表都写了同一件事，'
  '这条规则比只有单一出处的可信得多；只有规则表写了而制度里没有，'
  '说明它可能是业务惯例而非制度要求——这两种情况在清单上必须分得开。';

CREATE TABLE IF NOT EXISTS doclib_conflict (
    conflict_id TEXT        PRIMARY KEY,
    group_key   TEXT        NOT NULL,
    kind        TEXT        NOT NULL,          -- 阈值不一致 | 控制类型不一致 | 适用范围不一致
    detail      TEXT        NOT NULL,
    left_clause  TEXT       NOT NULL,
    right_clause TEXT       NOT NULL,
    state       TEXT        NOT NULL DEFAULT '待裁决',
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    resolution  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dlc_state CHECK (state IN ('待裁决', '已裁决', '已忽略'))
);

CREATE INDEX IF NOT EXISTS ix_dlconf_group ON doclib_conflict (group_key);

COMMENT ON TABLE doclib_conflict IS
  '多源口径不一致。**系统只负责发现并标出来，不自动选边**（2026-10-18 决策）。'
  '自动选边的风险很具体：选错了就是一条「看起来有制度依据」的错规则，'
  '而审定的人未必回查两个原文。裁决必须记 resolved_by——'
  '「谁拍的板」在制度冲突这种事情上，和结论本身一样重要。';

COMMENT ON COLUMN doclib_conflict.state IS
  '待裁决 | 已裁决 | 已忽略。**已忽略也要留行**：下次抽取还会发现同一个冲突，'
  '没有「已忽略」这一档就会反复提示同一件事，提示多了就没人看了。';
