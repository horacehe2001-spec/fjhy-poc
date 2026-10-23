-- ============================================================================
-- 0032 · 领域与实例：给「领域知识资产」加一层归属
-- ----------------------------------------------------------------------------
-- 用户决策（2026-10-21）：
--   ① 本期做「归属 + 展示」，**不做按实例过滤**。现有全部资产归到
--      「审核 / 福建海运集团」，各栏目页头显示当前实例，接口暂不加
--      WHERE instance_id。只有一个实例时，过滤与否结果相同；
--      等真有第二个实例，再逐条接口加过滤（那时才需要这张表上的外键）。
--   ② 当前选择是**全局单例**：库里一行，所有人共用。本 POC 没有登录体系，
--      存在浏览器里会出现「你看到的和我看到的不同」，排障时多一个变量。
--
-- 【为什么列名叫 label 而不是 name】
--   脱敏模块按**字段名**选策略，`name` 一律当人名打星号。
--   本项目已经为此踩过四次（规则名、本体名、服务名、大模型配置名全被打成
--   星号）。对外要展示的名称字段一律不叫 name。
--
-- 【为什么「报表」领域下可以没有实例】
--   它是占位：证明这套结构支持多领域，而不是假装报表领域已经有资产。
--   当前实例允许为空（当前领域切到报表时），页面显式显示「该领域下还没有实例」，
--   **不假装有**。
-- ============================================================================

CREATE TABLE IF NOT EXISTS domain (
    domain_id   TEXT        PRIMARY KEY,
    label       TEXT        NOT NULL,             -- 展示名，不叫 name（见上）
    sort_no     INTEGER     NOT NULL DEFAULT 100,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  domain IS '业务领域。审核 / 报表 …… 领域之下才是实例。';
COMMENT ON COLUMN domain.label IS '展示名。**不要改叫 name**：脱敏按字段名选策略，name 会被打成星号。';

CREATE TABLE IF NOT EXISTS domain_instance (
    instance_id TEXT        PRIMARY KEY,
    domain_id   TEXT        NOT NULL REFERENCES domain(domain_id) ON DELETE RESTRICT,
    label       TEXT        NOT NULL,
    is_default  BOOLEAN     NOT NULL DEFAULT FALSE,   -- 该领域下的默认实例
    sort_no     INTEGER     NOT NULL DEFAULT 100,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE domain_instance IS
    '领域下的具体实例（一个客户/一个主体/一套资产）。本期只有「福建海运集团」一个。';

-- 一个领域最多一个默认实例。没有这条约束，两条 is_default 会让
-- 「默认是哪个」取决于查询顺序——而那种错不会报错，只会偶尔选错。
CREATE UNIQUE INDEX IF NOT EXISTS ux_instance_default_per_domain
    ON domain_instance (domain_id) WHERE is_default;

CREATE INDEX IF NOT EXISTS ix_instance_domain ON domain_instance (domain_id, sort_no);

-- ── 全局设置（键值）──────────────────────────────────────────────────────
-- 当前领域、当前实例存在这里。做成通用键值表而不是「只有一行的 current 表」：
-- 以后还会有别的全局开关，一张表装得下；而每加一个开关建一张单行表，
-- 半年后会堆出一串谁也说不清用途的表。
CREATE TABLE IF NOT EXISTS app_setting (
    setting_key TEXT        PRIMARY KEY,
    value       TEXT,                                 -- 允许 NULL：当前实例可以没有
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT                                  -- 谁改的。切换是会影响所有人的动作
);

COMMENT ON TABLE app_setting IS
    '全局设置（键值）。current_domain / current_instance 存在这里，所有人共用一份。';

-- ── 种子 ────────────────────────────────────────────────────────────────
-- ON CONFLICT DO NOTHING：迁移可重跑，且**不覆盖人改过的值**。
-- 用 DO UPDATE 会把用户改过的展示名在下次迁移时悄悄改回去。
INSERT INTO domain (domain_id, label, sort_no, note) VALUES
    ('audit',  '审核', 10, '财务单据合规审核。本 POC 的全部资产都在这个领域下。'),
    ('report', '报表', 20, '占位领域：结构上支持，目前没有实例也没有资产。')
ON CONFLICT (domain_id) DO NOTHING;

INSERT INTO domain_instance (instance_id, domain_id, label, is_default, sort_no, note) VALUES
    ('fjhy', 'audit', '福建海运集团', TRUE, 10,
     '差旅费 AI 审核 POC。现有制度文件、规则、本体、发布版本全部归属于此。')
ON CONFLICT (instance_id) DO NOTHING;

INSERT INTO app_setting (setting_key, value, updated_by) VALUES
    ('current_domain',   'audit', 'migration:0032'),
    ('current_instance', 'fjhy',  'migration:0032')
ON CONFLICT (setting_key) DO NOTHING;
