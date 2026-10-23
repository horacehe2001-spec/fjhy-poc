-- ============================================================================
-- 0005 · 规则资产（对应前端「规则资产管理」的 6 个二级菜单）
-- ----------------------------------------------------------------------------
-- 六张栏目按**规则加工链**排序，不是按字母或重要性：
--   制度文件库 → 规则抽取 → 规则本体 → 流程本体 → 发布管理
--                                      ↑
--                                 大模型配置（支撑项，服务前四步）
-- 这个顺序本身就是信息：上游没有的东西，下游不可能凭空长出来。
-- 规则本体里每一条规则都必须能回溯到制度文件库里的某一份文件、某一条条款。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 栏目一 · 制度文件库
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policy_doc (
    doc_id         TEXT PRIMARY KEY,
    file_name      TEXT        NOT NULL,                 -- 原始文件名，**溯源只认它**
    doc_number     TEXT,                                 -- 文号，如 闽港财智〔2025〕20号
    title          TEXT,
    issuer         TEXT,
    issued_on      DATE,
    effective_from DATE,
    effective_to   DATE,
    file_hash      TEXT        NOT NULL,                 -- sha256，同一文件重复上传只留一份
    file_size      BIGINT,
    page_count     INTEGER,
    stored_path    TEXT        NOT NULL,
    -- 解析状态：三条纪律之一「解析不了就说解析不了」，逐份标原因，不记成「正文为空」
    parse_status   TEXT        NOT NULL DEFAULT 'pending',  -- pending|native_text|ocr|failed
    parse_reason   TEXT,
    has_text_layer BOOLEAN,                              -- PDF 是否自带文字层
    ocr_engine     TEXT,                                 -- 如 tesseract 5.4.0.20240606 + chi_sim
    ocr_pages      INTEGER,
    uploaded_by    TEXT,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_doc_parse CHECK (parse_status IN ('pending','native_text','ocr','failed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_doc_hash ON policy_doc (file_hash);

COMMENT ON COLUMN policy_doc.file_name IS
  '**溯源只认文件名上的硬证据**（文号、整串书名号标题）。正文里引用过某个文号不算匹配。'
  '实测原因：OCR 把「闽港财智〔2025〕20号」识别成「赣港财知2025] 20号」，'
  '拿 OCR 正文去匹配文号必然错配。OCR 文本只用于条款检索与抽取草稿。';

COMMENT ON COLUMN policy_doc.parse_status IS
  '四态，缺一不可：pending 未处理；native_text 有文字层直接取；ocr 扫描件走 OCR；'
  'failed 两条路都不通。**failed 必须配 parse_reason，不许记成「正文为空」**——'
  '「解析失败」和「文件本来就是空的」是两件事，混了就再也查不出来。'
  '实测：6 份制度 PDF 里 2 份有文字层、4 份纯扫描（含最关键的 27 页那份）。';

CREATE TABLE IF NOT EXISTS policy_doc_page (
    id            BIGSERIAL PRIMARY KEY,
    doc_id        TEXT    NOT NULL REFERENCES policy_doc(doc_id) ON DELETE CASCADE,
    page_no       INTEGER NOT NULL,
    text_content  TEXT,
    text_source   TEXT    NOT NULL,                      -- native | ocr | human_read
    confidence    NUMERIC(5,4),
    image_path    TEXT,                                  -- 渲成 PNG 的路径，供人眼读图
    UNIQUE (doc_id, page_no, text_source)
);

COMMENT ON COLUMN policy_doc_page.text_source IS
  '三档，与 standard_limit.confidence 同一套口径。human_read 这一档是实测逼出来的：'
  '附件1 住宿费标准表 OCR 后数字都在（460/420/430/310/480/440），'
  '但城市↔职级↔金额的对应关系全乱了。改用 get_pixmap(dpi=170) 渲成 PNG 人眼读图，'
  '才拿到完整的 15 行费率。交通工具等级表则 OCR 与人眼读图**双双失败**（表格线断裂、'
  '文字重影），据此判定 TR-D4-01 不编译（G-03）。';

CREATE TABLE IF NOT EXISTS policy_clause (
    clause_id     TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL REFERENCES policy_doc(doc_id) ON DELETE CASCADE,
    clause_no     TEXT NOT NULL,                         -- 第二十条 / 2.1.5 / 附件2 第3项
    text_content  TEXT NOT NULL,
    page_no       INTEGER,
    confidence    TEXT NOT NULL,                         -- 人眼读图 | OCR | 原生文本
    parent_clause TEXT REFERENCES policy_clause(clause_id),
    CONSTRAINT ck_clause_conf CHECK (confidence IN ('人眼读图','OCR','原生文本'))
);

CREATE INDEX IF NOT EXISTS ix_clause_doc ON policy_clause (doc_id, clause_no);
-- 条款全文检索（抽取与人工查条款都要用）
CREATE INDEX IF NOT EXISTS ix_clause_text ON policy_clause USING gin (to_tsvector('simple', text_content));

-- ─────────────────────────────────────────────────────────────────────────────
-- 栏目二 · 规则抽取
-- ─────────────────────────────────────────────────────────────────────────────
-- 两档：自动抽取给草稿，人工确认才落规则本体。**没有「自动发布」这条路。**
CREATE TABLE IF NOT EXISTS extract_task (
    task_id       TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL REFERENCES policy_doc(doc_id) ON DELETE CASCADE,
    mode          TEXT NOT NULL,                         -- auto | assisted
    llm_config_id TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',       -- pending|running|done|failed
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    candidate_cnt INTEGER DEFAULT 0,
    accepted_cnt  INTEGER DEFAULT 0,
    error         TEXT,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_extract_mode   CHECK (mode IN ('auto','assisted')),
    CONSTRAINT ck_extract_status CHECK (status IN ('pending','running','done','failed'))
);

CREATE TABLE IF NOT EXISTS extract_candidate (
    id            BIGSERIAL PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES extract_task(task_id) ON DELETE CASCADE,
    clause_id     TEXT REFERENCES policy_clause(clause_id),
    draft         JSONB NOT NULL,                        -- 规则八要素的草稿
    confidence    NUMERIC(5,4),
    review_state  TEXT NOT NULL DEFAULT 'pending',       -- pending|accepted|rejected|merged
    reviewer      TEXT,
    reviewed_at   TIMESTAMPTZ,
    reject_reason TEXT,
    merged_into   TEXT,                                  -- 合并到哪条规则
    CONSTRAINT ck_cand_state CHECK (review_state IN ('pending','accepted','rejected','merged'))
);

COMMENT ON TABLE extract_candidate IS
  '抽取产物一律是**候选**，不是规则。accepted 之后才写 rule 表，且要记 reviewer。'
  '理由很直接：规则是制度的投影，判错一条就是对一批单据误报或漏报。'
  '模型抽出来的八要素里，「判定逻辑」和「例外」两项最容易丢细节——'
  '实测市内交通费那条，制度原文有 40 元这一档（仅往返中转站），漏掉就会把'
  '样本里 120 元的正常单判成超标准。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 栏目三 · 规则本体（八要素 + 对齐客户配置器）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rule (
    rule_id        TEXT    NOT NULL,                     -- TR-D4-05
    version        INTEGER NOT NULL,
    name           TEXT    NOT NULL,                     -- 八要素①规则名称
    dimension      TEXT    NOT NULL,                     -- ②审核维度 D1~D10
    risk_class     TEXT    NOT NULL,                     -- ③风险类别
    rule_class     TEXT    NOT NULL,                     -- 硬/混合/软
    source_clause  JSONB   NOT NULL,                     -- ④来源条款（文件名+条款号+原文摘录+可信度）
    also_source    JSONB,                                -- 多源印证
    scope          JSONB   NOT NULL,                     -- ⑤适用范围
    logic          JSONB   NOT NULL,                     -- ⑥判定逻辑
    exception      TEXT,                                 -- ⑦例外
    priority       INTEGER NOT NULL,                     -- ⑧优先级
    owner          TEXT,                                 -- 责任部门
    effective_from DATE,                                 -- 生效期
    effective_to   DATE,
    -- 对齐客户现有配置器的 4 个字段（规则编号口径由用户确认）
    algo_code      TEXT,                                 -- ITSHxxxx / AISHxxxx / YWSHxxxx
    algo_type      TEXT,                                 -- IT审核 | 业务审核 | AI审核
    control_type   TEXT    NOT NULL,                     -- 禁止 | 警告
    configurator_status TEXT,                            -- 有效 | 无效 | 建议新增 | 不适用
    -- 两个独立的可执行性标记，**不能合并**
    compiled       BOOLEAN NOT NULL DEFAULT FALSE,
    compiled_reason TEXT,
    demo_executable BOOLEAN NOT NULL DEFAULT FALSE,
    demo_blocker    TEXT,
    demo_scope      TEXT,
    binding_node   TEXT,                                 -- N01 提单闸 / N04 AI审核 / OUT（执行点在 OA）
    impl_path      TEXT,                                 -- 客户原文：系统规则 / AI智能审核 / 通过OA控制？/ ？
    feasibility    TEXT,                                 -- 客户自评：已有/可以实现/部分可实现/无法实现/未评估
    external_table TEXT,                                 -- 引用的决策表或名单
    note           TEXT,
    status         TEXT    NOT NULL DEFAULT 'draft',      -- draft|published|retired
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, version),
    CONSTRAINT ck_rule_class   CHECK (rule_class IN ('hard','mixed','soft')),
    CONSTRAINT ck_rule_control CHECK (control_type IN ('禁止','警告')),
    CONSTRAINT ck_rule_status  CHECK (status IN ('draft','published','retired')),
    -- 软规则不许硬拦：模型辅助判断必须人工复核
    CONSTRAINT ck_soft_not_block CHECK (NOT (rule_class = 'soft' AND control_type = '禁止')),
    -- 未编译的规则必须写明原因
    CONSTRAINT ck_compiled_reason CHECK (compiled OR compiled_reason IS NOT NULL),
    -- 未编译不可能演示可执行
    CONSTRAINT ck_demo_implies_compiled CHECK (NOT demo_executable OR compiled)
);

COMMENT ON CONSTRAINT ck_soft_not_block ON rule IS
  '软规则（模型辅助）不得标禁止。模型给的是提示，不是判决；'
  '让模型硬拦单据等于把审批权责交给它，与「AI 仅作辅助」的定位相反。';

COMMENT ON COLUMN rule.compiled IS
  '规则资产层面是否已编译成可执行形式（口径清楚、阈值可信、决策表齐备）。'
  '与 demo_executable **是两件事**：TR-D1-01 客户自标「是否已有=是」，'
  '说明真实共享系统里有这个闸（compiled=true）；但导出样本里没有「付款主体」列，'
  '演示跑不了（demo_executable=false）。合成一个字段，'
  '「客户系统做不到」与「我手上没数据」就分不清了——前者是产品缺口，后者只是取数没到位。';

CREATE INDEX IF NOT EXISTS ix_rule_dim    ON rule (dimension, version DESC);
CREATE INDEX IF NOT EXISTS ix_rule_status ON rule (status) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS ix_rule_algo   ON rule (algo_code) WHERE algo_code IS NOT NULL;

-- 规则引用的本体概念（多对多），启动时契约校验要靠它做连接
CREATE TABLE IF NOT EXISTS rule_concept (
    rule_id    TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    concept_id TEXT    NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'read',          -- read | compare | threshold
    PRIMARY KEY (rule_id, version, concept_id),
    FOREIGN KEY (rule_id, version) REFERENCES rule(rule_id, version) ON DELETE CASCADE
);

-- 规则引用的实体（存在性判断），与概念分开
CREATE TABLE IF NOT EXISTS rule_entity (
    rule_id    TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    entity_id  TEXT    NOT NULL,
    semantics  TEXT    NOT NULL,
    PRIMARY KEY (rule_id, version, entity_id),
    FOREIGN KEY (rule_id, version) REFERENCES rule(rule_id, version) ON DELETE CASCADE
);

COMMENT ON TABLE rule_entity IS
  '**实体存在性判断与概念引用必须分开两张表。** TR-D2-01「是否有出差审批单」'
  '问的是「这类附件在不在」，不是「某个字段的值是多少」。'
  '一度把 E03/E04 直接塞进 concepts，结果契约校验把实体 ID 当成未定义概念报错——'
  '被 规则资产/validate.py 抓到过一次，所以分表。';

-- 规则之间的覆盖/豁免关系
CREATE TABLE IF NOT EXISTS rule_relation (
    id          BIGSERIAL PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    rel_type    TEXT NOT NULL,                           -- overrides | priority_over | conflicts_with
    other_rule  TEXT NOT NULL,
    note        TEXT,
    needs_confirm BOOLEAN NOT NULL DEFAULT FALSE,
    gap_id      TEXT,
    FOREIGN KEY (rule_id, version) REFERENCES rule(rule_id, version) ON DELETE CASCADE,
    CONSTRAINT ck_rel_type CHECK (rel_type IN ('overrides','priority_over','conflicts_with'))
);

COMMENT ON COLUMN rule_relation.needs_confirm IS
  '口径冲突时**不替客户拍板**。实测两处：当天往返闽侯/连江贵安/长乐机场的补助，'
  '省财政厅解答写 40 元、本企业细则附件4 写 50 元（暂取 50，企业细则适用主体更近）；'
  '机票保险，场景表专用审核要点写「不允许报销」、制度第十条写「可以购买一份」'
  '——这条干脆不编译，按一方口径误判另一方比不判更糟。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 决策表 / 标准表 / 名单的通用承载
-- ─────────────────────────────────────────────────────────────────────────────
-- 为什么一张通用表而不是每种一张：必要附件决策表 29 行、敏感词表 15 词、
-- 内部单位名单 23 条、标准表若干行，它们的共性是「规则管理员维护的查找表，
-- 改表不改规则」。做成一张通用表，前端一个编辑器就能管全部，
-- 新增一类查找表不用加表也不用改前端。
CREATE TABLE IF NOT EXISTS lookup_table (
    table_id      TEXT PRIMARY KEY,                      -- 必要附件决策表 / 敏感词表 / 内部单位名单 / 住宿费标准表
    version       TEXT NOT NULL,
    kind          TEXT NOT NULL,                         -- decision_table | keyword_list | allow_list | limit_table
    source_file   TEXT,
    source_rows   INTEGER,                               -- 源表行数，用于对账
    row_count     INTEGER,                               -- 本表行数
    key_fields    JSONB,
    fallback_key  JSONB,
    on_miss       TEXT NOT NULL DEFAULT '待人工',          -- 键未命中时的行为
    note          TEXT,
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_lookup_kind CHECK (kind IN ('decision_table','keyword_list','allow_list','limit_table'))
);

COMMENT ON COLUMN lookup_table.on_miss IS
  '**键未命中时不得按「最严」或「最宽」兜底。** 按最严（要求全部附件类型）→'
  '新业务小类上线当天全部单据被拦；按最宽（不要求任何附件）→ 这条闸对新小类静默失效。'
  '正确做法是出一条「场景未配置，转人工」并写 improve_log，让规则管理员知道该补表了。';

CREATE TABLE IF NOT EXISTS lookup_row (
    id          BIGSERIAL PRIMARY KEY,
    table_id    TEXT NOT NULL REFERENCES lookup_table(table_id) ON DELETE CASCADE,
    row_key     JSONB NOT NULL,
    row_value   JSONB NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    confidence  TEXT,
    source_rows INTEGER,                                 -- 这一行在源表里对应几行，用于 row_count 合计对账
    must_hit    JSONB,                                   -- 正例（关键词表/名单用）
    must_not_hit JSONB,                                  -- 反例，**没有反例的条目不许启用**
    conflict    JSONB,
    gap_id      TEXT,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS ix_lookup_row_tbl ON lookup_row (table_id);
CREATE INDEX IF NOT EXISTS ix_lookup_row_key ON lookup_row USING gin (row_key);

COMMENT ON COLUMN lookup_row.must_not_hit IS
  '**反例是硬要求，不是可选项。** 误报和漏报同样致命：漏报放过风险，'
  '误报把正常交付物判成问题件。实测踩过 5 处：SPA 的拉丁词边界与「删空白」归一'
  '互相打架（XX SPA会馆 → XXSPA会馆，左邻变字母 X，真命中被挡掉）；'
  '「水疗」误伤「水疗设备制造有限公司」；「按摩」误伤「按摩椅采购」；'
  '「景点门票」匹配不到「景区门票」。没有反例这些一个都发现不了。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 栏目五 · 发布管理
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS release (
    release_id      TEXT PRIMARY KEY,
    rule_set_version TEXT NOT NULL UNIQUE,               -- cycle_log.rule_set_version 引用它
    ontology_version TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft',       -- draft|reviewing|published|rolled_back
    rule_count      INTEGER NOT NULL DEFAULT 0,
    compiled_count  INTEGER NOT NULL DEFAULT 0,
    executable_count INTEGER NOT NULL DEFAULT 0,
    diff_summary    JSONB,                               -- 与上一版的差异
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by     TEXT,
    reviewed_at     TIMESTAMPTZ,
    published_at    TIMESTAMPTZ,
    rolled_back_at  TIMESTAMPTZ,
    rollback_reason TEXT,
    CONSTRAINT ck_release_status CHECK (status IN ('draft','reviewing','published','rolled_back')),
    -- 发布必须有人签字，不许自动发布
    CONSTRAINT ck_release_signed CHECK (status <> 'published' OR reviewed_by IS NOT NULL)
);

COMMENT ON CONSTRAINT ck_release_signed ON release IS
  '**发布必须有人签字。** 规则集一变，所有在跑的单据的判定口径就跟着变。'
  '让抽取结果自动发布，等于让模型直接改生产口径。'
  'reviewed_by 为空的 release 不允许进入 published。';

CREATE TABLE IF NOT EXISTS release_item (
    id          BIGSERIAL PRIMARY KEY,
    release_id  TEXT    NOT NULL REFERENCES release(release_id) ON DELETE CASCADE,
    rule_id     TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    change      TEXT    NOT NULL,                        -- added | modified | retired | unchanged
    diff        JSONB,
    FOREIGN KEY (rule_id, version) REFERENCES rule(rule_id, version),
    CONSTRAINT ck_item_change CHECK (change IN ('added','modified','retired','unchanged'))
);

CREATE INDEX IF NOT EXISTS ix_release_item ON release_item (release_id, change);

-- 同一时刻只有一个已发布的当前规则集
CREATE TABLE IF NOT EXISTS release_current (
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE,
    release_id  TEXT NOT NULL REFERENCES release(release_id),
    switched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    switched_by TEXT,
    CONSTRAINT ck_singleton CHECK (singleton)
);

COMMENT ON TABLE release_current IS
  '单行表，强制「当前规则集只有一个」。用 CHECK(singleton) 把主键钉死成 TRUE，'
  '比用「取 published 里最新的一条」可靠——后者在回滚场景下会选错：'
  '回滚是把 current 指回旧版，而旧版的 published_at 更早。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 栏目六 · 大模型配置
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_config (
    config_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    provider       TEXT NOT NULL,                        -- dashscope | openai_compatible | local
    base_url       TEXT,
    model_name     TEXT NOT NULL,
    api_key_cipher TEXT,                                 -- **只存密文，接口只回掩码**
    api_key_mask   TEXT,                                 -- 如 sk-****3f2a，给前端显示
    temperature    NUMERIC(4,3) DEFAULT 0,
    max_tokens     INTEGER,
    enable_thinking BOOLEAN DEFAULT FALSE,
    concurrency    SMALLINT DEFAULT 3,
    timeout_s      SMALLINT DEFAULT 120,
    purpose        TEXT,                                 -- vision_read | rule_judge | extract
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_llm_no_plain CHECK (api_key_cipher IS NULL OR api_key_cipher NOT LIKE 'sk-%')
);

COMMENT ON COLUMN llm_config.api_key_cipher IS
  '**明文 Key 一律不出这张表**，读取接口永远只回 api_key_mask。'
  '大模型配置是脱敏旁路出口的典型：审核报告正文里不含 Key，'
  '但 /config 接口会把整条配置原样回出去——做安全的那个模块往往只守住主输出路径。'
  'ck_llm_no_plain 这条约束是最后一道防线：密文不可能以 sk- 开头，'
  '一旦有人把明文直接塞进来，库会拒绝。';

CREATE TABLE IF NOT EXISTS llm_call_log (
    id             BIGSERIAL PRIMARY KEY,
    called_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_id      TEXT REFERENCES llm_config(config_id),
    cycle_id       TEXT,
    purpose        TEXT,
    prompt_tokens  INTEGER,
    completion_tokens INTEGER,
    latency_ms     INTEGER,
    status         TEXT,
    error          TEXT,
    prompt_masked  BOOLEAN NOT NULL DEFAULT TRUE
);

COMMENT ON COLUMN llm_call_log.prompt_masked IS
  '**送进模型前就要脱敏，不能只脱回来的。** 请求体一旦发出就离开了本系统的控制范围。'
  '这一列为 FALSE 的行出现在生产，说明有一条调用路径绕过了 mask.py。';
