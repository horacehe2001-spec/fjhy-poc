-- ============================================================================
-- 0057 · entertain_approval（业务接待审批单）+ meeting_notice 补活动起止
-- ----------------------------------------------------------------------------
-- 【编号】0056 之后接着走。0040–0052 属另一棵源目录（见 0053 文件头）。
--
-- 【为什么要建这张表】
--   822港航 的 147 个附件里有 29 个是「业务接待审批单」，是全部类型里最多的
--   一类。它上面的字段是下面这几条规则唯一的输入，而库里没有任何表装得下：
--       TR-接待-01  业务接待先审批后接待（要 审批日期 + 接待时间）
--       TR-通用-04  异常事项说明充分性（要 审批滞后 这个判断）
--       TR-接待-04  用餐标准与陪餐人数（审批单上有 标准/人数/预算总额）
--       人工驳回「酒水请标注出规格」（审批单上有 赠送纪念品/酒水 那几格）
--   实测 2 份是 PDF 且有完整文字层，26 份是 JPG（要读图），1 份是扫描 PDF。
--   先建表、先把 2 份免费的装进来；26 份读图是下一批，**表结构两批共用**，
--   所以这里按整张表单的字段建全，不只建那 2 份用得上的。
--
-- 【接待对象那一列：存，但永不进 fact】
--   `guest_roster` 装的是「中建海峡厦门海峡成功大厦项目部项目经理陈志卿等人」
--   这种**外部人员姓名与职务黏连**的自由文本。这类值脱敏闭集合切不出来
--   （见 app/mask.py 的 roster 策略，以及 entertain_detail.guest_list 的既有
--   处理：入库但 app/perception/reader.py 只取算出来的人数、不取名单本身）。
--   列名**不叫 name 也不含 name**：脱敏是按字段名选策略的，叫 name 的列会被
--   当成人名字段整列打星号——规则名/本体名/服务名已经为此被打过四次星。
--
-- 【不加枚举外键】
--   `entertain_type` 票面取值是「商务接待 / 公务接待」，看着像 EN 里的枚举，
--   但本实例**没有接待类型码表**（客户未提供，见 app/rules/impl_822gh.py
--   文件头纪律 4：entertain_detail 的三个码列是 OTHER_ENTERTAIN / EAT_WORK
--   这种系统码，没有码表就不许当业务值用）。
--   这里存票面原文，不建 FK：建了 FK 等于宣称我们有一套权威取值，而我们没有。
--   `work_meal / car_arrange / lodging_arrange / gift_arrange` 同理，
--   票面取值实测是「无」或一段说明文字，不是封闭集合。
--
-- 【meeting_notice 补三列】
--   TR-差旅-30 要比「审批单出差日期范围 vs 附件通知上的活动起止」，
--   第三方一直缺。实测 8 份会议/培训通知里有 1 份有文字层：
--     「省律师协会定于8 月22 日至28 日在北京理工大学（珠海校区）举办」
--     「培训时间：8 月23 日—28 日」
--   而人工驳 FJPG-BX2608311795 的原话正是「按照培训通知体现出差时间应为
--   8.22-8.28」——通知上的日期就是人工的判据。meeting_notice 现有列
--   （unified_board / meal_provided / shuttle / self_funded）装的是另一件事，
--   没有日期和地点，所以补三列。其余 7 份是扫描件，留给读图那一批。
--
-- 【两张表都是空表起步，两棵源目录共用】
--   entertain_approval 是新表；meeting_notice 全库 0 行，补列不动任何存量。
-- ============================================================================

CREATE TABLE IF NOT EXISTS entertain_approval (
    app_no          TEXT PRIMARY KEY,
    bill_no         TEXT NOT NULL REFERENCES expense_bill(bill_no)
                         ON DELETE CASCADE,
    dept            TEXT,
    entertain_type  TEXT,
    reason          TEXT,
    guest_roster    TEXT,
    entertain_at    TIMESTAMPTZ,
    meal_at         TIMESTAMPTZ,
    meal_place      TEXT,
    meal_standard   NUMERIC(14, 2),
    meal_persons    INTEGER,
    work_meal       TEXT,
    car_arrange     TEXT,
    lodging_arrange TEXT,
    gift_arrange    TEXT,
    budget_total    NUMERIC(14, 2),
    extracted_from  TEXT,
    instance_id     TEXT NOT NULL
                         REFERENCES domain_instance(instance_id)
);

CREATE INDEX IF NOT EXISTS ix_ent_appr_bill ON entertain_approval(bill_no);
CREATE INDEX IF NOT EXISTS ix_ent_appr_inst ON entertain_approval(instance_id);

COMMENT ON TABLE entertain_approval IS
  '业务接待审批单（附件 A16）。一张报账单可挂多份。'
  'TR-接待-01「先审批后接待」、TR-通用-04「审批滞后」的唯一输入来源。';
COMMENT ON COLUMN entertain_approval.guest_roster IS
  '接待对象原文（外部人员姓名与职务黏连的自由文本）。'
  '**入库但永不进 fact**：脱敏闭集合切不出来，按 roster 策略处理，'
  '规则要的是人数不是名单。列名不含 name——脱敏按字段名选策略。';
COMMENT ON COLUMN entertain_approval.entertain_type IS
  '票面原文（商务接待/公务接待）。**不建枚举外键**：本实例没有接待类型码表，'
  '建 FK 等于宣称我们有一套权威取值。';
COMMENT ON COLUMN entertain_approval.entertain_at IS
  '票面「接待时间」。注意这是审批单上写的**计划**时间，不是实际发生时间——'
  '实测 FJPG-BX2606240151 接待时间 17:00、最后审批 17:03:45，'
  '按时间戳判「先接待后审批」会命中，而人工没驳它。'
  '所以 TR-接待-01 的口径是**按日期比，不按时间戳比**。';

ALTER TABLE meeting_notice ADD COLUMN IF NOT EXISTS notice_start DATE;
ALTER TABLE meeting_notice ADD COLUMN IF NOT EXISTS notice_end DATE;
ALTER TABLE meeting_notice ADD COLUMN IF NOT EXISTS notice_place TEXT;

COMMENT ON COLUMN meeting_notice.notice_start IS
  '通知上写的活动起始日期。TR-差旅-30 比「审批单出差日期范围 vs 通知活动起止」'
  '的第三方，一直缺的就是它。';
COMMENT ON COLUMN meeting_notice.notice_end IS '通知上写的活动结束日期。';
COMMENT ON COLUMN meeting_notice.notice_place IS
  '通知上写的活动地点。和 trip_application.place 比得出「申报出差地是否'
  '与通知一致」。';
