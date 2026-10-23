-- ============================================================================
-- 0009 · rank_tier_map.rank_level 补外键
-- ----------------------------------------------------------------------------
-- 又是启动时契约校验里那一项「业务表的枚举列都有外键约束」报出来的。
-- rank_tier_map.rank_level 存的是 EN12 职级，必须受约束：
-- 写进「中层」「副总」这类 EN12 里不存在的职级，映射就查不到档位，
-- 于是住宿费限额取不到，而取不到时的行为是「待人工」——
-- 表面看是「谨慎」，实际是一条本该自动判的规则静默退化成人工。
--
-- rank_level 为 NULL 的那一行（职级未知 → 待人工）不受影响：
-- 外键默认 MATCH SIMPLE，任一列为 NULL 时约束自动满足。
-- ============================================================================

ALTER TABLE rank_tier_map
    ADD COLUMN IF NOT EXISTS rank_enum TEXT GENERATED ALWAYS AS ('EN12') STORED;

UPDATE rank_tier_map m SET rank_level = NULL
 WHERE rank_level IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM enum_value_current e
                   WHERE e.enum_id = 'EN12' AND e.value = m.rank_level);
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE rank_tier_map DROP CONSTRAINT IF EXISTS fk_rtm_rank;
ALTER TABLE rank_tier_map
    ADD CONSTRAINT fk_rtm_rank
    FOREIGN KEY (rank_enum, rank_level) REFERENCES enum_value_current(enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
