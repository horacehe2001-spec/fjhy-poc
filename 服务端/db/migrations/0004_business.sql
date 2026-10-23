-- ============================================================================
-- 0004 · 业务表（差旅报销单域）
-- ----------------------------------------------------------------------------
-- 每一列后面的 `-- Cxxxx` 是它对应的本体概念 ID。**这不是装饰**：
-- tests/test_schema_contract.py 会解析这些标记，断言本体的 119 个概念
-- 一个不漏地落到了某一列上。漂移一个就红。
--
-- 【取不到的字段为什么还要建列】
--   需OA同步（G-04）、需票据OCR、待业务侧明确的字段，眼下全是 NULL。
--   但列要先在：
--     1) 规则引用的是概念，概念要有落点，否则规则连「为什么跑不了」都说不清；
--     2) OA 一打通就是灌数据，不是改表结构；
--     3) ontology_concept.availability 记着每一列现在取不取得到，
--        启动时契约校验据此决定规则进不进执行集——这件事要有列可查。
--   **列存在 ≠ 数据存在。** 这两者的区别就写在 availability 里。
--
-- 【枚举列一律外键到 enum_value_current】
--   多一个表单上不存在的取值，规则就会去判一个永远为假的条件。
--   靠外键在库这一层把它挡住，而不是只靠应用层校验——
--   导入历史数据那条路是绕过应用层的。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- E09 核算主体
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org (
    org_id          TEXT PRIMARY KEY,
    name            TEXT NOT NULL,                                       -- C0901 单位名称
    direct_managed  TEXT,                                                -- C0902 直管企业   enum EN13（待业务侧明确 G-01）
    industry        TEXT,                                                -- C0903 所属行业   enum EN14
    payer_subject   TEXT,                                                -- C0904 付款主体（样本缺列，TR-D1-01 靠它）
    city            TEXT,                                                -- 办公地所在市，住宿费标准按市定额
    is_internal     BOOLEAN NOT NULL DEFAULT TRUE,                       -- 是否省港集团内部单位，TR-D9-05 的豁免面
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 枚举列的外键必须是**双列**：enum_value_current 的主键是 (enum_id, value)，
    -- 单列引用在 PostgreSQL 里直接报列数不匹配。所以每个枚举列都配一个
    -- 常量生成列放 enum_id，再两列一起做外键。
    -- 外键默认 MATCH SIMPLE：value 为 NULL 时约束自动满足，
    -- 这正是我们要的——取不到的字段留 NULL，不该被约束挡住。
    direct_managed_enum TEXT GENERATED ALWAYS AS ('EN13') STORED,
    industry_enum       TEXT GENERATED ALWAYS AS ('EN14') STORED,
    CONSTRAINT fk_org_dm  FOREIGN KEY (direct_managed_enum, direct_managed)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_org_ind FOREIGN KEY (industry_enum, industry)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

-- ─────────────────────────────────────────────────────────────────────────────
-- E08 出差人
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS employee (
    emp_no         TEXT PRIMARY KEY,                                     -- C0801 工号       【敏感】
    name           TEXT,                                                 -- C0802 姓名       【敏感】样本缺列
    dept           TEXT,                                                 -- C0803 所属部门（样本缺列）
    rank_level     TEXT,                                                 -- C0804 职级       enum EN12（待业务侧明确）
    resident_city  TEXT,                                                 -- C0805 常驻地（待业务侧明确 G-31，**阻塞**）
    org_id         TEXT REFERENCES org(org_id),                          -- C0806 所属单位
    has_official_car BOOLEAN,                                            -- C0807 是否配备公务用车（样本缺列）
    rank_enum      TEXT GENERATED ALWAYS AS ('EN12') STORED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_emp_rank FOREIGN KEY (rank_enum, rank_level)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON COLUMN employee.resident_city IS
  '常驻地。40 张样本里一列都没有（实测：单人 19 列 + 多人 12 列，全都没有）。'
  'TR-D9-05「出差地点须写到市级且非常驻地」靠它，而 TR-D4-02 住宿费标准匹配'
  '又靠 TR-D9-05——是一条链，断在最前面。缺口 G-31。';

-- ─────────────────────────────────────────────────────────────────────────────
-- E01 报账单
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expense_bill (
    bill_no            TEXT PRIMARY KEY,                                 -- C0101 单据编号
    doc_type           TEXT NOT NULL,                                    -- C0102 单据类型   enum EN01
    doc_date           TIMESTAMPTZ,                                      -- C0103 单据日期
    submitter_no       TEXT REFERENCES employee(emp_no),                 -- C0104 提单人
    claimant_no        TEXT REFERENCES employee(emp_no),                 -- C0105 报账人（多人导出缺此列 G-32）
    claim_dept         TEXT,                                             -- C0106 报账部门
    org_id             TEXT REFERENCES org(org_id),                      -- C0107 核算主体（多人导出缺此列 G-32）
    bill_status        TEXT,                                             -- C0108 单据状态   enum EN02（多人导出缺）
    bearing_dept       TEXT,                                             -- C0109 费用承担部门（多人导出缺）
    biz_type           TEXT NOT NULL,                                    -- C0110 业务类型   enum EN03
    amount             NUMERIC(18,2),                                    -- C0111 报账金额
    pay_amount         NUMERIC(18,2),                                    -- C0112 付款金额
    currency           TEXT,                                             -- C0113 币种
    tax                NUMERIC(18,2),                                    -- C0114 税金
    summary            TEXT,                                             -- C0115 摘要【自由文本，含真实姓名，M06 管】
    remark             TEXT,                                             -- C0116 补充说明【例外出口，12 条规则以它收口】
    attach_pages       INTEGER,                                          -- C0117 附件张数
    voucher_no         TEXT,                                             -- C0118 凭证编号
    booking_period     TEXT,                                             -- C0119 入账期间
    travel_settlement  NUMERIC(18,2),                                    -- C0120 商旅结算
    writeoff_amount    NUMERIC(18,2),                                    -- C0121 核销金额
    payable_amount     NUMERIC(18,2),                                    -- C0122 挂账及付款金额
    payee              TEXT,                                             -- C0123 收款方   【敏感】
    payee_account      TEXT,                                             -- C0124 收款账号 【敏感】
    payee_bank         TEXT,                                             -- C0125 收款银行 【敏感】
    pay_method         TEXT,                                             -- C0126 支付方式
    pay_purpose        TEXT,                                             -- C0127 付款用途【自由文本】
    is_multi_person    BOOLEAN,                                          -- C0128 是否多人（样本缺列，可由 doc_type 推）
    is_demo            BOOLEAN NOT NULL DEFAULT FALSE,                   -- 演示数据标记，见下方注释
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    doc_type_enum      TEXT GENERATED ALWAYS AS ('EN01') STORED,
    biz_type_enum      TEXT GENERATED ALWAYS AS ('EN03') STORED,
    status_enum        TEXT GENERATED ALWAYS AS ('EN02') STORED,
    CONSTRAINT fk_bill_doctype FOREIGN KEY (doc_type_enum, doc_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_bill_biztype FOREIGN KEY (biz_type_enum, biz_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_bill_status  FOREIGN KEY (status_enum, bill_status)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON COLUMN expense_bill.is_demo IS
  '演示数据标记。全局纪律第 12 条：在别人的环境里跑真实链路会产生真实数据，'
  '混进对方的演示环境会造成困惑，所以要用一眼可辨的标记命名、事后好清理。'
  '演示单据的 bill_no 一律带 DEMO 段（如 FJHY-DEMO-BX0001），并且这一列为 TRUE。'
  '审核报告上会显著标注【演示数据】。';

COMMENT ON COLUMN expense_bill.remark IS
  '补充说明是**例外出口**：TR-D6-02/03/05/06、TR-D9-01 等 12 条规则都以'
  '「补充说明非空」收口。所以它既不能忽略（忽略会产生大量误报），'
  '也不能只判非空就放过（那等于谁写一句话都能过）——'
  '判「说明是否成立」是软规则，交 AI 审核节点，且必须人工复核。';

CREATE INDEX IF NOT EXISTS ix_bill_org   ON expense_bill (org_id);
CREATE INDEX IF NOT EXISTS ix_bill_biz   ON expense_bill (biz_type);
CREATE INDEX IF NOT EXISTS ix_bill_date  ON expense_bill (doc_date DESC);
CREATE INDEX IF NOT EXISTS ix_bill_demo  ON expense_bill (is_demo) WHERE is_demo;

-- ─────────────────────────────────────────────────────────────────────────────
-- E02 费用明细
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expense_detail (
    id             BIGSERIAL PRIMARY KEY,
    bill_no        TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,  -- C0201 单据号
    expense_item   TEXT NOT NULL,                                        -- C0202 费用小类   enum EN04
    amount         NUMERIC(18,2) NOT NULL,                               -- C0203 费用明细金额（22 条规则引用，最热的一列）
    alloc_dept     TEXT,                                                 -- C0204 分摊部门（仅多人导出有）
    line_no        INTEGER,
    item_enum      TEXT GENERATED ALWAYS AS ('EN04') STORED,
    CONSTRAINT fk_detail_item FOREIGN KEY (item_enum, expense_item)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS ix_detail_bill ON expense_detail (bill_no);
CREATE INDEX IF NOT EXISTS ix_detail_item ON expense_detail (expense_item);

COMMENT ON TABLE expense_detail IS
  '**加载这张表时必须校验引用完整性。** 实测样本里有一张孤儿：'
  'FJPG-BX2609041860 有 8 条费用明细，但「2多人差旅报账单」主表里没有它。'
  '加载器遇到这种情况必须报错退出：静默丢弃会让这 8 行费用从此不被任何规则看到；'
  '凭明细凭空造一张主表，会让后面所有按核算主体/业务类型判的规则拿到编出来的值。'
  '缺口 G-30。';

-- ─────────────────────────────────────────────────────────────────────────────
-- E03 出差审批单 / E04 出差日历 —— 整块都是 G-04（OA 未同步）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trip_application (
    app_no          TEXT PRIMARY KEY,
    bill_no         TEXT REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    traveler_list   JSONB,                                               -- C0301 出差人名单
    start_date      DATE,                                                -- C0302 出差起始日期
    end_date        DATE,                                                -- C0303 出差结束日期
    place           TEXT,                                                -- C0304 出差地点
    days            INTEGER,                                             -- C0305 出差天数【可推导：伙食补助费 ÷ 日标准】
    person_count    INTEGER,                                             -- C0306 出差人数
    approve_date    DATE,                                                -- C0307 审批日期
    backfill_date   DATE,                                                -- C0308 回填日期
    meal_situation  TEXT,                                                -- C0309 用餐情况   enum EN05
    car_situation   TEXT,                                                -- C0310 用车情况   enum EN06
    reason          TEXT,                                                -- C0311 出差事由
    approve_form    TEXT,                                                -- C0312 审批形式   enum EN07
    synced_from_oa  BOOLEAN NOT NULL DEFAULT FALSE,
    meal_enum       TEXT GENERATED ALWAYS AS ('EN05') STORED,
    car_enum        TEXT GENERATED ALWAYS AS ('EN06') STORED,
    form_enum       TEXT GENERATED ALWAYS AS ('EN07') STORED,
    CONSTRAINT fk_trip_meal FOREIGN KEY (meal_enum, meal_situation)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_trip_car  FOREIGN KEY (car_enum, car_situation)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_trip_form FOREIGN KEY (form_enum, approve_form)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON COLUMN trip_application.synced_from_oa IS
  '**这一列是 G-04 的可观测形式。** 29 条规则表第 20~23 条的评估列原文是'
  '「OA**将**出差时间和出差地点同步给共享系统，可以实现」——是未来时。'
  '13 条规则的可执行性全系于此。FALSE 表示这张审批单的字段是空壳，'
  '引擎看到 FALSE 就不能把依赖 E03/E04 的规则放进执行集。';

CREATE TABLE IF NOT EXISTS trip_calendar (
    id          BIGSERIAL PRIMARY KEY,
    app_no      TEXT NOT NULL REFERENCES trip_application(app_no) ON DELETE CASCADE,
    cal_date    DATE NOT NULL,                                           -- C0401 日期
    place       TEXT,                                                    -- C0402 地点
    has_meal    BOOLEAN,                                                 -- C0403 是否用餐
    has_car     BOOLEAN,                                                 -- C0404 是否用车
    meal_slot   TEXT,                                                    -- C0405 用餐栏次   enum EN08
    slot_enum   TEXT GENERATED ALWAYS AS ('EN08') STORED,
    CONSTRAINT fk_cal_slot FOREIGN KEY (slot_enum, meal_slot)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

-- ─────────────────────────────────────────────────────────────────────────────
-- E05 发票 / E06 住宿费发票 / E07 交通费凭证 —— 需票据 OCR，样本一个票据文件都没给
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoice (
    invoice_id     TEXT PRIMARY KEY,
    bill_no        TEXT REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    inv_type       TEXT,                                                 -- C0501 发票类型   enum EN09
    inv_no         TEXT,                                                 -- C0502 发票号码
    issue_date     DATE,                                                 -- C0503 开票日期
    issuer         TEXT,                                                 -- C0504 开票单位（TR-D3-01 敏感词作用在这）
    title          TEXT,                                                 -- C0505 发票抬头（TR-D1-02 比核算主体）
    total_amount   NUMERIC(18,2),                                        -- C0506 价税合计
    tax_amount     NUMERIC(18,2),                                        -- C0507 税额
    tax_rate       NUMERIC(6,4),                                         -- C0508 税率
    detail_text    TEXT,                                                 -- C0509 发票明细（TR-D3-02 敏感词作用在这）
    remark_col     TEXT,                                                 -- C0510 备注栏
    void_status    TEXT,                                                 -- C0511 红冲状态   enum EN10
    is_eticket     BOOLEAN,                                              -- C0512 是否数电票
    ocr_confidence NUMERIC(5,4),                                         -- 读图置信度，低于门限转人工
    ocr_source     TEXT,                                                 -- 原件路径或影像 ID
    type_enum      TEXT GENERATED ALWAYS AS ('EN09') STORED,
    void_enum      TEXT GENERATED ALWAYS AS ('EN10') STORED,
    CONSTRAINT fk_inv_type FOREIGN KEY (type_enum, inv_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_inv_void FOREIGN KEY (void_enum, void_status)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS ix_inv_bill ON invoice (bill_no);
-- 发票号查重用：同一张发票在历史单据里报过就是重复报销
CREATE INDEX IF NOT EXISTS ix_inv_no   ON invoice (inv_no) WHERE inv_no IS NOT NULL;

COMMENT ON COLUMN invoice.inv_no IS
  '全电发票号码 20 位。**必须做确定性位数校验，不要指望模型自己发现自己读错。**'
  '参考项目的实测：读图模型的误差全部是「少读一个 0」，连续 0 ≥ 9 个时开始出错；'
  '放大二次精读与首读错在同一处，纠不过来。位数不符就转人工核验 + 税务查验。';

CREATE TABLE IF NOT EXISTS hotel_invoice (
    invoice_id     TEXT PRIMARY KEY REFERENCES invoice(invoice_id) ON DELETE CASCADE,
    check_in       DATE,                                                 -- C0601 入住时间
    check_out      DATE,                                                 -- C0602 离店时间
    guest_name     TEXT,                                                 -- C0603 入住人姓名【敏感】
    nights         INTEGER,                                              -- C0604 住宿天数
    rooms          INTEGER,                                              -- C0605 住宿间数
    is_daybreak    BOOLEAN                                               -- C0606 是否午休房
);

CREATE TABLE IF NOT EXISTS transport_voucher (
    voucher_id      TEXT PRIMARY KEY,
    bill_no         TEXT REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    invoice_id      TEXT REFERENCES invoice(invoice_id) ON DELETE SET NULL,
    vehicle         TEXT,                                                -- C0701 交通工具   enum EN11
    seat_class      TEXT,                                                -- C0702 坐席等级（TR-D4-01 靠它，但等级标准表无法录入 G-03）
    from_place      TEXT,                                                -- C0703 出发地
    to_place        TEXT,                                                -- C0704 到达地
    ride_date       DATE,                                                -- C0705 乘车日期
    has_round_trip  BOOLEAN,                                             -- C0706 是否含往返
    refund_fee      NUMERIC(18,2),                                       -- C0707 退票手续费
    bus_has_identity BOOLEAN,                                            -- C0708 大巴票备注身份信息
    vehicle_enum    TEXT GENERATED ALWAYS AS ('EN11') STORED,
    CONSTRAINT fk_tv_vehicle FOREIGN KEY (vehicle_enum, vehicle)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON COLUMN transport_voucher.seat_class IS
  '坐席等级。TR-D4-01「交通坐席等级符合制度标准」**不编译**：'
  '细则附件2 的交通工具等级标准表是扫描件，OCR 出来表格线断裂、文字重影，'
  '人眼读图也失败（1 倍与 2 倍图都试过），全表无法录入。缺口 G-03。'
  '这一列先留着——标准表一旦拿到可录入的版本，规则不用改。';

-- ─────────────────────────────────────────────────────────────────────────────
-- E12 审批记录
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approval_record (
    id            BIGSERIAL PRIMARY KEY,
    bill_no       TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    seq           INTEGER,                                               -- C1201 序号
    node          TEXT,                                                  -- C1202 审批节点   enum EN16
    approver      TEXT,                                                  -- C1203 审批人【敏感】格式「工号/姓名」，AI 节点为 aiAudit/null
    opinion       TEXT,                                                  -- C1204 审批意见【自由文本，退回理由在这，是 F 阶段最重要的输入】
    approved_at   TIMESTAMPTZ,                                           -- C1205 审批时间
    node_enum     TEXT GENERATED ALWAYS AS ('EN16') STORED,
    CONSTRAINT fk_appr_node FOREIGN KEY (node_enum, node)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS ix_appr_bill ON approval_record (bill_no, seq);

COMMENT ON COLUMN approval_record.opinion IS
  '人工的退回理由。**这是 feedback_log 最重要的 source**：'
  'AI 判了什么、人最后怎么判、理由是什么，三者放在一起才能算出误报/漏报。'
  '实测 50 页审批记录里有真实退回理由可回流。'
  '注意它是自由文本，含真实姓名，出口前必须过 M06。';

-- ─────────────────────────────────────────────────────────────────────────────
-- E13 附件 —— 实测 40 张样本里这张表的来源数据 100% 为空（G-29，阻塞）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS attachment (
    id            BIGSERIAL PRIMARY KEY,
    bill_no       TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    attach_type   TEXT,                                                  -- C1301 附件类型   enum EN17
    file_name     TEXT,                                                  -- C1302 文件名
    is_required   BOOLEAN,                                               -- C1303 是否必要（来自必要附件决策表，不是上传时填的）
    pages         INTEGER,                                               -- C1304 页数
    stored_path   TEXT,
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    type_enum     TEXT GENERATED ALWAYS AS ('EN17') STORED,
    CONSTRAINT fk_att_type FOREIGN KEY (type_enum, attach_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX IF NOT EXISTS ix_att_bill ON attachment (bill_no, attach_type);

COMMENT ON TABLE attachment IS
  '**实测：这张表的源数据在 40 张样本里 100% 为空。** 单人导出的「发票文件」'
  '「事前附件」两列 33 行全空、「1报销附件」表 33 行全空、多人「报销附件」表 7 行全空'
  '——三条路都是空的。TR-D6-01（必要附件完整性）是禁止级，演示中开启会对'
  '40/40 张单报「必要附件缺失」，误报率 100%，所以它的 demo_executable=false。'
  '29 行决策表本身已就绪，数据一到就能开。缺口 G-29。';

-- ─────────────────────────────────────────────────────────────────────────────
-- E14 事权审批单 / E15 会议培训通知 / E16 费用分摊
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS authority_approval (
    id            BIGSERIAL PRIMARY KEY,
    bill_no       TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    matter_type   TEXT,                                                  -- C1401 事项类别   enum EN18（船舶3000/机关1000）
    approved_amt  NUMERIC(18,2),                                         -- C1402 事权审批金额
    attached      BOOLEAN,                                               -- C1403 是否已附
    matter_enum   TEXT GENERATED ALWAYS AS ('EN18') STORED,
    CONSTRAINT fk_auth_matter FOREIGN KEY (matter_enum, matter_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON TABLE authority_approval IS
  '事权审批单。TR-D6-04 **不编译**，因为适用范围未定（G-12）：'
  '条文主语是「海运集团」，而 40 张样本的核算主体全部是「财智分公司」。'
  '若适用，28 张金额 ≥1000 的单全部缺附件；若不适用，一条都不该报。'
  '28 条误报与 28 条漏报之间，只能由业务侧定。';

CREATE TABLE IF NOT EXISTS meeting_notice (
    id              BIGSERIAL PRIMARY KEY,
    bill_no         TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    unified_board   BOOLEAN,                                             -- C1501 是否统一安排食宿
    meal_provided   TEXT,                                                -- C1502 是否提供用餐 enum EN19
    shuttle         BOOLEAN,                                             -- C1503 是否提供接送车
    self_funded     BOOLEAN,                                             -- C1504 是否费用自理
    meal_enum       TEXT GENERATED ALWAYS AS ('EN19') STORED,
    CONSTRAINT fk_mn_meal FOREIGN KEY (meal_enum, meal_provided)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS expense_allocation (
    id            BIGSERIAL PRIMARY KEY,
    bill_no       TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    alloc_target  TEXT,                                                  -- C1601 分摊对象
    alloc_amount  NUMERIC(18,2)                                          -- C1602 分摊金额
);

-- ─────────────────────────────────────────────────────────────────────────────
-- E10 标准表 / E11 预算 / E17 直管企业特殊规则
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS standard_limit (
    id             BIGSERIAL PRIMARY KEY,
    std_type       TEXT NOT NULL,                                        -- C1001 标准类型   enum EN15
    rank_level     TEXT,                                                 -- C1002 适用职级
    region         TEXT,                                                 -- C1003 适用地区
    limit_amount   NUMERIC(18,2),                                        -- C1004 限额金额【OCR 来源，不得直接当硬阈值】
    unit           TEXT,                                                 -- C1005 计量单位（元/人·天 等）
    effective      DATERANGE,                                            -- C1006 生效期间
    source_clause  TEXT,                                                 -- C1007 出处条款
    confidence     TEXT NOT NULL,                                        -- 人眼读图 | OCR | 原生文本
    cross_checked  BOOLEAN NOT NULL DEFAULT FALSE,                       -- 是否被真实单据交叉印证过
    std_enum       TEXT GENERATED ALWAYS AS ('EN15') STORED,
    CONSTRAINT fk_std_type FOREIGN KEY (std_enum, std_type)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_std_conf CHECK (confidence IN ('人眼读图','OCR','原生文本'))
);

COMMENT ON COLUMN standard_limit.confidence IS
  '三档可信度，决定这条限额能不能进硬规则：'
  '原生文本——可用；人眼读图——可用（附件1 住宿费标准表就是渲成 PNG 人眼读的，'
  'OCR 把城市↔职级↔金额的对应关系全弄乱了）；OCR——**不得直接进硬规则阈值**，'
  '除非 cross_checked=TRUE。'
  '已交叉印证的两组：伙食补助 100 元/天与样本 100/200/300/400/600/900 全部吻合；'
  '市内交通费 80 元/天 + 40 元档解释了样本里唯一的异常值 120。';

CREATE TABLE IF NOT EXISTS budget (
    id             BIGSERIAL PRIMARY KEY,
    org_id         TEXT REFERENCES org(org_id),
    subject_code   TEXT,                                                 -- C1101 预算科目
    budget_amount  NUMERIC(18,2),                                        -- C1102 预算金额
    used_amount    NUMERIC(18,2),                                        -- C1103 已用金额
    adjust_attach  BIGINT REFERENCES attachment(id),                     -- C1104 预算调整审批材料
    period         TEXT
);

CREATE TABLE IF NOT EXISTS org_special_rule (
    id             BIGSERIAL PRIMARY KEY,
    direct_managed TEXT,                                                 -- C1701 直管企业   enum EN13
    rule_category  TEXT,                                                 -- C1702 规则类别   enum EN20
    rule_text      TEXT,                                                 -- C1703 特殊规则正文
    overrides      JSONB,                                                -- C1704 覆盖的通用规则（规则 ID 数组）
    dm_enum        TEXT GENERATED ALWAYS AS ('EN13') STORED,
    cat_enum       TEXT GENERATED ALWAYS AS ('EN20') STORED,
    CONSTRAINT fk_osr_dm  FOREIGN KEY (dm_enum, direct_managed)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_osr_cat FOREIGN KEY (cat_enum, rule_category)
        REFERENCES enum_value_current(enum_id, value) DEFERRABLE INITIALLY DEFERRED
);

COMMENT ON COLUMN org_special_rule.overrides IS
  '**特殊规则与通用规则是覆盖关系，不是并列关系。** 细则附件2 与规则清单里没有'
  '「直管企业特殊规则优先于通用规则」的对应表，这是缺口 G-01：'
  '「漳州港务古雷港驻点人员在古雷港期间出差」到底按哪条判，'
  '不能靠猜，否则会对整个单位误报。所以本列为空时，特殊规则只提示、不覆盖。';
