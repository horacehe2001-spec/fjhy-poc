-- ============================================================================
-- 0012 · 报账单出行人员 + 标准依据人
-- ----------------------------------------------------------------------------
-- 这两个字段是解析审批意见 PDF 之后才补的（C0129 / C0130）。
--
-- **它们不是 trip_application（出差审批单）的字段。** 出行人员记在报账单的
-- 审批意见页上；C0301「出差人名单」是 OA 出差审批单上的字段。
-- 两者看着像，但一个是「这张单报了谁的费用」、一个是「审批单批了谁出差」——
-- TR-D2-03 要比的正是这两者是否一致。塞进同一张表，那条规则就变成
-- 自己和自己比，永远通过，而且报告上看不出来。
--
-- 另外 standard_basis_person（标准依据人）决定多人同行时按谁的职级算住宿标准，
-- 是住宿费档位链在多人单上的入口。
-- ============================================================================

CREATE TABLE IF NOT EXISTS bill_traveler (
    id        BIGSERIAL PRIMARY KEY,
    bill_no   TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    emp_no    TEXT,                                   -- C0129 出行人员（审批意见页里的真实工号）
    name      TEXT,                                   -- C0129 出行人员·姓名【敏感】
    ord       SMALLINT,
    UNIQUE (bill_no, emp_no)
);

CREATE INDEX IF NOT EXISTS ix_traveler_bill ON bill_traveler (bill_no);

COMMENT ON TABLE bill_traveler IS
  '报账单的出行人员（C0129）。实测 7 张多人单都有这一栏，单人单没有'
  '（出行人就是报账人）。多人单的人数由它数出来，比用「分摊部门去重」更准——'
  '分摊部门去重会把同部门两个人算成一个，人数算小则人日均算大，'
  '方向上偏安全但不准。';

ALTER TABLE expense_bill
    ADD COLUMN IF NOT EXISTS standard_basis_person TEXT;   -- C0130 标准依据人

COMMENT ON COLUMN expense_bill.standard_basis_person IS
  '标准依据人（C0130）。多人同行时住宿标准按这个人的职级算。'
  '实测 7 张多人单都有。没有它，多人单的住宿费档位无从确定。';

-- 审批意见页还给出了这些原先取不到的字段，补上
ALTER TABLE expense_bill ADD COLUMN IF NOT EXISTS applicant_rank TEXT;    -- 申请人栏里的职级
COMMENT ON COLUMN expense_bill.applicant_rank IS
  '申请人栏「姓名/职级/部门」里的职级。放在单据上而不是只放在 employee 上，'
  '因为它是**这张单提交时**的职级——人的职级会变，而单据的判定要用当时的职级。'
  'employee.rank_level 存的是当前职级，两者可能不同，审计时要用单据上这一份。';
