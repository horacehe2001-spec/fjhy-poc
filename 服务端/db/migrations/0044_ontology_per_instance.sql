-- ============================================================================
-- 0044 · 本体链按实例隔离
-- ----------------------------------------------------------------------------
-- 【为什么非做不可】
--   0042/0043 把多模态读图接进抽取之后，要给 822港航 建它自己的住宿费限额，
--   却被这道闸拦住：
--       standard_limit.tier_enum 是 GENERATED ALWAYS AS ('EN21')（迁移 0008），
--       FOREIGN KEY (tier_enum, tier) REFERENCES enum_value_current(enum_id, value)
--   也就是说**档位名必须是本体枚举 EN21 里定义过的值**——这道闸是对的，
--   它防的是"现编一个档位名"。但 EN21 只有 福建海运集团 那两档
--   （公司领导 / 其他人员），而 822港航 的附件1 用的是三档
--   （第一档在源文件里没有表头 / 厅局级 / 其他人员）。
--
--   于是暴露出：**「住宿费标准档位」这种枚举是实例特有的制度内容，
--   不是通用领域模型。** 0042 当时定的"本体共用一份"在这里不成立。
--   用户 2026-10-30 决定：本体链按实例隔离。
--
-- 【为什么会牵连 10 张业务子表】
--   enum_value_current 的主键从 (enum_id, value) 变成
--   (instance_id, enum_id, value) 之后，**所有指向它的外键都必须带上
--   instance_id**——这是 Postgres 的硬要求，不是设计选择。
--   24 条外键里有 10 张子表还没有这一列（approval_record / attachment /
--   expense_detail / invoice / trip_application 等），所以它们跟着加。
--   **这推翻了 0040 当时"bill_no 全局唯一所以子表一列都不用加"那条基准**，
--   原因是那条基准只考虑了业务隔离，没考虑枚举外键。加的列是归属标记，
--   不改变 bill_no 仍然全局唯一这件事。
--
-- 【顺序是硬要求，分四段】
--   ①全部加列回填 → ②删掉所有指向父表的外键 → ③换父表主键 → ④重建组合外键。
--   按表分段写会在第一句就被 PG 拒绝（cannot drop constraint ... because
--   other objects depend on it）——0041 在这上面连着失败过两次。
--
-- 【这个文件的 SQL 是生成的，不是手抄的】
--   24 条外键的列名（node_enum/node、type_enum/attach_type …）各不相同，
--   手抄一个错字就是一条静默的坏约束。生成脚本从 pg_constraint 读实际定义。
--
-- 本次变更前已 pg_dump：服务端/var/backup/pre_onto_isolation_20261030_211220.sql
-- ============================================================================

-- ── 第一阶段：所有相关表加 instance_id 并回填 ──────────────────────────
-- 存量数据全部属于福建海运集团：822港航 此前只有制度文件与条款，
-- 没有任何业务单据、也没有自己的本体。回填 'fjhy' 是如实归属，不是默认值。
ALTER TABLE ontology_version ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_version SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_version ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_version DROP CONSTRAINT IF EXISTS fk_ontology_version_instance;
ALTER TABLE ontology_version ADD CONSTRAINT fk_ontology_version_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE ontology_entity ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_entity SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_entity ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_entity DROP CONSTRAINT IF EXISTS fk_ontology_entity_instance;
ALTER TABLE ontology_entity ADD CONSTRAINT fk_ontology_entity_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE ontology_concept ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_concept SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_concept ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_concept DROP CONSTRAINT IF EXISTS fk_ontology_concept_instance;
ALTER TABLE ontology_concept ADD CONSTRAINT fk_ontology_concept_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE ontology_enum ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_enum SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_enum ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_enum DROP CONSTRAINT IF EXISTS fk_ontology_enum_instance;
ALTER TABLE ontology_enum ADD CONSTRAINT fk_ontology_enum_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE ontology_enum_value ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_enum_value SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_enum_value ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_enum_value DROP CONSTRAINT IF EXISTS fk_ontology_enum_value_instance;
ALTER TABLE ontology_enum_value ADD CONSTRAINT fk_ontology_enum_value_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE ontology_relation ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE ontology_relation SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE ontology_relation ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE ontology_relation DROP CONSTRAINT IF EXISTS fk_ontology_relation_instance;
ALTER TABLE ontology_relation ADD CONSTRAINT fk_ontology_relation_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE process_node ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE process_node SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE process_node ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE process_node DROP CONSTRAINT IF EXISTS fk_process_node_instance;
ALTER TABLE process_node ADD CONSTRAINT fk_process_node_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE process_transition ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE process_transition SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE process_transition ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE process_transition DROP CONSTRAINT IF EXISTS fk_process_transition_instance;
ALTER TABLE process_transition ADD CONSTRAINT fk_process_transition_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE enum_value_current ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE enum_value_current SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE enum_value_current ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE enum_value_current DROP CONSTRAINT IF EXISTS fk_enum_value_current_instance;
ALTER TABLE enum_value_current ADD CONSTRAINT fk_enum_value_current_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE approval_record ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE approval_record SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE approval_record ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE approval_record DROP CONSTRAINT IF EXISTS fk_approval_record_instance;
ALTER TABLE approval_record ADD CONSTRAINT fk_approval_record_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE attachment ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE attachment SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE attachment ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE attachment DROP CONSTRAINT IF EXISTS fk_attachment_instance;
ALTER TABLE attachment ADD CONSTRAINT fk_attachment_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE authority_approval ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE authority_approval SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE authority_approval ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE authority_approval DROP CONSTRAINT IF EXISTS fk_authority_approval_instance;
ALTER TABLE authority_approval ADD CONSTRAINT fk_authority_approval_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE employee ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE employee SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE employee ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE employee DROP CONSTRAINT IF EXISTS fk_employee_instance;
ALTER TABLE employee ADD CONSTRAINT fk_employee_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE expense_detail ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE expense_detail SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE expense_detail ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE expense_detail DROP CONSTRAINT IF EXISTS fk_expense_detail_instance;
ALTER TABLE expense_detail ADD CONSTRAINT fk_expense_detail_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE invoice ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE invoice SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE invoice ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE invoice DROP CONSTRAINT IF EXISTS fk_invoice_instance;
ALTER TABLE invoice ADD CONSTRAINT fk_invoice_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE meeting_notice ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE meeting_notice SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE meeting_notice ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE meeting_notice DROP CONSTRAINT IF EXISTS fk_meeting_notice_instance;
ALTER TABLE meeting_notice ADD CONSTRAINT fk_meeting_notice_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE transport_voucher ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE transport_voucher SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE transport_voucher ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE transport_voucher DROP CONSTRAINT IF EXISTS fk_transport_voucher_instance;
ALTER TABLE transport_voucher ADD CONSTRAINT fk_transport_voucher_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE trip_application ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE trip_application SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE trip_application ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE trip_application DROP CONSTRAINT IF EXISTS fk_trip_application_instance;
ALTER TABLE trip_application ADD CONSTRAINT fk_trip_application_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;
ALTER TABLE trip_calendar ADD COLUMN IF NOT EXISTS instance_id TEXT;
UPDATE trip_calendar SET instance_id = 'fjhy' WHERE instance_id IS NULL;
ALTER TABLE trip_calendar ALTER COLUMN instance_id SET NOT NULL;
ALTER TABLE trip_calendar DROP CONSTRAINT IF EXISTS fk_trip_calendar_instance;
ALTER TABLE trip_calendar ADD CONSTRAINT fk_trip_calendar_instance
    FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id) ON DELETE RESTRICT;

-- ── 第二阶段：删掉所有指向本体链九表的外键（31 条）────────────────────
-- **必须在换主键之前。** 第一版只删了指向 enum_value_current 和
-- ontology_version 的，漏了本体链内部那两条（concept→entity、
-- enum_value→enum），于是在 ontology_entity 换主键那一句被拒：
--   cannot drop constraint ontology_entity_pkey because other objects depend on it
-- 教训和 0041 完全一样：**依赖清单要从 pg_constraint 全量查，不能凭印象列。**
ALTER TABLE approval_record DROP CONSTRAINT IF EXISTS fk_appr_node;
ALTER TABLE attachment DROP CONSTRAINT IF EXISTS fk_att_type;
ALTER TABLE authority_approval DROP CONSTRAINT IF EXISTS fk_auth_matter;
ALTER TABLE employee DROP CONSTRAINT IF EXISTS fk_emp_rank;
ALTER TABLE expense_bill DROP CONSTRAINT IF EXISTS fk_bill_biztype;
ALTER TABLE expense_bill DROP CONSTRAINT IF EXISTS fk_bill_doctype;
ALTER TABLE expense_bill DROP CONSTRAINT IF EXISTS fk_bill_status;
ALTER TABLE expense_detail DROP CONSTRAINT IF EXISTS fk_detail_item;
ALTER TABLE invoice DROP CONSTRAINT IF EXISTS fk_inv_type;
ALTER TABLE invoice DROP CONSTRAINT IF EXISTS fk_inv_void;
ALTER TABLE meeting_notice DROP CONSTRAINT IF EXISTS fk_mn_meal;
ALTER TABLE org DROP CONSTRAINT IF EXISTS fk_org_dm;
ALTER TABLE org DROP CONSTRAINT IF EXISTS fk_org_ind;
ALTER TABLE org_special_rule DROP CONSTRAINT IF EXISTS fk_osr_cat;
ALTER TABLE org_special_rule DROP CONSTRAINT IF EXISTS fk_osr_dm;
ALTER TABLE rank_tier_map DROP CONSTRAINT IF EXISTS fk_rtm_rank;
ALTER TABLE rank_tier_map DROP CONSTRAINT IF EXISTS fk_rtm_tier;
ALTER TABLE standard_limit DROP CONSTRAINT IF EXISTS fk_std_tier;
ALTER TABLE standard_limit DROP CONSTRAINT IF EXISTS fk_std_type;
ALTER TABLE transport_voucher DROP CONSTRAINT IF EXISTS fk_tv_vehicle;
ALTER TABLE trip_application DROP CONSTRAINT IF EXISTS fk_trip_car;
ALTER TABLE trip_application DROP CONSTRAINT IF EXISTS fk_trip_form;
ALTER TABLE trip_application DROP CONSTRAINT IF EXISTS fk_trip_meal;
ALTER TABLE trip_calendar DROP CONSTRAINT IF EXISTS fk_cal_slot;
ALTER TABLE ontology_concept DROP CONSTRAINT IF EXISTS ontology_concept_version_entity_id_fkey;
ALTER TABLE ontology_enum_value DROP CONSTRAINT IF EXISTS ontology_enum_value_version_enum_id_fkey;
ALTER TABLE ontology_entity DROP CONSTRAINT IF EXISTS ontology_entity_version_fkey;
ALTER TABLE ontology_enum DROP CONSTRAINT IF EXISTS ontology_enum_version_fkey;
ALTER TABLE ontology_relation DROP CONSTRAINT IF EXISTS ontology_relation_version_fkey;
ALTER TABLE process_node DROP CONSTRAINT IF EXISTS process_node_version_fkey;
ALTER TABLE process_transition DROP CONSTRAINT IF EXISTS process_transition_version_fkey;

-- ── 第三阶段：换本体链的主键 ───────────────────────────────────────────
ALTER TABLE ontology_version DROP CONSTRAINT IF EXISTS ontology_version_pkey;
ALTER TABLE ontology_version ADD CONSTRAINT ontology_version_pkey PRIMARY KEY (instance_id, version);
ALTER TABLE ontology_entity DROP CONSTRAINT IF EXISTS ontology_entity_pkey;
ALTER TABLE ontology_entity ADD CONSTRAINT ontology_entity_pkey PRIMARY KEY (instance_id, version, entity_id);
ALTER TABLE ontology_concept DROP CONSTRAINT IF EXISTS ontology_concept_pkey;
ALTER TABLE ontology_concept ADD CONSTRAINT ontology_concept_pkey PRIMARY KEY (instance_id, version, concept_id);
ALTER TABLE ontology_enum DROP CONSTRAINT IF EXISTS ontology_enum_pkey;
ALTER TABLE ontology_enum ADD CONSTRAINT ontology_enum_pkey PRIMARY KEY (instance_id, version, enum_id);
ALTER TABLE ontology_enum_value DROP CONSTRAINT IF EXISTS ontology_enum_value_pkey;
ALTER TABLE ontology_enum_value ADD CONSTRAINT ontology_enum_value_pkey PRIMARY KEY (instance_id, version, enum_id, value);
ALTER TABLE process_node DROP CONSTRAINT IF EXISTS process_node_pkey;
ALTER TABLE process_node ADD CONSTRAINT process_node_pkey PRIMARY KEY (instance_id, version, node_id);
ALTER TABLE enum_value_current DROP CONSTRAINT IF EXISTS enum_value_current_pkey;
ALTER TABLE enum_value_current ADD CONSTRAINT enum_value_current_pkey PRIMARY KEY (instance_id, enum_id, value);
-- ontology_relation / process_transition 的主键是代理键（id），不用动，只加归属列。

-- ── 第四阶段：重建组合外键（31 条，全部带上 instance_id）──────────────
ALTER TABLE ontology_concept ADD CONSTRAINT ontology_concept_version_entity_id_fkey
    FOREIGN KEY (instance_id, version, entity_id)
    REFERENCES ontology_entity(instance_id, version, entity_id) ON DELETE CASCADE;
ALTER TABLE ontology_enum_value ADD CONSTRAINT ontology_enum_value_version_enum_id_fkey
    FOREIGN KEY (instance_id, version, enum_id)
    REFERENCES ontology_enum(instance_id, version, enum_id) ON DELETE CASCADE;
ALTER TABLE ontology_entity ADD CONSTRAINT ontology_entity_version_fkey
    FOREIGN KEY (instance_id, version)
    REFERENCES ontology_version(instance_id, version) ON DELETE CASCADE;
ALTER TABLE ontology_enum ADD CONSTRAINT ontology_enum_version_fkey
    FOREIGN KEY (instance_id, version)
    REFERENCES ontology_version(instance_id, version) ON DELETE CASCADE;
ALTER TABLE ontology_relation ADD CONSTRAINT ontology_relation_version_fkey
    FOREIGN KEY (instance_id, version)
    REFERENCES ontology_version(instance_id, version) ON DELETE CASCADE;
ALTER TABLE process_node ADD CONSTRAINT process_node_version_fkey
    FOREIGN KEY (instance_id, version)
    REFERENCES ontology_version(instance_id, version) ON DELETE CASCADE;
ALTER TABLE process_transition ADD CONSTRAINT process_transition_version_fkey
    FOREIGN KEY (instance_id, version)
    REFERENCES ontology_version(instance_id, version) ON DELETE CASCADE;
ALTER TABLE approval_record ADD CONSTRAINT fk_appr_node
    FOREIGN KEY (instance_id, node_enum, node)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE attachment ADD CONSTRAINT fk_att_type
    FOREIGN KEY (instance_id, type_enum, attach_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE authority_approval ADD CONSTRAINT fk_auth_matter
    FOREIGN KEY (instance_id, matter_enum, matter_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE employee ADD CONSTRAINT fk_emp_rank
    FOREIGN KEY (instance_id, rank_enum, rank_level)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE expense_bill ADD CONSTRAINT fk_bill_biztype
    FOREIGN KEY (instance_id, biz_type_enum, biz_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE expense_bill ADD CONSTRAINT fk_bill_doctype
    FOREIGN KEY (instance_id, doc_type_enum, doc_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE expense_bill ADD CONSTRAINT fk_bill_status
    FOREIGN KEY (instance_id, status_enum, bill_status)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE expense_detail ADD CONSTRAINT fk_detail_item
    FOREIGN KEY (instance_id, item_enum, expense_item)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE invoice ADD CONSTRAINT fk_inv_type
    FOREIGN KEY (instance_id, type_enum, inv_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE invoice ADD CONSTRAINT fk_inv_void
    FOREIGN KEY (instance_id, void_enum, void_status)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE meeting_notice ADD CONSTRAINT fk_mn_meal
    FOREIGN KEY (instance_id, meal_enum, meal_provided)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE org ADD CONSTRAINT fk_org_dm
    FOREIGN KEY (instance_id, direct_managed_enum, direct_managed)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE org ADD CONSTRAINT fk_org_ind
    FOREIGN KEY (instance_id, industry_enum, industry)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE org_special_rule ADD CONSTRAINT fk_osr_cat
    FOREIGN KEY (instance_id, cat_enum, rule_category)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE org_special_rule ADD CONSTRAINT fk_osr_dm
    FOREIGN KEY (instance_id, dm_enum, direct_managed)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE rank_tier_map ADD CONSTRAINT fk_rtm_rank
    FOREIGN KEY (instance_id, rank_enum, rank_level)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE rank_tier_map ADD CONSTRAINT fk_rtm_tier
    FOREIGN KEY (instance_id, tier_enum, tier)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE standard_limit ADD CONSTRAINT fk_std_tier
    FOREIGN KEY (instance_id, tier_enum, tier)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE standard_limit ADD CONSTRAINT fk_std_type
    FOREIGN KEY (instance_id, std_enum, std_type)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE transport_voucher ADD CONSTRAINT fk_tv_vehicle
    FOREIGN KEY (instance_id, vehicle_enum, vehicle)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trip_application ADD CONSTRAINT fk_trip_car
    FOREIGN KEY (instance_id, car_enum, car_situation)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trip_application ADD CONSTRAINT fk_trip_form
    FOREIGN KEY (instance_id, form_enum, approve_form)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trip_application ADD CONSTRAINT fk_trip_meal
    FOREIGN KEY (instance_id, meal_enum, meal_situation)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE trip_calendar ADD CONSTRAINT fk_cal_slot
    FOREIGN KEY (instance_id, slot_enum, meal_slot)
    REFERENCES enum_value_current(instance_id, enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON COLUMN enum_value_current.instance_id IS
  '这套枚举取值属于哪个实例。**枚举不是通用领域模型**：住宿费标准档位
   福建海运集团 分两档（公司领导/其他人员），822港航 分三档且名字不同，
   两边都是各自制度的投影。0042 当时按「本体=领域模型、两实例共用」处理，
   在 822港航 建限额时就撞上了——档位名不在共用枚举里，写不进去。';

COMMENT ON COLUMN ontology_version.instance_id IS
  '本体属于哪个实例。源文件也随之一对一：本体_v0.1.yaml 是 福建海运集团 的
   （meta.entity 写着财智分公司），别的实例要有自己那一份。';
