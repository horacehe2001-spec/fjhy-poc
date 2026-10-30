-- ============================================================================
-- 0046 · 业务子表的 instance_id 由父表自动派生
-- ----------------------------------------------------------------------------
-- 0044 给 10 张业务子表加了 instance_id NOT NULL（枚举外键变三列的必然结果）。
-- 代价立刻出现：全量回归 48 个 error + 16 failed，全是 NotNullViolation——
-- 种子、演示数据生成器、感知链路里 22 处 INSERT 都没带这一列。
--
-- 【为什么不是逐个打补丁】
--   补 22 处只能解决今天这 22 处。**下一个写入方还是会忘**，
--   而忘了的表现是运行时 NotNullViolation——在演示现场就是一个红屏。
--   更要紧的是：这 10 张表的归属**完全由父表决定**（报账单的附件必然和报账单
--   同属一个实例），它是**派生属性，不是录入项**。录入项才需要调用方填。
--   让调用方填一个本来就推得出来的值，等于给每个写入点都埋一个忘记的机会。
--
-- 【做法】
--   BEFORE INSERT OR UPDATE 触发器：instance_id 为空就从父表取。
--   显式传了就用传的（并且仍然受组合外键约束，传错了会被拒）。
--   父表找不到时**不兜底成默认实例**，让它继续报 NOT NULL——
--   父表不存在本来就该失败，悄悄塞一个默认值会让孤儿数据带着错的归属入库。
--
-- 【为什么用触发器而不是生成列】
--   GENERATED ALWAYS AS 只能引用**本行**的列，查不了父表。
--   标准表那两列（std_enum/tier_enum）能用生成列是因为它们是常量。
--
-- 父子关系（逐条核对过 pg_constraint，不是凭印象）：
--   approval_record / attachment / authority_approval / expense_detail /
--   invoice / meeting_notice / transport_voucher / trip_application
--       → expense_bill(bill_no)
--   employee      → org(org_id)
--   trip_calendar → trip_application(app_no)   ← 它没有 bill_no，隔了一层
-- ============================================================================

CREATE OR REPLACE FUNCTION fjhy_inherit_instance_from_bill() RETURNS trigger AS $$
BEGIN
    IF NEW.instance_id IS NULL THEN
        SELECT instance_id INTO NEW.instance_id
          FROM expense_bill WHERE bill_no = NEW.bill_no;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fjhy_inherit_instance_from_org() RETURNS trigger AS $$
BEGIN
    IF NEW.instance_id IS NULL THEN
        SELECT instance_id INTO NEW.instance_id FROM org WHERE org_id = NEW.org_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fjhy_inherit_instance_from_trip() RETURNS trigger AS $$
BEGIN
    IF NEW.instance_id IS NULL THEN
        SELECT instance_id INTO NEW.instance_id
          FROM trip_application WHERE app_no = NEW.app_no;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION fjhy_inherit_instance_from_bill() IS
  '子表的 instance_id 从报账单继承。**派生属性，不是录入项**——'
  '报账单的附件、发票、明细、审批记录必然和报账单同属一个实例，'
  '让每个写入点自己填等于给每个写入点埋一个忘记的机会。'
  '显式传了就用传的（仍受组合外键约束）；父表不存在时不兜底，让它照常报错。';

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['approval_record', 'attachment', 'authority_approval',
                             'expense_detail', 'invoice', 'meeting_notice',
                             'transport_voucher', 'trip_application']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_instance ON %I', t, t);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_instance BEFORE INSERT OR UPDATE ON %I
             FOR EACH ROW EXECUTE FUNCTION fjhy_inherit_instance_from_bill()', t, t);
    END LOOP;
END $$;

DROP TRIGGER IF EXISTS trg_employee_instance ON employee;
CREATE TRIGGER trg_employee_instance BEFORE INSERT OR UPDATE ON employee
    FOR EACH ROW EXECUTE FUNCTION fjhy_inherit_instance_from_org();

DROP TRIGGER IF EXISTS trg_trip_calendar_instance ON trip_calendar;
CREATE TRIGGER trg_trip_calendar_instance BEFORE INSERT OR UPDATE ON trip_calendar
    FOR EACH ROW EXECUTE FUNCTION fjhy_inherit_instance_from_trip();
