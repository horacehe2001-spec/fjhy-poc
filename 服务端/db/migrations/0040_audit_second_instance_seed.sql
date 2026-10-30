-- ============================================================================
-- 0040 · 审核领域第二实例「822港航」：种子 + 低风险表的 instance_id 归属
-- ----------------------------------------------------------------------------
-- 用户决策（2026-10-29）：审核领域下每个实例的领域知识资产完全隔离，不是只做
-- 归属展示（这与 0032/0034 的「本期只归属不过滤」口径不同——那时只有一个实例，
-- 过滤与否结果相同；现在真的有第二个实例了）。新实例起始状态完全空白，不克隆
-- 福建海运集团现有资产。
--
-- 【这个文件只做"加列不改主键"的部分】
--   org / expense_bill / standard_limit / rank_tier_map / org_special_rule：
--     这几张是业务链的"根"，但业务主键（org_id / bill_no）按用户决策维持全局
--     唯一，不改主键，只加列按实例过滤即可。
--   policy_doc 及其子表（policy_doc_page/policy_clause/extract_task/
--     extract_candidate）：**全仓库扫描确认过，没有任何 Python 代码在写这几张
--     表**（0015/0034 的注释已经说明，制度文件库的真正落地表是 doc_lib_file，
--     这几张是被 doc_lib_* 取代后留下的旧表）。没有写路径就没有主键碰撞风险，
--     只加列备查，不改主键。
--   onto_adoption / onto_cand_action：主键（adoption_id/action_id）是程序生成的
--     哈希/随机 id，碰撞概率可忽略，只加列不改主键。
--
-- 【改主键的部分在哪】
--   rule / release / lookup_table 及其子表的主键改造（因为这几张的业务主键是
--   人工/装载器赋的编号，如 rule_id="TR-D4-05"——两个公司各自的规则抽取流程
--   很可能生成同一个编号，不改主键会在第二个实例写入时主键冲突）放在下一个
--   文件 0041，单独验证，不和这个低风险文件混在一起。
-- ============================================================================

-- ── 一、种子：822港航实例 ───────────────────────────────────────────────────
INSERT INTO domain_instance (instance_id, domain_id, label, is_default, sort_no, note) VALUES
    ('822gh', 'audit', '822港航', FALSE, 20,
     '审核领域第二个实例。起始完全空白：制度文件、规则、本体、发布版本均为 0，'
     '需要用户自行上传制度文件、走规则抽取/本体构建流程建立，不克隆福建海运集团'
     '现有资产。')
ON CONFLICT (instance_id) DO NOTHING;

-- ── 二、业务链根表（主键不变，只加列）──────────────────────────────────────
ALTER TABLE org ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE org SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE org ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE org ADD CONSTRAINT fk_org_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_org_instance ON org (instance_id);

ALTER TABLE expense_bill ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE expense_bill SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE expense_bill ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE expense_bill ADD CONSTRAINT fk_expense_bill_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_expense_bill_instance ON expense_bill (instance_id);

COMMENT ON COLUMN expense_bill.instance_id IS
  '归属实例。bill_no 按用户决策维持**全局唯一**（不同实例的单号不会撞），'
  '所以这一列是直接加的，不进主键——但 /api/bills 等列表接口要靠它过滤，'
  '这是全库 59 张表里访问最频繁的分页列表之一，直表过滤比每次 join org 快。';

ALTER TABLE standard_limit ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE standard_limit SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE standard_limit ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE standard_limit ADD CONSTRAINT fk_standard_limit_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_standard_limit_instance ON standard_limit (instance_id);

ALTER TABLE rank_tier_map ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE rank_tier_map SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE rank_tier_map ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE rank_tier_map ADD CONSTRAINT fk_rank_tier_map_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_rank_tier_map_instance ON rank_tier_map (instance_id);

ALTER TABLE org_special_rule ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE org_special_rule SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE org_special_rule ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE org_special_rule ADD CONSTRAINT fk_org_special_rule_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_org_special_rule_instance ON org_special_rule (instance_id);

-- ── 三、policy_doc 及其子表（无写路径，只加列备查）─────────────────────────
ALTER TABLE policy_doc ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE policy_doc SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE policy_doc ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE policy_doc ADD CONSTRAINT fk_policy_doc_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE policy_doc_page ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE policy_doc_page pp SET instance_id = pd.instance_id
  FROM policy_doc pd WHERE pd.doc_id = pp.doc_id AND pp.instance_id IS NULL;
UPDATE policy_doc_page SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE policy_doc_page ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE policy_doc_page ADD CONSTRAINT fk_policy_doc_page_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE policy_clause ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE policy_clause pc SET instance_id = pd.instance_id
  FROM policy_doc pd WHERE pd.doc_id = pc.doc_id AND pc.instance_id IS NULL;
UPDATE policy_clause SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE policy_clause ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE policy_clause ADD CONSTRAINT fk_policy_clause_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE extract_task ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE extract_task et SET instance_id = pd.instance_id
  FROM policy_doc pd WHERE pd.doc_id = et.doc_id AND et.instance_id IS NULL;
UPDATE extract_task SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE extract_task ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE extract_task ADD CONSTRAINT fk_extract_task_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE extract_candidate ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE extract_candidate ec SET instance_id = et.instance_id
  FROM extract_task et WHERE et.task_id = ec.task_id AND ec.instance_id IS NULL;
UPDATE extract_candidate SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE extract_candidate ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE extract_candidate ADD CONSTRAINT fk_extract_candidate_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON TABLE policy_doc IS
  '制度文件库旧表。**0015/0034 之后无任何代码写入**，被 doc_lib_file 取代'
  '（前端 2026-10-21 已删掉对应页签）。本迁移只给它加 instance_id 备查，'
  '不代表这条链路还在用。';

-- ── 四、onto_adoption / onto_cand_action（主键是程序生成的 id，不改主键）──
ALTER TABLE onto_adoption ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE onto_adoption SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE onto_adoption ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE onto_adoption ADD CONSTRAINT fk_onto_adoption_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_onto_adoption_instance ON onto_adoption (instance_id);

ALTER TABLE onto_cand_action ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE onto_cand_action SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE onto_cand_action ALTER COLUMN instance_id SET NOT NULL;
DO $$ BEGIN
    ALTER TABLE onto_cand_action ADD CONSTRAINT fk_onto_cand_action_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS ix_onto_cand_action_instance ON onto_cand_action (instance_id);
