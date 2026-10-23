-- ============================================================================
-- 0003 · 本体（业务本体 + 规则本体 + 流程本体）
-- ----------------------------------------------------------------------------
-- 这几张表不是手工维护的，是从 `本体_v0.1.yaml` 装载进来的
-- （app/ontology/loader.py）。YAML 是源，库里是投影。
--
-- 【为什么本体要进库，不是只留在 YAML 里】
--   1) 业务表的枚举列要外键指向 ontology_enum_value。枚举改一个取值就成了
--      插一行数据，而不是改一次 schema——差旅费的「费用小类」有 13 个取值，
--      客户随时会加，不能每加一个就走一次数据库迁移。
--   2) 启动时契约校验要在库里做：规则引用的概念是否存在、枚举取值是否一致。
--      只有 YAML 的话，校验只能在进程内做，而写库的那条路（比如导入历史数据）
--      绕不过去就会塞进非法值。
--   3) 前端「规则本体」「流程本体」两个栏目要查询和画图。
--
-- 【双本体：业务本体 + 规则本体，通过概念 ID 绑定，独立演进】
--   业务本体答「审什么」（报账单有哪些字段、取值范围是什么）；
--   规则本体答「依据什么、怎么判」（八要素）。
--   两者**不能合并**：制度换一版，规则本体整体换版，而业务本体基本不动；
--   OA 多同步一个字段，业务本体加一列，而规则本体不受影响。
--   合在一起会让每次制度修订都牵动业务表结构。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 业务本体
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ontology_version (
    version      TEXT        PRIMARY KEY,          -- 如 0.1
    source_file  TEXT        NOT NULL,             -- 本体_v0.1.yaml
    source_hash  TEXT        NOT NULL,             -- 文件内容 sha256，用于判「库里的本体是不是当前 YAML」
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_current   BOOLEAN     NOT NULL DEFAULT FALSE,
    stats        JSONB                             -- {entities:17, concepts:119, enums:20, relations:18}
);

-- 同一时刻只能有一个当前版本
CREATE UNIQUE INDEX IF NOT EXISTS uq_ontology_current
    ON ontology_version (is_current) WHERE is_current;

CREATE TABLE IF NOT EXISTS ontology_entity (
    entity_id    TEXT        NOT NULL,             -- E01 ~ E17
    version      TEXT        NOT NULL REFERENCES ontology_version(version) ON DELETE CASCADE,
    name         TEXT        NOT NULL,             -- 报账单 / 费用明细 / ...
    source       TEXT,                             -- 该实体的证据来源：单据样本 / 审核要点 / 制度 / 场景表 ...
    note         TEXT,
    PRIMARY KEY (version, entity_id)
);

CREATE TABLE IF NOT EXISTS ontology_concept (
    concept_id   TEXT        NOT NULL,             -- C0101 ~ C1704
    version      TEXT        NOT NULL,
    entity_id    TEXT        NOT NULL,
    name         TEXT        NOT NULL,
    data_type    TEXT        NOT NULL,             -- string|integer|decimal(18,2)|date|timestamp|boolean|enum|ref|rate|daterange
    enum_id      TEXT,                             -- data_type=enum 时指向 ontology_enum
    ref_entity   TEXT,                             -- data_type=ref 时指向另一个实体
    source       TEXT,
    availability TEXT        NOT NULL,             -- 实测可得性，见 规则绑定_v0.1.json 的 availability_classes
    gap_id       TEXT,                             -- 取不到时对应的缺口编号，如 G-04 / G-29
    sensitive    BOOLEAN     NOT NULL DEFAULT FALSE,
    ocr_sourced  BOOLEAN     NOT NULL DEFAULT FALSE,
    note         TEXT,
    PRIMARY KEY (version, concept_id),
    FOREIGN KEY (version, entity_id) REFERENCES ontology_entity(version, entity_id) ON DELETE CASCADE
);

COMMENT ON COLUMN ontology_concept.availability IS
  '**这一列是整套系统最有信息量的一列。** 它记的是「这个字段在手上的数据里到底取不取得到」，'
  '实测得来，不是设计出来的：样本可得 / 仅单人可得 / 样本全空 / 样本缺列 / 需OA同步 / '
  '需票据OCR / 待业务侧明确 / 已录入资产 / 需外部数据源 / 可推导。'
  '启动时契约校验据此把引用了取不到字段的规则剔出执行集——'
  '让 AI 声称复核了它读不到的数据，比少复核更危险。';

COMMENT ON COLUMN ontology_concept.ocr_sourced IS
  'TRUE 表示该字段的值来自 OCR。OCR 来源的数值**不得直接作为硬规则的阈值**：'
  '实测 OCR 把「闽港财智〔2025〕20号」识别成「赣港财知2025] 20号」，'
  '阈值错一位，硬规则就从保护变成伤害。';

CREATE TABLE IF NOT EXISTS ontology_enum (
    enum_id   TEXT NOT NULL,                       -- EN01 ~ EN20
    version   TEXT NOT NULL REFERENCES ontology_version(version) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    source    TEXT,
    note      TEXT,
    PRIMARY KEY (version, enum_id)
);

CREATE TABLE IF NOT EXISTS ontology_enum_value (
    version   TEXT     NOT NULL,
    enum_id   TEXT     NOT NULL,
    value     TEXT     NOT NULL,
    ord       SMALLINT NOT NULL,
    PRIMARY KEY (version, enum_id, value),
    FOREIGN KEY (version, enum_id) REFERENCES ontology_enum(version, enum_id) ON DELETE CASCADE
);

COMMENT ON TABLE ontology_enum_value IS
  '**枚举取值一个都不能多。** 多一个表单上不存在的取值，规则就会去判一个永远为假的条件，'
  '这条规则就等于悄悄失效了。实测踩过：出差审批单的「用餐情况」我一度写了 4 项'
  '（多了一个「举办方统一提供」），读到附件3 原件后才发现表单上只有 3 项。'
  '所以这张表的内容只能来自单据/表单原件，不能来自推断。';

-- 业务表的枚举列外键指向这张视图式的「当前版本取值」。
-- 用视图而不是直接外键到 ontology_enum_value，是因为外键不能带 WHERE 条件；
-- 下面用一张冗余的 current 表 + 触发器同步来承载外键。
CREATE TABLE IF NOT EXISTS enum_value_current (
    enum_id TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (enum_id, value)
);

COMMENT ON TABLE enum_value_current IS
  '当前版本的枚举取值，供业务表外键引用。由 loader 在切换当前本体版本时整表重建。'
  '**为什么要这张冗余表**：PostgreSQL 的外键不支持「只引用满足某条件的行」，'
  '而业务表只应该受当前版本约束，不该被历史版本的取值放行。';

CREATE TABLE IF NOT EXISTS ontology_relation (
    id          BIGSERIAL PRIMARY KEY,
    version     TEXT NOT NULL REFERENCES ontology_version(version) ON DELETE CASCADE,
    from_entity TEXT NOT NULL,
    to_entity   TEXT NOT NULL,
    rel_type    TEXT NOT NULL,                     -- 包含 / 流转 / 归属 / 占用 / 分摊 / 需附 / 凭据 ...
    cardinality TEXT,                              -- 1..1 / 1..n / 0..n / 0..1
    source      TEXT,
    note        TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 流程本体
-- ─────────────────────────────────────────────────────────────────────────────
-- 节点不是拟的，是从 50 页真实审批记录里数出来的：7 个节点 + 2 个旁路 + 12 条迁移。
CREATE TABLE IF NOT EXISTS process_node (
    node_id      TEXT NOT NULL,                    -- N01 ~ N06 / N99
    version      TEXT NOT NULL REFERENCES ontology_version(version) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    is_bypass    BOOLEAN NOT NULL DEFAULT FALSE,
    occurrences  INTEGER,                          -- 在 50 页样本里出现次数，实测值
    note         TEXT,
    PRIMARY KEY (version, node_id)
);

CREATE TABLE IF NOT EXISTS process_transition (
    id          BIGSERIAL PRIMARY KEY,
    version     TEXT NOT NULL REFERENCES ontology_version(version) ON DELETE CASCADE,
    from_node   TEXT NOT NULL,
    to_node     TEXT NOT NULL,
    cond        TEXT,
    blocking    BOOLEAN NOT NULL DEFAULT TRUE,     -- 这条迁移是否构成阻断
    note        TEXT
);

COMMENT ON COLUMN process_transition.blocking IS
  '**N04 AI审核 → N05 共享审核 这条迁移的 blocking 必须是 FALSE。** 实测：'
  'FJPG-BX2610130821 在 2026-10-13 17:25:03 得到「AI审核不通过」，'
  '17:35:41 仍进入共享审核并由人工给出退回理由。'
  'AI 结论是参考，闸门在人工手里。把它建模成阻断，等于把一个本来由人掌握的'
  '权责交给模型——这与「AI 仅作辅助、不替代人工审批权责」的定位相反。';
