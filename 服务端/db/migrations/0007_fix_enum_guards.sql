-- ============================================================================
-- 0007 · 补上漏掉的枚举列外键
-- ----------------------------------------------------------------------------
-- 为什么单开一个迁移文件而不是改 0004：
--   0004 已经应用过了。migrate.py 会记录每个迁移文件的 sha256，
--   改动已应用的文件会被拒绝执行并报错——这是故意的：
--   改了文件而库结构不变，两者就永久不一致，而且在 code review 里看不出来。
--   所以结构修正一律新增迁移。
--
-- 这个漏洞是**启动时契约校验自己发现的**：
--   contract.verify() 里有一项「业务表的枚举列都有外键约束」，
--   它查 information_schema 找出所有枚举语义的列，再查 pg_constraint
--   看有没有外键护着。0004 给 standard_limit.std_type 配了 std_enum + 外键，
--   但 rank_level 漏了。
--   漏的后果：standard_limit 里可以写进「副总」「中层」这类本体 EN12 里
--   不存在的职级（EN12 只有「公司领导」「其他人员」两档，因为细则第十二条
--   已把分公司人员一律归入「其他人员」）。一旦写进去，
--   按职级查限额的规则会匹配不到任何行，于是住宿费标准静默失效。
-- ============================================================================

ALTER TABLE standard_limit
    ADD COLUMN IF NOT EXISTS rank_enum TEXT GENERATED ALWAYS AS ('EN12') STORED;

-- 先清掉不合法的历史值，再加约束。
-- 这里不会丢数据：standard_limit 是从标准表 YAML 装载的投影，
-- 装载器用的就是 EN12 的两个取值。这条 UPDATE 是防御性的。
UPDATE standard_limit s
   SET rank_level = NULL
 WHERE rank_level IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM enum_value_current e
                   WHERE e.enum_id = 'EN12' AND e.value = s.rank_level);

-- 这一行是踩出来的，不能删：
-- 上面的 UPDATE 会触发 0004 里那些 DEFERRABLE INITIALLY DEFERRED 外键的延迟检查，
-- 留下 pending trigger events；同一个事务里紧接着 ALTER TABLE 就会被拒绝，
-- 报 "cannot ALTER TABLE ... because it has pending trigger events"。
-- SET CONSTRAINTS ALL IMMEDIATE 把延迟检查立刻执行掉，清空待处理事件。
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE standard_limit
    DROP CONSTRAINT IF EXISTS fk_std_rank;
ALTER TABLE standard_limit
    ADD CONSTRAINT fk_std_rank
    FOREIGN KEY (rank_enum, rank_level) REFERENCES enum_value_current(enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON COLUMN standard_limit.rank_level IS
  '适用职级。本体 EN12 只有「公司领导」「其他人员」两档——'
  '不是简化，是细则第十二条本身就把分公司人员一律归入「其他人员」。'
  '这一列必须受 enum_value_current 约束：写进 EN12 里不存在的职级，'
  '按职级查限额就会匹配不到任何行，住宿费标准于是静默失效。';

-- standard_limit 的 region 不做枚举约束：地区是开放集合（全国 30+ 省市），
-- 且目前只录了客户用得上的 15 行（G-13）。约束上去只会把未来要补的行挡住。
-- 这一条写下来是为了说明「为什么这一列故意不加约束」，
-- 免得下次有人看到契约校验没覆盖它就顺手补一个。
COMMENT ON COLUMN standard_limit.region IS
  '适用地区。**故意不加枚举约束**：地区是开放集合，附件1 全表 30+ 省，'
  '目前只录了客户用得上的 15 行（福建各市 + 厦门 + 北京上海等），见 G-13。'
  '加约束会把将来补录的行挡在外面。';
