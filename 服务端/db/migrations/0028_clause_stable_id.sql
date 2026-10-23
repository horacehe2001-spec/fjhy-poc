-- ============================================================================
-- 0028 · 条款的稳定身份：让付费买来的草稿活过一次重切
-- ----------------------------------------------------------------------------
-- 实测到的事故（台账还在 doclib_extract_task 里）：
--   10-18 21:06 AI草稿 → 7 条     10-18 21:28 AI草稿 → 7 条
--   10-19 16:39 条款切分 → **上面 14 条草稿全部消失**
--
-- 两件事凑成的：
--   ① run_split() 每份文件先 `DELETE FROM doclib_clause WHERE file_id=...`
--   ② doclib_draft.clause_id 是 ON DELETE CASCADE
-- 于是重切一次，之前所有草稿连同**采纳/驳回结论和驳回原因**一起静默清零。
-- 丢草稿是丢钱（一条 150 秒的调用），丢驳回原因更贵——
-- 那正是「下次别再抽这条」的唯一记录，没了就会反复抽同一条。
--
-- 根因不在 CASCADE，在**条款没有身份**：clause_id 以前是 uuid4，
-- 同一条「第十条」每次重切都换一个 id，所以连重新挂回去都做不到。
--
-- 这里把 id 改成内容定址：
--   dlc- + md5(file_id | 切分策略 | 条款号 | 同号第几次)[:12]
--
-- 为什么**不用 seq**：seq 是文件内序号，文件前面插一条，后面所有条款的 seq
-- 全体位移，等于全部换了身份——那就又回到了现在这个局面。
-- 为什么**要带「同号第几次」**：实扫有 3 处同一份文件里条款号重复
-- （20号文「第十六条」「第二十一条」各出现两次，解答六「第三条」两次），
-- 只用条款号会把两条不同的条款算成同一条，后一条覆盖前一条。
-- 为什么**切分策略进 id**：换了策略条款号口径就变了（「第十条」→「第5行」），
-- 那本来就该是另一条条款。
--
-- 迁移本身必须**无损**：先给子表外键补 ON UPDATE CASCADE，再改 id，
-- 这样现有 8 条草稿跟着一起改，一条都不丢。
-- ============================================================================

-- ① 子表外键补 ON UPDATE CASCADE（原来只有 ON DELETE CASCADE）
ALTER TABLE doclib_draft DROP CONSTRAINT IF EXISTS doclib_draft_clause_id_fkey;
ALTER TABLE doclib_draft
    ADD CONSTRAINT doclib_draft_clause_id_fkey FOREIGN KEY (clause_id)
        REFERENCES doclib_clause(clause_id) ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE doclib_rule_source DROP CONSTRAINT IF EXISTS doclib_rule_source_clause_id_fkey;
ALTER TABLE doclib_rule_source
    ADD CONSTRAINT doclib_rule_source_clause_id_fkey FOREIGN KEY (clause_id)
        REFERENCES doclib_clause(clause_id) ON DELETE CASCADE ON UPDATE CASCADE;

-- ② 把现有条款的 id 换成内容定址的值（草稿随外键一起跟过去）
WITH n AS (
    SELECT clause_id,
           'dlc-' || substr(md5(
               file_id || '|' || coalesce(split_strategy, '') || '|' || clause_no
                       || '|' || row_number() OVER (PARTITION BY file_id, split_strategy,
                                                                 clause_no
                                                    ORDER BY seq)::text), 1, 12) AS nid
      FROM doclib_clause
)
UPDATE doclib_clause c SET clause_id = n.nid
  FROM n WHERE n.clause_id = c.clause_id AND n.nid <> c.clause_id;

-- ③ 同一份文件里不许出现两条同身份的条款（UPSERT 的落点）
CREATE UNIQUE INDEX IF NOT EXISTS ux_doclib_clause_ident
    ON doclib_clause (file_id, split_strategy, clause_no, clause_id);

-- ④ 抽草稿是第几次。失败可以重试，重试要能看出是第几回。
--    和 doc_page_vision.attempt 同一套口径：**不覆盖，加一行**。
ALTER TABLE doclib_draft ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;

WITH a AS (
    SELECT draft_id, row_number() OVER (PARTITION BY clause_id ORDER BY created_at) AS k
      FROM doclib_draft
)
UPDATE doclib_draft d SET attempt = a.k FROM a WHERE a.draft_id = d.draft_id;

COMMENT ON COLUMN doclib_clause.clause_id IS
  '内容定址：dlc- + md5(file_id|切分策略|条款号|同号第几次)[:12]。'
  '**不是 uuid，也不含 seq。** 文件前面插一条就会让所有 seq 位移，'
  '而 id 一变，挂在上面的草稿（一条 150 秒的付费调用）和审定结论就全断了——'
  '实测丢过 14 条草稿。带「同号第几次」是因为实扫有 3 处同份文件条款号重复。';

COMMENT ON COLUMN doclib_draft.attempt IS
  '这条条款抽到第几次。**失败的允许重试，成功的不再重抽**——'
  '取数时按「有没有成功过」判，不按「有没有抽过」判：'
  '按后者判，上次超时的那 7 条就被判了死刑，再也不会被抽；'
  '不判的话，每点一次都把同样几条重新买一遍。';

COMMENT ON TABLE doclib_draft IS
  '抽取产物一律是**草稿**，不是规则。采纳之后才写 rule 表，且必须记 reviewer。'
  '理由很直接：规则是制度的投影，判错一条就是对一批单据误报或漏报。'
  '模型抽出来的八要素里「判定逻辑」和「例外」最容易丢细节——'
  '实测市内交通费那条，制度原文有 40 元这一档（仅往返中转站），'
  '漏掉就会把样本里 120 元的正常单判成超标准。'
  '**草稿跨重切存活**（0028 起）：clause_id 内容定址 + 外键 ON UPDATE CASCADE。';
