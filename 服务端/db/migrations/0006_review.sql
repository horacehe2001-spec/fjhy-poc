-- ============================================================================
-- 0006 · 审核结果与人工复核闸门
-- ----------------------------------------------------------------------------
-- 这一组表是「报账单 AI 审核端」的数据底座。
--
-- 【最重要的建模决定：AI 结论不阻断流程】
--   实测：FJPG-BX2610130821 在 2026-10-13 17:25:03 得到「AI审核不通过」，
--   17:35:41 仍进入共享审核，并由人工给出退回理由。
--   所以 review_run.conclusion **不是闸门**，manual_review 才是。
--   把 AI 结论建成闸门，等于把一个本来由人掌握的权责交给模型。
-- ============================================================================

CREATE TABLE IF NOT EXISTS review_run (
    run_id           TEXT PRIMARY KEY,
    bill_no          TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    cycle_id         TEXT,                               -- → cycle_log.cycle_id
    rule_set_version TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    conclusion       TEXT NOT NULL,                      -- pass | review | reject
    -- 候选 / 执行 / 剔除三个数必须同时记
    rule_candidates  INTEGER NOT NULL DEFAULT 0,
    rule_executed    INTEGER NOT NULL DEFAULT 0,
    rule_excluded    JSONB,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    blocked_count    INTEGER NOT NULL DEFAULT 0,         -- 禁止级命中数
    warned_count     INTEGER NOT NULL DEFAULT 0,         -- 警告级命中数
    pending_count    INTEGER NOT NULL DEFAULT 0,         -- 转人工数
    duration_ms      INTEGER,
    is_demo          BOOLEAN NOT NULL DEFAULT FALSE,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    CONSTRAINT ck_run_conclusion CHECK (conclusion IN ('pass','review','reject')),
    -- 执行数不可能超过候选数
    CONSTRAINT ck_run_counts CHECK (rule_executed <= rule_candidates)
);

CREATE INDEX IF NOT EXISTS ix_run_bill ON review_run (bill_no, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_run_concl ON review_run (conclusion, started_at DESC);

COMMENT ON COLUMN review_run.rule_excluded IS
  '**被剔除的规则必须在报告上单列一节，不能只写在日志里。** 审核岗看到'
  '「本次执行 17 条」时，必须同时看到「另有 20 条因缺字段未执行」，'
  '否则他会以为 82 条都查过了。让 AI 声称复核了它读不到的数据，比少复核更危险。';

COMMENT ON COLUMN review_run.conclusion IS
  '**这一列不参与任何流程阻断。** 它是给人看的参考。'
  '流程闸门在 manual_review 表。';

CREATE TABLE IF NOT EXISTS review_finding (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES review_run(run_id) ON DELETE CASCADE,
    rule_id       TEXT NOT NULL,
    rule_version  INTEGER NOT NULL,
    rule_class    TEXT NOT NULL,                         -- hard | mixed | soft
    control_type  TEXT NOT NULL,                         -- 禁止 | 警告
    outcome       TEXT NOT NULL,                         -- hit | pass | pending | excluded
    severity      TEXT,
    -- 证据三件套：判了什么、依据哪条条款、用的哪些字段的什么值
    message       TEXT NOT NULL,
    evidence      JSONB NOT NULL,
    clause_ref    JSONB,
    concepts_used JSONB,
    matched_text  TEXT,                                  -- 关键词类规则要回显命中的原文片段
    pending_reason TEXT,
    llm_involved  BOOLEAN NOT NULL DEFAULT FALSE,
    llm_rationale TEXT,
    needs_human   BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_find_outcome CHECK (outcome IN ('hit','pass','pending','excluded')),
    CONSTRAINT ck_find_class   CHECK (rule_class IN ('hard','mixed','soft')),
    CONSTRAINT ck_find_control CHECK (control_type IN ('禁止','警告')),
    -- 软规则命中必须要求人工复核
    CONSTRAINT ck_soft_needs_human CHECK (NOT (rule_class = 'soft' AND outcome = 'hit') OR needs_human),
    -- 模型参与过的结论必须带理由，否则不可复核
    CONSTRAINT ck_llm_has_rationale CHECK (NOT llm_involved OR llm_rationale IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_find_run  ON review_finding (run_id, outcome);
CREATE INDEX IF NOT EXISTS ix_find_rule ON review_finding (rule_id, outcome);

COMMENT ON COLUMN review_finding.evidence IS
  '**证据必须能让人自己复核一遍，不能只给结论。** 比如超标准住宿：'
  '要给出「实际 500 元/晚、厦门标准 650/520、职级口径取自细则第十二条」，'
  '而不是一句「住宿费超标」。'
  '关键词类规则还要回显 matched_text——命中「黄山」的单子，'
  '看到回显的「福州黄山路XX酒店」就立刻知道是路名，三秒结束；'
  '只说「发现敏感内容」则要重新翻一遍发票。'
  '回显的原文片段同样要先过脱敏。';

COMMENT ON CONSTRAINT ck_soft_needs_human ON review_finding IS
  '软规则靠模型辅助判断，命中即必须人工复核。这条约束让「忘记要求复核」'
  '在库这一层就写不进去，而不是靠代码自觉。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 人工复核闸门 —— 真正的闸在这里
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS manual_review (
    id             BIGSERIAL PRIMARY KEY,
    bill_no        TEXT NOT NULL REFERENCES expense_bill(bill_no) ON DELETE CASCADE,
    run_id         TEXT REFERENCES review_run(run_id) ON DELETE SET NULL,
    reviewer       TEXT NOT NULL,                        -- 【敏感】
    decision       TEXT NOT NULL,                        -- agree | return | agree_with_note
    return_reason  TEXT,                                 -- 退回理由，**F 阶段最重要的输入**
    disagree_with  JSONB,                                -- 人工不认可的 finding id 列表
    agree_with     JSONB,
    decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_mr_decision CHECK (decision IN ('agree','return','agree_with_note')),
    -- 退回必须给理由
    CONSTRAINT ck_mr_return_reason CHECK (decision <> 'return' OR return_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_mr_bill ON manual_review (bill_no, decided_at DESC);

COMMENT ON TABLE manual_review IS
  '**这张表才是闸门。** AI 审核不通过不阻断流程（实测印证），'
  '人工在这里给出的 decision 才决定单据往前走还是退回。'
  'disagree_with 列是误报的直接证据来源：人工明确「不认可第 3 条命中」时，'
  '那一条就是一次误报，应回流到 feedback_log(deviation_kind=false_positive)。';

COMMENT ON COLUMN manual_review.return_reason IS
  '退回理由是回流闭环的起点。实测 50 页审批记录里有真实退回理由，'
  '比如 FJPG-BX2610132121 与 FJPG-BX2610090441 的补充说明都在解释返程车站不同'
  '（「因泉州港务集团办公地点为多处，返程时办公地点离泉港站较近」）'
  '——这正是 TR-D6-05 的例外出口在起作用。'
  '这类文本含真实姓名与单位名，出口前过 M06。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 回流：人工结论 → feedback_log 的中间视图
-- ─────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_review_deviation AS
SELECT
    r.run_id,
    r.bill_no,
    r.cycle_id,
    r.conclusion                                            AS ai_conclusion,
    m.decision                                              AS human_decision,
    CASE
        -- AI 说有问题、人工同意 → 一致
        WHEN r.conclusion IN ('reject','review') AND m.decision = 'return' THEN 'match'
        -- AI 说没问题、人工退回 → 漏报
        WHEN r.conclusion = 'pass' AND m.decision = 'return'               THEN 'false_negative'
        -- AI 说有问题、人工放行 → 误报
        WHEN r.conclusion IN ('reject','review') AND m.decision <> 'return' THEN 'false_positive'
        ELSE 'match'
    END                                                     AS deviation_kind,
    m.return_reason,
    m.disagree_with,
    m.decided_at
FROM review_run r
JOIN manual_review m ON m.run_id = r.run_id;

COMMENT ON VIEW v_review_deviation IS
  '**误报与漏报分开计，不合成一个「准确率」。** 漏报放过风险；'
  '误报把正常单据判成问题件，审核岗很快就不再信任这套系统。'
  '合成一个数会把误报的代价藏起来。'
  'F 阶段从这个视图取数写 feedback_log，source=human_review。';
