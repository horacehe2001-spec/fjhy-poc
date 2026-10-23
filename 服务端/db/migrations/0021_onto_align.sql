-- ============================================================================
-- 0021 · 规则本体：统一口径的三张台账
-- ----------------------------------------------------------------------------
-- 「统一口径是本体要做的事，规则抽取只负责抽取」（2026-10-19 用户口径）。
-- 抽取那一栏产出的是**散的**：条款、草稿、归并组、口径冲突。
-- 本体这一栏要回答的是**另一个问题**——「这些东西和我现有的规则清单对得上吗」。
--
-- 对账必须是**双向**的，单向会各漏一半：
--   规则 → 依据   只看这一向，看不出制度里写了而规则清单里根本没有的要求
--   制度 → 落地   只看这一向，看不出那 82 条里有几条其实找不到制度依据
-- 实测 0005 的 rule_relation 注释里已经记了两处口径分歧（市内交通补助
-- 40 元 vs 50 元、机票保险「不允许报销」vs「可以购买一份」），
-- 这类东西只有两向都对一遍才会浮出来。
--
-- 三张表分成**算出来的**和**人拍的**两类，这是这个迁移最重要的一条线：
--   onto_align / onto_rule_cover  算出来的 → 每次重算 DELETE+INSERT
--   onto_adoption                 人拍的   → 重算一个字都不许动
-- 混成一张表的后果很具体：重算一次，谁采纳过、谁驳回过就全没了，
-- 而「谁拍的板」在制度口径这种事情上和结论本身一样重要。
-- ============================================================================

-- ── 一、制度 → 落地：归并组对上了哪几条规则 ─────────────────────────────────
CREATE TABLE IF NOT EXISTS onto_align (
    group_key    TEXT        PRIMARY KEY,       -- 与 doclib_rule_source.group_key 同一套键
    verdict      TEXT        NOT NULL,          -- 已落地 | 未落地 | 判不出
    rule_ids     TEXT[]      NOT NULL DEFAULT '{}',
    match_basis  TEXT        NOT NULL,          -- 凭什么这么判，写给人看
    n_sources    INTEGER     NOT NULL DEFAULT 0,
    evidence     TEXT        NOT NULL,          -- 多源印证 | 单一来源
    has_conflict BOOLEAN     NOT NULL DEFAULT FALSE,
    has_ocr      BOOLEAN     NOT NULL DEFAULT FALSE,
    aligned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_oa_verdict  CHECK (verdict IN ('已落地', '未落地', '判不出')),
    CONSTRAINT ck_oa_evidence CHECK (evidence IN ('多源印证', '单一来源')),
    -- 判「已落地」必须说得出落在哪条规则上。说不出就不是已落地。
    CONSTRAINT ck_oa_landed   CHECK (verdict <> '已落地' OR array_length(rule_ids, 1) >= 1)
);

COMMENT ON TABLE onto_align IS
  '制度 → 落地方向的对账结果。**算出来的，每次统一口径重算全表。**'
  '人的动作（采纳/驳回/暂缓）在 onto_adoption 里，重算不动它。';

COMMENT ON COLUMN onto_align.verdict IS
  '**必须有「判不出」这一档，不许二分。** 匹配只认硬证据：规则的 source_clause 里'
  '写的文件名（归一后）和条款号与归并组里某条条款对得上，才算落地。'
  '对不上有两种完全不同的情况——真的没落地，或者那条规则的 source_clause'
  '压根没写条款号（coverage_index 的 blind 名单实测有若干条）。'
  '把后者算进「未落地」，人就会去新增一条其实已经存在的规则，'
  '而**重复规则对同一张单会给出两条结论**，比漏一条更难查。';

COMMENT ON COLUMN onto_align.rule_ids IS
  '对上的规则号，可能多条——同一个主题在规则清单里本来就可能拆成几条'
  '（住宿费有标准、有发票、有超标处置）。这里不收敛成一条：'
  '收敛就要选一条当「主」的，而那是没有依据的选择。';

COMMENT ON COLUMN onto_align.match_basis IS
  '判定依据的大白话，直接显示在页面上。'
  '不写这一列，页面上就只有一个「未落地」标签，而人没法判断该信它还是该核查。';

-- ── 二、规则 → 依据：每条规则找不找得到制度出处 ─────────────────────────────
CREATE TABLE IF NOT EXISTS onto_rule_cover (
    rule_id     TEXT        NOT NULL,
    version     INTEGER     NOT NULL,
    state       TEXT        NOT NULL,           -- 有制度依据 | 仅规则表 | 查无依据 | 来源未写明
    group_keys  TEXT[]      NOT NULL DEFAULT '{}',
    basis       TEXT        NOT NULL,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, version),
    FOREIGN KEY (rule_id, version) REFERENCES rule(rule_id, version) ON DELETE CASCADE,
    CONSTRAINT ck_orc_state CHECK (state IN ('有制度依据', '仅规则表', '查无依据', '来源未写明'))
);

COMMENT ON TABLE onto_rule_cover IS
  '规则 → 依据方向的对账结果。同样是**算出来的**，每次重算全表。'
  '这里带外键（而 doclib_change 故意不带）：规则被删掉，它的对账结论就该跟着消失，'
  '留着会变成一条指向不存在规则的记录。变化台账不同——那是事件流水，'
  '事件要比它的主体活得久。';

COMMENT ON COLUMN onto_rule_cover.state IS
  '四档，**「仅规则表」必须单独一档**：规则表（差旅费审核规则.xlsx）是客户人工'
  '整理的二手产物，制度文件才是法源。一条规则只有规则表写了、制度里找不到，'
  '它可能是业务惯例而不是制度要求——这件事和「有制度依据」差别很大，'
  '合成一档就等于把惯例当制度。'
  '「来源未写明」也不许并进「查无依据」：前者是我们的台账没记全（规则的'
  'source_clause 缺条款号），后者是真找不到制度支撑，责任方和下一步动作都不同。';

-- ── 三、人的动作：采纳 / 驳回 / 暂缓 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS onto_adoption (
    adoption_id  TEXT        PRIMARY KEY,
    group_key    TEXT        NOT NULL,
    action       TEXT        NOT NULL,          -- 采纳 | 驳回 | 暂缓
    rule_id      TEXT,                          -- 采纳时新建的规则号
    rule_version INTEGER,
    adopted_by   TEXT        NOT NULL,
    adopted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason       TEXT,
    CONSTRAINT ck_ado_action CHECK (action IN ('采纳', '驳回', '暂缓')),
    -- 采纳必须落到一条具体规则上，驳回/暂缓必须写理由
    CONSTRAINT ck_ado_rule   CHECK (action <> '采纳' OR rule_id IS NOT NULL),
    CONSTRAINT ck_ado_reason CHECK (action = '采纳' OR
                                    (reason IS NOT NULL AND btrim(reason) <> ''))
);

-- 一个归并组只允许采纳一次。重复采纳会在本体里长出两条同源规则，
-- 对同一张单给出两条结论，而两条都「有制度依据」，查不出哪条是多余的。
CREATE UNIQUE INDEX IF NOT EXISTS ux_ado_adopted_once
    ON onto_adoption (group_key) WHERE action = '采纳';

CREATE INDEX IF NOT EXISTS ix_ado_group ON onto_adoption (group_key);

COMMENT ON TABLE onto_adoption IS
  '采纳入本体的动作台账。**Agent 写不进这张表，也写不进 rule 表**——'
  'WRITABLE_TABLES 白名单里没有它们，不是「约定不写」，是能力上就不给。'
  '采纳是人的动作：adopted_by 是必填的，没有署名的采纳在审计上等于没发生。';

COMMENT ON COLUMN onto_adoption.action IS
  '采纳 | 驳回 | 暂缓。**「暂缓」这一档不是凑数**：口径有冲突但还没裁决的组，'
  '既不能采纳（会把一方口径当成结论），也不该驳回（制度要求是真实存在的）。'
  '没有这一档，人就只能在两个错选项里挑一个，或者干脆不处理——'
  '而不处理的那些会和「还没看过的」混在一起，下次无从区分。';

COMMENT ON COLUMN onto_adoption.reason IS
  '驳回与暂缓必填（由 ck_ado_reason 卡住）。'
  '「为什么不采纳」比「采纳了什么」更容易被时间冲掉：三个月后再看这个组，'
  '没有理由就只剩一个「驳回」标签，而当时的判断依据已经没人记得了。';

COMMENT ON COLUMN onto_adoption.rule_id IS
  '采纳时新建的规则号。**采纳建出来的规则一律 status=draft、compiled=false**：'
  '采纳进本体 ≠ 发布生效，发布是栏目五的事，且 0005 的 ck_release_signed'
  '要求 published 的 release 必须有签字人。';
