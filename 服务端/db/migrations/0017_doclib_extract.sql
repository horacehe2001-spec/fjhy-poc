-- ============================================================================
-- 0017 · 制度抽取（从只读文件库出发）
-- ----------------------------------------------------------------------------
-- 为什么**不复用 0005 的 extract_task / extract_candidate**：
--   那两张表的外键挂在 policy_doc / policy_clause 上，口径是「上传进系统的
--   制度副本」。本条链路的输入是只读文件库（doc_lib_file），两者在 0015 里
--   已经把边界讲死了。硬接过去要么砍外键、要么给每份磁盘文件伪造一行
--   policy_doc——前者让既有约束失效，后者造假数据，都比多两张表贵。
--
-- 这条链路产出什么，必须一句话说清，否则会被当成「规则已经抽好了」：
--   **它产出的是「候选条款」和「草稿」，不是规则。**
--   确定性那一步只回答：这份制度里哪些条款带阈值/禁止性表述、
--   其中哪些至今没有任何规则引用它。要不要变成规则，是业务专家的判断。
--
-- 两条口径跟着结果一起显示，少一条就会被读错：
--   1.「未覆盖」不等于「缺规则」：定义、职责、流程叙述类条款本来就不该成规则。
--   2.「已覆盖」不等于「覆盖完整」：只表示有规则引用了这个条款号，
--      不代表该条款每一款每一项都被规则表达了。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 切出来的条款
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doclib_clause (
    clause_id  TEXT        PRIMARY KEY,
    file_id    TEXT        NOT NULL REFERENCES doc_lib_file(file_id) ON DELETE CASCADE,
    seq        INTEGER     NOT NULL,            -- 在该文件内的序号，保证清单顺序稳定
    clause_no  TEXT        NOT NULL,            -- 第十条 / 附件2 第3项
    title      TEXT,                            -- 「第十条（住宿费）」里的括注
    body       TEXT        NOT NULL,
    char_pos   INTEGER     NOT NULL,            -- 在正文里的起始位置，供回查原文
    features   TEXT[]      NOT NULL DEFAULT '{}',   -- 阈值/数值/禁止/强制/例外/名单制
    numbers    TEXT[]      NOT NULL DEFAULT '{}',   -- 条款里出现的数值表述
    doc_kind   TEXT        NOT NULL,            -- 制度 | 范本 | 其他
    covered_by TEXT[]      NOT NULL DEFAULT '{}',   -- 引用了这条的规则号
    file_fingerprint TEXT  NOT NULL,            -- 切的时候文件是哪一版
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_doclib_clause_kind CHECK (doc_kind IN ('制度', '范本', '其他'))
);

CREATE INDEX IF NOT EXISTS ix_doclib_clause_file ON doclib_clause (file_id, seq);
CREATE INDEX IF NOT EXISTS ix_doclib_clause_kind ON doclib_clause (doc_kind);

COMMENT ON COLUMN doclib_clause.doc_kind IS
  '范本=合同/申请书/通知书/协议这类**文书模板**。它们里面也有「第X条」，'
  '但那是合同条款不是制度条款。混进统计会让「制度里还有 N 条没变成规则」失真——'
  '参考工程实扫时一份《特别约定》一家就贡献了 41 条。单独分组，仍可查看。';

COMMENT ON COLUMN doclib_clause.features IS
  '带这些表述的条款才可能变成可判定的规则。'
  '**「以上/以下/以内」必须紧跟在数字后面才算阈值**：单独匹配这三个词时，'
  '「分为以下两类」「以下统称差旅费」会把名词解释条也标成阈值条款，'
  '而误报会让「还有多少条没变成规则」这个数字整体失真——这个数字正是本功能的全部价值。';

COMMENT ON COLUMN doclib_clause.file_fingerprint IS
  '切分时文件的指纹。文件后来被改过，这些条款就是旧版的——'
  '与 doc_lib_file.fingerprint 不等即可判定，不另设「过期」标志位。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 抽取批次
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doclib_extract_task (
    task_id       TEXT        PRIMARY KEY,
    mode          TEXT        NOT NULL,          -- 条款切分 | AI草稿
    scope         TEXT        NOT NULL,          -- 全部 | 仅变化 | 指定文件
    llm_config_id TEXT,                          -- AI草稿才有
    status        TEXT        NOT NULL DEFAULT 'running',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    file_cnt      INTEGER     NOT NULL DEFAULT 0,
    clause_cnt    INTEGER     NOT NULL DEFAULT 0,
    candidate_cnt INTEGER     NOT NULL DEFAULT 0,   -- 带特征、且没被规则覆盖的
    draft_cnt     INTEGER     NOT NULL DEFAULT 0,
    error         TEXT,
    note          TEXT,
    CONSTRAINT ck_dlx_mode   CHECK (mode IN ('条款切分', 'AI草稿')),
    CONSTRAINT ck_dlx_status CHECK (status IN ('running', 'done', 'failed'))
);

-- ─────────────────────────────────────────────────────────────────────────────
-- AI 抽出来的八要素草稿
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doclib_draft (
    draft_id     TEXT        PRIMARY KEY,
    task_id      TEXT        NOT NULL REFERENCES doclib_extract_task(task_id) ON DELETE CASCADE,
    clause_id    TEXT        NOT NULL REFERENCES doclib_clause(clause_id) ON DELETE CASCADE,
    rulable      BOOLEAN,                        -- 模型判断这条能不能规则化
    draft        JSONB       NOT NULL,           -- 八要素草稿原样
    number_check JSONB       NOT NULL,           -- 数值核对结果，见下
    sent_chars   INTEGER     NOT NULL DEFAULT 0, -- 实际发给模型多少字
    truncated    BOOLEAN     NOT NULL DEFAULT FALSE,
    model        TEXT,
    latency_ms   INTEGER,
    review_state TEXT        NOT NULL DEFAULT '待审',
    reviewer     TEXT,
    reviewed_at  TIMESTAMPTZ,
    reject_reason TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_dld_state CHECK (review_state IN ('待审', '采纳', '驳回'))
);

CREATE INDEX IF NOT EXISTS ix_doclib_draft_task   ON doclib_draft (task_id);
CREATE INDEX IF NOT EXISTS ix_doclib_draft_clause ON doclib_draft (clause_id);

COMMENT ON TABLE doclib_draft IS
  '抽取产物一律是**草稿**，不是规则。采纳之后才写 rule 表，且必须记 reviewer。'
  '理由很直接：规则是制度的投影，判错一条就是对一批单据误报或漏报。'
  '模型抽出来的八要素里「判定逻辑」和「例外」最容易丢细节——'
  '实测市内交通费那条，制度原文有 40 元这一档（仅往返中转站），'
  '漏掉就会把样本里 120 元的正常单判成超标准。';

COMMENT ON COLUMN doclib_draft.number_check IS
  '数值核对：草稿里出现、而条款原文里没有的数字 = 幻觉嫌疑，逐个列出。'
  '**这是能确定性检出的一类幻觉，必须查**：抽规则时编一个阈值出来比不抽更糟，'
  '它会以「有制度依据」的面目进入规则清单，而审定的人未必逐字回查原文。'
  '核对不通过的草稿照样存下来，但清单上必须打红标——删掉它等于掩盖模型的问题。';

COMMENT ON COLUMN doclib_draft.sent_chars IS
  '实际发给模型的字数（原文可能被截断）。发出去的内容必须可交代：'
  '哪一段原文离开了本系统、有没有被截断，都要查得到。';
