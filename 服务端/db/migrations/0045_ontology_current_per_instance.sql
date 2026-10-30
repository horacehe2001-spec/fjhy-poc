-- ============================================================================
-- 0045 · 「当前本体版本」从全库一条改成每实例一条
-- ----------------------------------------------------------------------------
-- 0044 把本体链按实例隔离了，但漏了这个**部分唯一索引**：
--
--   CREATE UNIQUE INDEX uq_ontology_current
--       ON ontology_version (is_current) WHERE is_current
--
-- 它的含义是「全库只能有一条当前本体版本」。福建海运集团 已经占着那一条，
-- 于是 822港航 装载自己的本体时，最后一句 UPDATE ... SET is_current=TRUE
-- 直接撞唯一键：
--   UniqueViolation: duplicate key value violates "uq_ontology_current"
--   DETAIL: Key (is_current)=(t) already exists.
--
-- **这类「全库单例」的约束是隔离工作里最容易漏的一种**：它不在外键清单里，
-- 也不在主键清单里，survey 的时候两张表都查不到它。本项目已经踩过两次：
--   · 0041：release_current 的 singleton BOOLEAN PRIMARY KEY CHECK(singleton)
--   · 0045：这一条
-- 改法和 0041 一样，也和 domain_instance 的 ux_instance_default_per_domain
-- 是同一个模式：**把实例列加进唯一索引，「每实例一条」而不是「全库一条」**，
-- 约束仍然留在数据库层，不退化成应用层自觉。
-- ============================================================================

DROP INDEX IF EXISTS uq_ontology_current;

CREATE UNIQUE INDEX uq_ontology_current
    ON ontology_version (instance_id)
 WHERE is_current;

COMMENT ON INDEX uq_ontology_current IS
  '每个实例只能有一条「当前本体版本」。**不是全库一条**——'
  '0044 之后每个实例装自己那份 YAML，各自有各自的当前版本。'
  '写成全库一条的后果是第二个实例装载时撞唯一键，而报错信息'
  '（duplicate key (is_current)=(t)）和「本体」「实例」都不沾边，很难想到是这里。';
