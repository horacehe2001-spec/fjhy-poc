-- ============================================================================
-- 0041 · 审核领域第二实例：资产链主键改造（rule / release / lookup_table）
-- ----------------------------------------------------------------------------
-- 这三张表的业务主键是人工/装载器赋的编号（rule_id 如 "TR-D4-05"，release_id
-- 如 "rel-0001"，rule_set_version 如 "rs-20261021-01"，table_id 如 "必要附件决策表"），
-- **不是随机生成**。822港航独立抽取规则时很可能撞上同样的编号——
-- D1~D10 是通用审核维度，不是福建海运集团专属，两边各自从 01 往后编号，
-- "TR-D4-05" 完全可能在两边各出现一次。不改主键的话，第二个实例一写就是
-- 主键冲突（rule_id/release_id/table_id）或唯一约束冲突（rule_set_version）。
--
-- 做法：主键/唯一约束里加 instance_id，子表跟着加列升级成组合外键。
-- 子表范围按"谁的外键指向这三张根表"算出来的（pg_constraint 实查，不是猜的）：
--   rule        → rule_concept / rule_entity / rule_relation / onto_rule_cover
--   release     → release_item / release_asset / release_access_log
--   lookup_table → lookup_row
--
-- 【语句顺序是硬要求，不是风格问题】
--   第一版把"删父表主键"写在了子表外键之前，PG 直接拒绝：
--     cannot drop constraint rule_pkey on table rule because other objects depend on it
--   所以分四段走，不能按表分段：
--     ① 所有表（父+子）先加 instance_id 列并回填
--     ② 删掉所有指向这三张父表的子表外键
--     ③ 换父表的主键/唯一约束
--     ④ 建子表的新主键与组合外键
--   迁移是单文件事务，中途失败整体回滚，不会留半成品——第一版失败时就是这样
--   干净退回去的。
--
-- 对应要跟着改的 Python 写入点（全仓库 grep 过，只有这几处）：
--   app/rules/loader.py   —— 装载脚本，INSERT 时补 instance_id='fjhy'
--     （这个脚本只装载福建海运集团手工整理的 规则清单_v0.1.json 等固定文件，
--      822港航没有等价文件，不需要让这个脚本变成通用的——它本来就是
--      一次性的客户专属装载器，不是实例无关的工具）
--   app/onto_coord.py     —— 统一口径"采纳"建规则的**活接口**，822港航会走这条
--   app/release_pack.py   —— 发布管理"冻结为新版本"的**活接口**
-- 这三个文件在下一步（Python 代码改动批次）一起改，迁移先把库结构铺好。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- ① 先加列并回填（父表与子表一起，子表优先按父表的归属回填）
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE rule               ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE rule_concept       ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE rule_entity        ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE rule_relation      ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE onto_rule_cover    ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE release            ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE release_item       ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE release_asset      ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE release_access_log ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE release_current    ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE lookup_table       ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE lookup_row         ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE onto_align         ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE onto_candidate     ADD COLUMN IF NOT EXISTS instance_id TEXT;

-- 父表：本迁移之前库里的全部资产都属于「审核 / 福建海运集团」（0032/0033/0034 口径）
UPDATE rule         SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE release      SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE lookup_table SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE onto_align     SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE onto_candidate SET instance_id = 'fjhy' WHERE instance_id IS NULL;

-- 子表：按父表的归属回填，父行已不存在的（release_item 是弱引用，0025 拆过外键）
-- 兜底填 fjhy——历史数据本来就全是它的。
UPDATE rule_concept rc SET instance_id = r.instance_id
  FROM rule r WHERE r.rule_id = rc.rule_id AND r.version = rc.version
  AND rc.instance_id IS NULL;
UPDATE rule_entity re SET instance_id = r.instance_id
  FROM rule r WHERE r.rule_id = re.rule_id AND r.version = re.version
  AND re.instance_id IS NULL;
UPDATE rule_relation rr SET instance_id = r.instance_id
  FROM rule r WHERE r.rule_id = rr.rule_id AND r.version = rr.version
  AND rr.instance_id IS NULL;
UPDATE onto_rule_cover orc SET instance_id = r.instance_id
  FROM rule r WHERE r.rule_id = orc.rule_id AND r.version = orc.version
  AND orc.instance_id IS NULL;
UPDATE release_item ri SET instance_id = r.instance_id
  FROM release r WHERE r.release_id = ri.release_id AND ri.instance_id IS NULL;
UPDATE release_asset ra SET instance_id = r.instance_id
  FROM release r WHERE r.release_id = ra.release_id AND ra.instance_id IS NULL;
UPDATE release_access_log ral SET instance_id = r.instance_id
  FROM release r WHERE r.release_id = ral.release_id AND ral.instance_id IS NULL;
UPDATE release_current rc SET instance_id = r.instance_id
  FROM release r WHERE r.release_id = rc.release_id AND rc.instance_id IS NULL;
UPDATE release_current SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE lookup_row lr SET instance_id = lt.instance_id
  FROM lookup_table lt WHERE lt.table_id = lr.table_id AND lr.instance_id IS NULL;

UPDATE rule_concept    SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE rule_entity     SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE rule_relation   SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE onto_rule_cover SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE release_item    SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE release_asset   SET instance_id = 'fjhy' WHERE instance_id IS NULL;
UPDATE lookup_row      SET instance_id = 'fjhy' WHERE instance_id IS NULL;
-- release_access_log 不兜底：它的 release_id 本来就允许 NULL（版本被删时 SET NULL），
-- 那种行算不出归属，如实留空，不挑一个填上去（与 0034 对 doc_lib_scan 的处理同口径）。

ALTER TABLE rule               ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE rule_concept       ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE rule_entity        ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE rule_relation      ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE onto_rule_cover    ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE release            ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE release_item       ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE release_asset      ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE release_current    ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE lookup_table       ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE lookup_row         ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE onto_align         ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE onto_candidate     ALTER COLUMN instance_id SET NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- ② 删掉所有指向这三张父表的子表外键（必须在换父表主键之前）
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE rule_concept       DROP CONSTRAINT rule_concept_rule_id_version_fkey;
ALTER TABLE rule_entity        DROP CONSTRAINT rule_entity_rule_id_version_fkey;
ALTER TABLE rule_relation      DROP CONSTRAINT rule_relation_rule_id_version_fkey;
ALTER TABLE onto_rule_cover    DROP CONSTRAINT onto_rule_cover_rule_id_version_fkey;
ALTER TABLE release_item       DROP CONSTRAINT release_item_release_id_fkey;
ALTER TABLE release_asset      DROP CONSTRAINT release_asset_release_id_fkey;
ALTER TABLE release_access_log DROP CONSTRAINT release_access_log_release_id_fkey;
ALTER TABLE release_current    DROP CONSTRAINT release_current_release_id_fkey;
ALTER TABLE lookup_row         DROP CONSTRAINT lookup_row_table_id_fkey;

-- ─────────────────────────────────────────────────────────────────────────────
-- ③ 换父表的主键与唯一约束
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE rule DROP CONSTRAINT rule_pkey;
ALTER TABLE rule ADD CONSTRAINT rule_pkey PRIMARY KEY (instance_id, rule_id, version);

ALTER TABLE release DROP CONSTRAINT release_pkey;
ALTER TABLE release DROP CONSTRAINT release_rule_set_version_key;
ALTER TABLE release ADD CONSTRAINT release_pkey PRIMARY KEY (instance_id, release_id);
ALTER TABLE release ADD CONSTRAINT release_rule_set_version_key
    UNIQUE (instance_id, rule_set_version);

ALTER TABLE lookup_table DROP CONSTRAINT lookup_table_pkey;
ALTER TABLE lookup_table ADD CONSTRAINT lookup_table_pkey PRIMARY KEY (instance_id, table_id);

ALTER TABLE onto_align DROP CONSTRAINT onto_align_pkey;
ALTER TABLE onto_align ADD CONSTRAINT onto_align_pkey PRIMARY KEY (instance_id, group_key);

ALTER TABLE onto_candidate DROP CONSTRAINT onto_candidate_pkey;
ALTER TABLE onto_candidate ADD CONSTRAINT onto_candidate_pkey PRIMARY KEY (instance_id, cand_id);

-- 父表各自挂到 domain_instance
DO $$ BEGIN
    ALTER TABLE rule ADD CONSTRAINT fk_rule_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE release ADD CONSTRAINT fk_release_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE lookup_table ADD CONSTRAINT fk_lookup_table_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE onto_align ADD CONSTRAINT fk_onto_align_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE onto_candidate ADD CONSTRAINT fk_onto_candidate_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- ④ 子表：换主键 + 建组合外键
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE rule_concept DROP CONSTRAINT rule_concept_pkey;
ALTER TABLE rule_concept ADD CONSTRAINT rule_concept_pkey
    PRIMARY KEY (instance_id, rule_id, version, concept_id);
ALTER TABLE rule_concept ADD CONSTRAINT fk_rule_concept_rule
    FOREIGN KEY (instance_id, rule_id, version) REFERENCES rule (instance_id, rule_id, version)
    ON DELETE CASCADE;

ALTER TABLE rule_entity DROP CONSTRAINT rule_entity_pkey;
ALTER TABLE rule_entity ADD CONSTRAINT rule_entity_pkey
    PRIMARY KEY (instance_id, rule_id, version, entity_id);
ALTER TABLE rule_entity ADD CONSTRAINT fk_rule_entity_rule
    FOREIGN KEY (instance_id, rule_id, version) REFERENCES rule (instance_id, rule_id, version)
    ON DELETE CASCADE;

-- rule_relation 的主键是 BIGSERIAL id，不动主键，只补组合外键
ALTER TABLE rule_relation ADD CONSTRAINT fk_rule_relation_rule
    FOREIGN KEY (instance_id, rule_id, version) REFERENCES rule (instance_id, rule_id, version)
    ON DELETE CASCADE;

ALTER TABLE onto_rule_cover DROP CONSTRAINT onto_rule_cover_pkey;
ALTER TABLE onto_rule_cover ADD CONSTRAINT onto_rule_cover_pkey
    PRIMARY KEY (instance_id, rule_id, version);
ALTER TABLE onto_rule_cover ADD CONSTRAINT fk_onto_rule_cover_rule
    FOREIGN KEY (instance_id, rule_id, version) REFERENCES rule (instance_id, rule_id, version)
    ON DELETE CASCADE;

-- release_item 的主键是 BIGSERIAL id，不动主键
ALTER TABLE release_item ADD CONSTRAINT fk_release_item_release
    FOREIGN KEY (instance_id, release_id) REFERENCES release (instance_id, release_id)
    ON DELETE CASCADE;

ALTER TABLE release_asset DROP CONSTRAINT release_asset_pkey;
ALTER TABLE release_asset ADD CONSTRAINT release_asset_pkey
    PRIMARY KEY (instance_id, release_id, layer);
ALTER TABLE release_asset ADD CONSTRAINT fk_release_asset_release
    FOREIGN KEY (instance_id, release_id) REFERENCES release (instance_id, release_id)
    ON DELETE CASCADE;

-- release_access_log：两列同生共死（版本被删时一起置空），所以 instance_id 允许 NULL
ALTER TABLE release_access_log ADD CONSTRAINT fk_release_access_release
    FOREIGN KEY (instance_id, release_id) REFERENCES release (instance_id, release_id)
    ON DELETE SET NULL;

-- release_current：从「全库单行」改成「每个实例一行」。
-- 原来是 singleton BOOLEAN PRIMARY KEY DEFAULT TRUE + CHECK(singleton)，
-- 用数据库强制「当前规则集只有一个」（0005 的注释解释过为什么不用
-- 「取 published 里最新的一条」：回滚时会选错，因为旧版的 published_at 更早）。
-- **那条纪律没有被放松，只是改了作用域**：主键从 singleton 换成 instance_id，
-- 仍然是数据库级强制——每个实例有且只有一个当前版本，而不是全库一个。
-- singleton 列到这里已经没有意义（恒为 TRUE 的列），一并删掉；
-- release_pack.py 里那两处 INSERT 显式写了这个列名，跟着这批一起改。
ALTER TABLE release_current DROP CONSTRAINT release_current_pkey;
ALTER TABLE release_current DROP CONSTRAINT ck_singleton;
ALTER TABLE release_current DROP COLUMN singleton;
ALTER TABLE release_current ADD CONSTRAINT release_current_pkey PRIMARY KEY (instance_id);
ALTER TABLE release_current ADD CONSTRAINT fk_release_current_release
    FOREIGN KEY (instance_id, release_id) REFERENCES release (instance_id, release_id);

COMMENT ON TABLE release_current IS
  '每个实例当前生效的是哪一版。**0041 之前是全库单行表**（singleton BOOLEAN'
  ' PRIMARY KEY + CHECK(singleton)），审核领域有第二个实例之后那个形状不成立了：'
  '福建海运集团和 822港航 各自有自己的当前生效版本。'
  '主键换成 instance_id，「一个实例只有一个当前版本」仍然由数据库强制。'
  '为什么不用「取 published 里最新的一条」代替这张表：回滚是把 current 指回旧版，'
  '而旧版的 published_at 更早，按时间选会选错（0005 的原始注释）。';

-- lookup_row 的主键是 BIGSERIAL id，不动主键
ALTER TABLE lookup_row ADD CONSTRAINT fk_lookup_row_table
    FOREIGN KEY (instance_id, table_id) REFERENCES lookup_table (instance_id, table_id)
    ON DELETE CASCADE;

-- ─────────────────────────────────────────────────────────────────────────────
-- ⑤ 索引与列注释
-- ─────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS ix_rule_instance           ON rule (instance_id);
CREATE INDEX IF NOT EXISTS ix_rule_relation_instance  ON rule_relation (instance_id);
CREATE INDEX IF NOT EXISTS ix_release_instance        ON release (instance_id);
CREATE INDEX IF NOT EXISTS ix_release_item_instance   ON release_item (instance_id);
CREATE INDEX IF NOT EXISTS ix_release_access_instance ON release_access_log (instance_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_lookup_row_instance     ON lookup_row (instance_id, table_id);

COMMENT ON COLUMN rule.instance_id IS
  '归属实例，**在主键里**。rule_id 是人工/装载器编号（如 TR-D4-05），'
  '两个实例各自独立编号完全可能撞号——D1~D10 是通用审核维度，不是客户专属分类。'
  '不把它放进主键，822港航第一次采纳规则就会跟福建海运集团的同号规则主键冲突。';

COMMENT ON COLUMN release.instance_id IS
  '归属实例，**在主键里**。release_id/rule_set_version 都是装载器/发布流程按'
  '日期+序号生成的（如 rel-0001、rs-20261021-01），两个实例同一天各发一版'
  '完全可能撞号，所以连 rule_set_version 的唯一约束也从全局改成按实例。';

COMMENT ON COLUMN lookup_table.instance_id IS
  '归属实例，**在主键里**。table_id 是固定中文名（如"必要附件决策表"），'
  '两个实例的装载器都会用同一个名字建同一类查找表，不放进主键就会撞主键。';
