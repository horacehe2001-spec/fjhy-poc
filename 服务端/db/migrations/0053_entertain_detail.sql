-- ============================================================================
-- 0053 · 业务招待明细（entertain_detail）
-- ----------------------------------------------------------------------------
-- 【为什么跳到 0053】
--   **这台机器上的库是两份源目录共用的**，0040–0052 这段编号已经被另一棵树
--   （福建港口集团智能审核POC-柳航版，远端 JoJoH72/fjhy-poc）占了：
--       0040_iam / 0041_role_home_first_menu / 0042_role_sort_no /
--       0043_mywork_claim / 0044_ia_rework / 0045_sys_op_log /
--       0046_manual_review_fix_unsigned / 0047_manual_review_category /
--       0048_feedback_resolve / 0049_chat_draft / 0050_attachment_judge /
--       0051_rule_revise / 0052_draft_dup
--   它们建的 sys_user / sys_role / sys_session / sys_op_log / sys_user_org
--   就在同一个库里。所以本工程 `db/migrate.py --status` 会有 13 条
--   「库里有记录、文件不存在」——**那是正常的，不是漏文件**，
--   不要去把对方的迁移文件复制过来：复制过来就是在本仓库里产生一份影子副本，
--   两边各改一处就漂移，而漂移在 code review 里极难看出来。
--   新增编号一律从 0053 往后走，避开那一段。
--
-- 【为什么要这张表】
--   822港航 的招待费报账单导出里有一个「招待明细区」工作表（实测 18 行 / 9 张单），
--   给了招待地点、发生日期、招待人数、陪同人数、领用酒水金额、招待对象名单。
--   库里没有能装这些的地方：expense_detail 只有费用小类/金额/分摊部门。
--   结果是 15 条规则判不了（rule_feasibility 实测：「业务招待清单」涉及 14 条、
--   「招待明细日期」涉及 1 条），而**原始数据一直在手上**——
--   这不是客户的缺口，是我方没有建表。
--
--   客户自己那 29 条人工驳回意见里有 4 条就是在查这些，原话例如
--   「简餐请按照接待日期，在招待明细区分开进行填列」。
--
-- 【一张单多行，所以是子表不是列】
--   实测 9 张招待单对应 18 行明细，一次接待一行（不同日期/地点/对象分开填）。
--   往 expense_bill 上加列会把多行压成一行，丢掉的正是规则要比的东西
--   （同一张单里哪一次接待超了标准）。
--
-- 【三列是系统码，没有码表 —— 故意按原样存】
--   招待类型  OTHER_ENTERTAIN / BUSINESS_ENTERTAIN
--   招待方式  EAT_WORK / EAT_OUT
--   陪同级别  LEVEL_01 / LEVEL_05
--   导出里给的就是这种码，**客户没给码表**。不进枚举（enum_value_current
--   要的是「业务上看得懂的取值」，塞 LEVEL_05 进去等于把码当成了业务值），
--   也**不自己翻译**——把 LEVEL_05 猜成「副总级」，整类陪同级别判定全错，
--   而且错得看起来很合理。原样存着，码表列进客户清单。
--
-- 【招待对象名单里是外部人员的真实姓名】
--   实测取值形如「福建省华侨实业集团有限责任公司总经理朱宗秋、总经理助理詹志强、
--   创新事业部经理俞婷」——姓名和职务**黏在一起、没有分隔符**，
--   脱敏闭集合按 2~3 字汉字 token 取，这种写法一个都切不出来；
--   而这些人是**外部人员**，不在 employee 表里，闭集合按构造也取不到他们。
--   所以这一列在 app/mask.py 里单独一条 roster 策略：整列不出明文，
--   对外只给「名单 N 人（已整列脱敏）」。
--   列名叫 guest_list 而不是 guest_names：带 name 的字段名会被按姓名策略
--   脱敏（项目里为这个踩过四次），而这一列要的是比姓名更严的处理。
--
-- 【instance_id 由触发器从报账单继承】
--   沿用 0046 的做法：派生属性不是录入项，让每个写入点自己填等于
--   给每个写入点埋一个忘记的机会。
-- ============================================================================

CREATE TABLE IF NOT EXISTS entertain_detail (
    id               BIGSERIAL PRIMARY KEY,
    bill_no          TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    line_no          INTEGER,
    amount           NUMERIC(14,2),
    entertain_type   TEXT,
    entertain_way    TEXT,
    companion_level  TEXT,
    place            TEXT,
    happen_date      DATE,
    guest_count      INTEGER,
    companion_count  INTEGER,
    liquor_amount    NUMERIC(14,2),
    guest_list       TEXT,
    instance_id      TEXT NOT NULL REFERENCES domain_instance(instance_id)
                          ON DELETE RESTRICT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_entertain_detail_bill ON entertain_detail(bill_no);
CREATE INDEX IF NOT EXISTS ix_entertain_detail_inst ON entertain_detail(instance_id);

COMMENT ON TABLE entertain_detail IS
  '业务招待明细。源：招待费报账单导出的「招待明细区」工作表。'
  '一次接待一行（不同日期/地点/对象分开填），所以是子表不是 expense_bill 的列。';
COMMENT ON COLUMN entertain_detail.entertain_type IS
  '招待类型。导出里是系统码（OTHER_ENTERTAIN/BUSINESS_ENTERTAIN），'
  '客户没给码表，**原样存，不翻译**。';
COMMENT ON COLUMN entertain_detail.entertain_way IS
  '招待方式。系统码（EAT_WORK/EAT_OUT），同上。';
COMMENT ON COLUMN entertain_detail.companion_level IS
  '陪同级别。系统码（LEVEL_01…LEVEL_05），同上。'
  '猜错一个档位，整类陪同级别判定全错，而且错得看起来很合理。';
COMMENT ON COLUMN entertain_detail.guest_list IS
  '招待对象名单。**里面是外部人员的真实姓名，和职务黏在一起没有分隔符**，'
  '脱敏闭集合取不到。这一列整列不出明文，见 app/mask.py 的 roster 策略。';
COMMENT ON COLUMN entertain_detail.happen_date IS
  '发生日期。这是本实例目前**唯一一个业务发生日期**——'
  'expense_bill.doc_date 全空、只有到月的入账期间，而客户 29 条驳回意见里'
  '有 8 条是「入账期间填错」。';

DROP TRIGGER IF EXISTS trg_entertain_detail_instance ON entertain_detail;
CREATE TRIGGER trg_entertain_detail_instance
    BEFORE INSERT OR UPDATE ON entertain_detail
    FOR EACH ROW EXECUTE FUNCTION fjhy_inherit_instance_from_bill();
