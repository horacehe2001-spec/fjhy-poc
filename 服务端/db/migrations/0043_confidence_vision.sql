-- ============================================================================
-- 0043 · 可信度白名单补上「读图」
-- ----------------------------------------------------------------------------
-- 0042 把多模态读图升成了与原生文本、本机 OCR 平级的**第三条取文路径**，
-- 于是 doclib_clause.text_source 多了一个取值「读图」（实测 349 条）。
-- 但几张表的 confidence 还是老白名单，于是：
--
--   CheckViolation: doclib_rule_source violates "ck_dls_conf"
--
-- 822港航 跑「一键搭建 ①→⑦」时第 ⑦ 步整个失败——条款都在、文本也都在，
-- 只是写不进去。**这是 0042 漏改的，不是新需求。**
--
-- 【「读图」和「人眼读图」是两回事，不许合并成一档】
--   人眼读图 = 人把扫描件看了一遍，自己抄下来的。项目可信度里**最高的那一档**
--              （和原生文本并列），住宿费标准表那 15 行费率就是这么来的。
--   读图     = 多模态模型读的。它同样会把看不清的格子编出来，而且编得通顺，
--              唯一的警报是提示词里那个「看不清写 ?」和 doc_page_vision.unreadable。
--   把模型读的算成人眼读的，等于把这条链上唯一的人工复核痕迹抹掉。
--
-- 【为什么三张表加、policy_clause 不加】
--   doclib_rule_source / rank_tier_map / standard_limit 都是**活的**：
--     · doclib_rule_source.confidence 直接取自 doclib_clause.text_source，现在就在报错；
--     · rank_tier_map / standard_limit 是下一步要做的事——822港航 的职级档位与
--       限额要从它自己那份《住宿标准》xlsx 建，而那份表的正文正是读图读出来的。
--       现在不开，到时候又是同一个 CheckViolation。
--   policy_clause 是**确认过的死表**（全项目零写入，0040 的调查结论），
--   给死表加取值只会让人以为它还活着。
--
-- 用户 2026-10-30 已决定「VL 文本与原生文本同等对待」（见 0042 的决策记录），
-- 所以这里只是放行取值，不在库里另设可信度等级——等级那件事归业务代码管。
-- ============================================================================

ALTER TABLE doclib_rule_source DROP CONSTRAINT IF EXISTS ck_dls_conf;
ALTER TABLE doclib_rule_source ADD CONSTRAINT ck_dls_conf
    CHECK (confidence IN ('原生文本', '读图', 'OCR'));

ALTER TABLE rank_tier_map DROP CONSTRAINT IF EXISTS ck_rtm_conf;
ALTER TABLE rank_tier_map ADD CONSTRAINT ck_rtm_conf
    CHECK (confidence IN ('人眼读图', '原生文本', '读图', 'OCR'));

ALTER TABLE standard_limit DROP CONSTRAINT IF EXISTS ck_std_conf;
ALTER TABLE standard_limit ADD CONSTRAINT ck_std_conf
    CHECK (confidence IN ('人眼读图', '原生文本', '读图', 'OCR'));

COMMENT ON CONSTRAINT ck_dls_conf ON doclib_rule_source IS
  '正文来自哪条取文路径：原生文本（PDF/Office 自带文字层）/ 读图（多模态模型）'
  '/ OCR（本机 tesseract）。**三者必须一路区分到规则草稿上**——'
  '页面上三种来源的数字长得一模一样，不分开就没人知道哪个该回查原文。';

COMMENT ON CONSTRAINT ck_std_conf ON standard_limit IS
  '这条限额的数字是怎么拿到的。**「人眼读图」和「读图」不是一回事**：'
  '前者是人看着扫描件抄的（可信度最高档），后者是多模态模型读的——'
  '模型同样会把看不清的格子编出来，而且编得通顺。合并成一档，'
  '等于把这条链上唯一的人工复核痕迹抹掉。';
