-- ============================================================================
-- 0008 · 把「住宿费标准档位」从「职级」里拆出来
-- ----------------------------------------------------------------------------
-- 这是 0007 加的那个外键报错抓出来的建模错误。
--
-- 原来 standard_limit.rank_level 的外键指向 EN12（职级），而装载器往里写的是
-- 制度附件1 的**列名**「其他人员」——EN12 里没有这个值，于是插入被拒。
-- 看起来像「枚举漏了一个取值」，其实是两个概念被混成一个：
--
--   EN12 职级            系统里「人」的字段，取值来自审批意见页「申请人」栏：
--                        公司领导 / 其他员工
--   EN21 住宿费标准档位    制度附件1 住宿费标准表的列名：
--                        公司领导 / 其他人员
--
-- 混在一起的后果很具体：细则第十二条规定**分公司人员一律按「其他人员」列执行**，
-- 而 40 张样本的核算主体全部是财智分公司。若按职级直接查限额，
-- 财智分公司的「公司领导」会取到 480 元档而不是 380 元档——
-- 一张 400 元/晚的单子本该报超标准，结果放过。全量样本都走这一支，所以是全量漏报。
--
-- 所以这个迁移做三件事：
--   1. standard_limit.rank_level 改成档位，外键指向 EN21
--   2. 新建 rank_tier_map 承载「主体 × 职级 → 档位」的映射（不是恒等映射）
--   3. 职级取不到时的默认行为写成数据，而不是埋在代码里
-- ============================================================================

-- ── 1 档位列改指 EN21 ────────────────────────────────────────────────────────
ALTER TABLE standard_limit DROP CONSTRAINT IF EXISTS fk_std_rank;
ALTER TABLE standard_limit DROP COLUMN IF EXISTS rank_enum;

-- 列名也跟着改：叫 rank_level 会继续误导下一个人以为它是职级
ALTER TABLE standard_limit RENAME COLUMN rank_level TO tier;
ALTER TABLE standard_limit
    ADD COLUMN IF NOT EXISTS tier_enum TEXT GENERATED ALWAYS AS ('EN21') STORED;

UPDATE standard_limit s SET tier = NULL
 WHERE tier IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM enum_value_current e
                   WHERE e.enum_id = 'EN21' AND e.value = s.tier);
SET CONSTRAINTS ALL IMMEDIATE;

ALTER TABLE standard_limit
    ADD CONSTRAINT fk_std_tier
    FOREIGN KEY (tier_enum, tier) REFERENCES enum_value_current(enum_id, value)
    DEFERRABLE INITIALLY DEFERRED;

COMMENT ON COLUMN standard_limit.tier IS
  '住宿费标准档位（EN21：公司领导 / 其他人员），**是制度附件1 的列名，不是人的职级**。'
  '人的职级在 employee.rank_level（EN12：公司领导 / 其他员工）。'
  '两者之间靠 rank_tier_map 映射，映射带主体条件，不是恒等。';

-- ── 2 主体 × 职级 → 档位 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rank_tier_map (
    id           BIGSERIAL PRIMARY KEY,
    subject      TEXT NOT NULL,                 -- 核算主体名，'*' 表示通用
    rank_level   TEXT,                          -- EN12；NULL 表示「职级取不到」这一支
    tier         TEXT,                          -- EN21；NULL 配合 on_unknown
    clause       TEXT NOT NULL,                 -- 出处条款
    confidence   TEXT NOT NULL,
    on_unknown   TEXT,                          -- 职级取不到时的行为，只允许「待人工」
    note         TEXT,
    CONSTRAINT ck_rtm_conf CHECK (confidence IN ('人眼读图','OCR','原生文本')),
    -- 兜底行为只允许转人工，不允许写成某个档位
    CONSTRAINT ck_rtm_unknown CHECK (on_unknown IS NULL OR on_unknown = '待人工'),
    -- 要么给出档位，要么说明职级未知时转人工，不能两头都空
    CONSTRAINT ck_rtm_complete CHECK (tier IS NOT NULL OR on_unknown IS NOT NULL)
);

COMMENT ON CONSTRAINT ck_rtm_unknown ON rank_tier_map IS
  '**职级取不到时不许兜底到任何档位。** 兜到「其他人员」（低档 380）会把合规的'
  '领导住宿（480 以内）判成超标，是误报；兜到「公司领导」（高档 480）会放过'
  '超标的普通员工，是漏报。两个方向都错，所以只能转人工。'
  '职级字段 C0804 的可得性是「待业务侧明确」，这一支是当前的默认路径。';

-- 具体映射从本体的 business.rank_to_tier 装载（app/ontology/loader.py），
-- 不在这里写死数据——数据属于资产，schema 属于代码。
CREATE INDEX IF NOT EXISTS ix_rtm_lookup ON rank_tier_map (subject, rank_level);

-- ── 3 查限额的那条路：主体 + 职级 + 地区 → 限额 ──────────────────────────────
CREATE OR REPLACE VIEW v_accommodation_limit AS
SELECT m.subject,
       m.rank_level,
       m.tier,
       s.region,
       s.limit_amount,
       s.unit,
       s.confidence,
       s.cross_checked,
       s.source_clause
FROM rank_tier_map m
LEFT JOIN standard_limit s
       ON s.std_type = '住宿费限额标准' AND s.tier = m.tier
WHERE m.tier IS NOT NULL;

COMMENT ON VIEW v_accommodation_limit IS
  '住宿费限额的完整查法：主体 × 职级 → 档位 → （档位 × 地区）→ 限额。'
  '中间少一环就会取错档。'
  '注意这个视图只回「职级已知」的组合；职级未知的那一支（on_unknown=待人工）'
  '故意不在视图里，免得有人 LEFT JOIN 一下就当成有结果。';
