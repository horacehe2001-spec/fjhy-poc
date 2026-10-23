-- ============================================================================
-- 0036 · 去掉 attachment.source_kind 的默认值，并修回被错标的演示附件
-- ----------------------------------------------------------------------------
-- 0035 给 source_kind 设了 `DEFAULT 'xlsx内嵌'`。**那是个错误的默认值。**
--
-- 实测（2026-10-21，当天就踩到）：跑完一轮全量测试之后，
-- attachment 表 273 行**全部**变成了 'xlsx内嵌'——演示单那 54 条合成附件
-- 被标成了「从客户 xlsx 里抽出来的真实附件」。
--
-- 成因：app/demo/generator.py 的 INSERT 没有写 source_kind（0035 之前这一列不存在），
-- 于是吃了列默认值；而测试会重建演示数据（conftest 里已记过这类副作用）。
--
-- 【为什么不是「把默认值改成演示合成」】
--   换个默认值只是把错误调了个方向：那样一来，将来任何一处忘了写 source_kind 的
--   真实数据插入，都会被标成演示数据。
--   **两个方向的错都很难发现，因为都不报错。**
--   所以这一列**不要默认值**：NOT NULL 且无默认 → 谁忘了填，谁当场 NotNullViolation。
--   这是本项目反复用的那条——**确定性的闸失效而没人发现，比没有这道闸更危险**。
--
-- 【判据：怎么知道哪些该标成演示合成】
--   演示附件的判据是**所挂单据是不是演示单**（expense_bill.is_demo），
--   不是文件名前缀、也不是 stored_path 为空。
--   实测演示附件的 file_name 都带「【演示】」前缀，但那是约定不是约束，
--   拿它当判据，哪天前缀改了就会静默漏掉一批。
-- ============================================================================

ALTER TABLE attachment ALTER COLUMN source_kind DROP DEFAULT;

-- 修回被错标的那些：挂在演示单上的，一律是演示合成。
UPDATE attachment a
   SET source_kind = '演示合成'
  FROM expense_bill b
 WHERE b.bill_no = a.bill_no
   AND b.is_demo
   AND a.source_kind <> '演示合成';

-- 反方向也校一遍：挂在真实单上、却标着「演示合成」的，说明抽取那一步写错了。
-- 这里**不自动改**——真实单上出现演示标记是个需要人去看的状态，
-- 静默纠正会把问题藏起来。只在迁移输出里报个数。
DO $$
DECLARE n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM attachment a JOIN expense_bill b USING (bill_no)
     WHERE NOT b.is_demo AND a.source_kind = '演示合成';
    IF n > 0 THEN
        RAISE WARNING '有 % 条附件挂在真实单上却标着「演示合成」，请人工确认', n;
    END IF;
END $$;

COMMENT ON COLUMN attachment.source_kind IS
  'xlsx内嵌 | 演示合成 | 上传。**NOT NULL 且刻意没有默认值**：'
  '每一处 INSERT 都必须显式写清楚这一条是哪来的，忘了就当场报错。'
  '0035 曾设 DEFAULT ''xlsx内嵌''，结果演示生成器吃了默认值，'
  '跑完一轮测试后 273 行全成了「xlsx内嵌」——**演示数据被标成了客户真实附件**。'
  '换个默认值只是把错调个方向，所以干脆不给默认值。'
  '演示数据必须一眼可辨（全局纪律第 12 条），而「一眼可辨」不能依赖别处的默认值。';
