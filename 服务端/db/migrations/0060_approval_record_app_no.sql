-- ============================================================================
-- 0060 · approval_record 补 app_no / extracted_from（哪份审批单、怎么来的）
-- ----------------------------------------------------------------------------
-- 【编号】0059 之后接着走。0040–0052 属另一棵源目录（见 0053 文件头）。
--
-- 【为什么要这两列：读图那一批没有办法「重跑不重复」】
--   0059 给 approval_record 加了 `flow`，解决的是「这条流水属于哪条流程」。
--   现在要把 26 份 JPG 业务接待审批单读图入库，又撞上同一类问题的下一层：
--   **一张报账单可以挂多份业务接待审批单**（实测 FJPG-BX2603230510 挂了
--   9 份、FJPG-BX2609201921 挂了 7 份），每份自己有一套审批节点。
--   approval_record 的主键是 `id`（自增），除此之外没有任何能标识
--   「这条流水是哪份审批单上的」的列。后果是：
--     · 重跑读图 → 同一份审批单的流水再插一遍，`max(approved_at)` 不变但
--       行数翻倍，报出去的「审批流水 N 条」是假的
--     · 想按「这份审批单」先删后插 → 删不动，只能按 bill_no 删，
--       而那会把同一张单上**另外 8 份**审批单的流水一起删掉
--   所以补 `app_no`：取值就是 entertain_approval.app_no，
--   删除/回写的作用域从「这张报账单」收紧到「这份审批单」。
--
--   **不建 FK 指向 entertain_approval(app_no)**：报账流程的 388 行
--   （fjhy，flow IS NULL）本来就没有审批单，app_no 永远是 NULL；
--   建了 FK 并不能表达「NULL 合法但非 NULL 必须存在」之外的任何约束，
--   而它会让「先写流水后写审批单」这种顺序无谓地报错。
--
-- 【extracted_from：文字层那一批和读图那一批要能互不覆盖】
--   app/demo/load_822gh_entertain_approval.py 是「只装文字层」的定点程序，
--   它开头做的是 `DELETE FROM approval_record WHERE instance_id=%s`——
--   **读图入库之后再跑一次它，会把读图的成果整批删掉**。
--   有了这一列，它的删除作用域收成
--   `WHERE instance_id=%s AND extracted_from='审批单文字层'`，
--   两批互不相干。
--   取值：'审批单文字层' / '审批单读图' / NULL（报账流程的存量，不动）。
--
-- 【两列都是 nullable，存量 396 行一行不动】
--   fjhy 388 行（报账流程）、822gh 8 行（文字层解出的业务接待审批）。
--   822gh 那 8 行由文字层程序重跑时自己填上 extracted_from，
--   这里不 UPDATE——迁移脚本改业务数据是另一回事，不混在一起。
-- ============================================================================

ALTER TABLE approval_record ADD COLUMN IF NOT EXISTS app_no TEXT;
ALTER TABLE approval_record ADD COLUMN IF NOT EXISTS extracted_from TEXT;

CREATE INDEX IF NOT EXISTS ix_appr_app_no ON approval_record(app_no);

COMMENT ON COLUMN approval_record.app_no IS
  '这条流水属于哪份业务接待审批单（= entertain_approval.app_no）。'
  '报账流程的流水没有审批单，这一列是 NULL。'
  '**不建 FK**：NULL 合法、非 NULL 必须存在这个约束 FK 表达不了，'
  '而 FK 会让「先写流水后写审批单」的顺序无谓报错。'
  '作用：一张报账单挂多份审批单时（实测最多 9 份），'
  '读图回写能按「这份审批单」先删后插，而不是按整张报账单删。';
COMMENT ON COLUMN approval_record.extracted_from IS
  '这条流水怎么来的：审批单文字层 / 审批单读图 / NULL（报账流程存量）。'
  '存在的理由是**两批不许互相覆盖**：'
  'load_822gh_entertain_approval.py 只管文字层，它的 DELETE 要靠这一列'
  '收窄作用域，否则重跑一次就把读图的成果删光。';
