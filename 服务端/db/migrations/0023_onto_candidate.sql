-- ============================================================================
-- 0023 · 本体构建：候选台账 + 采纳动作（含 YAML 补丁留痕）
-- ----------------------------------------------------------------------------
-- 「本体怎么建的？是不是缺少按钮，点击后由智能体协同完成」（2026-10-19 用户）。
--
-- 缺的不只是按钮。在这个迁移之前，本体骨架（17 实体 / 121 概念 / 21 枚举 /
-- 116 枚举取值 / 18 关系 / 9 流程节点 / 12 迁移）是**人手写 本体_v0.1.yaml
-- 再用命令行脚本装进库的**——页面上既没有入口，也没有任何智能体参与。
-- 这两张表是给「本体构建」那条协同链落产物用的。
--
-- **这条链产出候选，不产出本体。** 和规则抽取那边同一条口径：
-- Agent 写不进 ontology_* 任何一张表（WRITABLE_TABLES 白名单里不给），
-- 采纳是人的动作。
--
-- 采纳之后写哪里，是 2026-10-19 用户拍的：**定点追加进 本体_v0.1.yaml，
-- 再自动重装**。不是写库。理由是现成的铁律——YAML 是源、库是投影、单向永不回写：
--   · 直接写 ontology_* 表，库和 YAML 会**静默分叉**。而「本体未漂移」那道
--     启动自检比的是 YAML 文件的 sha256，不比行数，这种分叉它恰好查不出来；
--     更糟的是下次 loader 重装会把采纳进来的本体元素全部抹掉。
--   · 回写整份 YAML（yaml.load→dump）会把 924 行里的注释一次性抹光，
--     而注释里是口径纪律和踩坑记录——这种损失在 code review 里极难发现。
-- 所以只能**按行定点插入**，并把补丁内容、插入位置、备份路径全部留痕：
-- 程序改了用户的源文件，必须能原样复盘。
-- ============================================================================

-- ── 一、候选（算出来的，每次重算）─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS onto_candidate (
    cand_id     TEXT        PRIMARY KEY,       -- kind+key 的稳定哈希，重算后同一候选还是同一行
    kind        TEXT        NOT NULL,          -- 实体 | 概念 | 枚举取值 | 流程节点 | 流程迁移
    cand_key    TEXT        NOT NULL,          -- 候选标识：列头名 / EN16|总经理 / 节点名 / A→B
    cand_name   TEXT        NOT NULL,
    source_kind TEXT        NOT NULL,          -- 单据列头 | 真实取值 | 审批记录 | 条款 | 规则引用
    evidence    TEXT        NOT NULL,          -- 硬证据：出现在哪、出现多少次
    occurrences INTEGER,
    verdict     TEXT        NOT NULL,          -- 已在本体 | 建议新增 | 层间不一致
    existing_ref TEXT,                         -- 对上的本体元素 id
    basis       TEXT        NOT NULL,
    yaml_target TEXT,                          -- 采纳时该追加到 YAML 的哪一段
    found_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ocand_kind CHECK (kind IN ('实体', '概念', '枚举取值', '流程节点', '流程迁移')),
    CONSTRAINT ck_ocand_verdict CHECK (verdict IN ('已在本体', '建议新增', '层间不一致')),
    -- 判「已在本体」必须说得出对上的是哪个元素，说不出就不是已在本体
    CONSTRAINT ck_ocand_ref CHECK (verdict <> '已在本体' OR existing_ref IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_ocand_verdict ON onto_candidate (verdict, kind);

COMMENT ON TABLE onto_candidate IS
  '本体构建的候选元素。**算出来的，每次重算整表覆盖**；人的动作在 onto_cand_action。'
  '每条候选都必须有 evidence——**没有出处的候选不许进这张表**，'
  '这和 本体_v0.1.yaml 的第一条口径纪律是同一条：没出处的概念不许进本体。';

COMMENT ON COLUMN onto_candidate.verdict IS
  '三档。**「层间不一致」这一档是实测逼出来的，不是凑数**：'
  '实测 EN16「审批节点」枚举里有「综合部分管领导/总经理/分管领导」（审批记录里'
  '分别出现 5/3/2 次），而 process_node 这一层根本没有对应节点——'
  '**本体内部两层之间口径不一致**。'
  '这类东西人工读 924 行 YAML 几乎看不出来，而它一旦存在，'
  '「按节点统计」和「按枚举统计」会给出两套数，谁也说不清哪套对。'
  '把它并进「建议新增」是错的：该补的可能是节点层，也可能是枚举层写多了，'
  '方向要人判。';

COMMENT ON COLUMN onto_candidate.evidence IS
  '硬证据：这个候选在素材里的出处与出现次数。'
  '页面上只给一个候选名没有用——人要判「该不该进本体」，看的就是这一列。';

-- ── 二、人的动作（永不清，且必须能复盘对源文件做了什么）────────────────────
CREATE TABLE IF NOT EXISTS onto_cand_action (
    action_id    TEXT        PRIMARY KEY,
    cand_id      TEXT        NOT NULL,
    cand_kind    TEXT        NOT NULL,
    cand_key     TEXT        NOT NULL,
    action       TEXT        NOT NULL,         -- 采纳 | 驳回 | 暂缓
    acted_by     TEXT        NOT NULL,
    acted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason       TEXT,
    -- 采纳时程序改了用户的源文件，这四列是复盘的全部依据
    yaml_file    TEXT,
    yaml_line    INTEGER,
    yaml_patch   TEXT,
    backup_path  TEXT,
    reloaded     BOOLEAN     NOT NULL DEFAULT FALSE,
    onto_version TEXT,
    CONSTRAINT ck_ocact_action CHECK (action IN ('采纳', '驳回', '暂缓')),
    -- 采纳必须留下补丁与备份；驳回/暂缓必须写理由
    CONSTRAINT ck_ocact_patch CHECK (action <> '采纳' OR
                                     (yaml_patch IS NOT NULL AND backup_path IS NOT NULL)),
    CONSTRAINT ck_ocact_reason CHECK (action = '采纳' OR
                                      (reason IS NOT NULL AND btrim(reason) <> ''))
);

-- 一个候选只允许采纳一次：重复采纳会在 YAML 里插入两段同样的定义，
-- 而 loader 对重复 id 的行为是后者覆盖前者——两段定义只有一段生效，
-- 另一段静静地留在源文件里，下一个人读 YAML 时会被它误导。
CREATE UNIQUE INDEX IF NOT EXISTS ux_ocact_adopted_once
    ON onto_cand_action (cand_id) WHERE action = '采纳';

CREATE INDEX IF NOT EXISTS ix_ocact_cand ON onto_cand_action (cand_id);

COMMENT ON TABLE onto_cand_action IS
  '本体候选的处置台账。**采纳会改用户的源文件 本体_v0.1.yaml**，'
  '所以这张表不只记「谁采纳了什么」，还记**改了哪一行、插了什么、备份在哪**。'
  '程序动别人手写的源文件，必须能原样复盘并退回去；'
  '只记一个「已采纳」标签，三个月后没人说得清那几行是怎么进去的。';

COMMENT ON COLUMN onto_cand_action.reloaded IS
  '追加完是否成功重装进库。**分开记**：YAML 改成功而重装失败时，'
  '源文件已经变了而库还是旧的——这种半途状态必须看得见，'
  '否则页面显示「已采纳」，而本体里查不到这个元素。';

COMMENT ON COLUMN onto_cand_action.backup_path IS
  '追加前的整份备份路径（var/onto_yaml_backup/）。'
  '**定点插入前先备份**，不是为了好看：按行插入出错时，'
  '手上必须有一份改之前的原文，而不是靠 git（这个项目没有 git）。';
