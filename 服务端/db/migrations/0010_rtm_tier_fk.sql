-- ============================================================================
-- 0010 · rank_tier_map.tier 补外键（指向 EN21 住宿费标准档位）
-- ----------------------------------------------------------------------------
-- 0008 建 rank_tier_map 时给 tier 列留了语义但没加约束；0009 补了 rank_level
-- 那一侧（EN12 职级），这里补 tier 那一侧（EN21 档位）。
--
-- 为什么这个映射表的两列都必须有约束：它是住宿费限额取值链的中间一环
--   主体 × 职级 → 档位 → （档位 × 地区）→ 限额
-- 任一侧写进本体里不存在的值，链就断在中间，limit_amount 取不到，
-- 规则退化成「待人工」。退化本身不报错，所以只能靠约束在写入时挡住。
--
-- tier 为 NULL 的那一行（职级未知 → 待人工）不受影响：MATCH SIMPLE。
-- ============================================================================

ALTER TABLE rank_tier_map
    ADD COLUMN IF NOT EXISTS tier_enum TEXT GENERATED ALWAYS AS ('EN21') STORED;

UPDATE rank_tier_map m SET tier = NULL
 WHERE tier IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM enum_value_current e
                   WHERE e.enum_id = 'EN21' AND e.value = m.tier);
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE rank_tier_map DROP CONSTRAINT IF EXISTS fk_rtm_tier;
ALTER TABLE rank_tier_map
    ADD CONSTRAINT fk_rtm_tier
    FOREIGN KEY (tier_enum, tier) REFERENCES enum_value_current(enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;
