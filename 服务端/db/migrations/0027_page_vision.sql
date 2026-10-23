-- ============================================================================
-- 0027 · 多模态读图：OCR 丢结构的那几页，交给读图模型再读一遍
-- ----------------------------------------------------------------------------
-- 背景（app/ocr.py 的第 3 条纪律里已经写了，这里是它的下半句）：
--   实测：住宿费标准表 OCR 后数字全在，但城市↔职级↔金额的对应关系全乱了。
--   **OCR 丢的不是字，是结构。** 而差旅规则的全部价值就在那个对应关系上。
--   交通工具等级表更彻底，OCR 出来是「(1) 分 公 | Ti [Sree 责 人 凭据 报销 等 座」，
--   TR-D3 那批规则因此一直编不出来。
--   那条纪律给的出路是两条：**要么人眼读图，要么上多模态模型**。这张表是后者。
--
-- 三条口径，每一条都是为了不让这件事变成「AI 说它看过了」：
--
-- ① **一页一模型一行，不覆盖。** 同一页用不同模型读是两份独立证据，
--    两份都要留着才能互相印证。重读一次就再加一行（attempt 递增），
--    不许原地改——改了就没法回答「上次读出来的是什么」。
--
-- ② **读图结果不是事实，是又一份线索。** 可信度三档（原生文本 / 人眼读图 / OCR）
--    里，模型读图**不自动等于人眼读图**：它同样会把看不清的格子编出来，
--    而且编得通顺。所以 verified_by 默认为空，没有人签字之前，
--    这里的数字一律不许进 standard_limit、不许进任何规则阈值。
--
-- ③ **出网要留痕。** 这是整个系统里**唯一会把客户制度文件发到外部**的动作
--    （OCR 是本机跑的）。所以逐次记：哪一页、发给谁、哪个型号、多少 token、
--    几毫秒、回了什么。事后要能回答「哪一页的图离开过本系统」。
-- ============================================================================

CREATE TABLE IF NOT EXISTS doc_page_vision (
    id             BIGSERIAL PRIMARY KEY,
    file_id        TEXT        NOT NULL REFERENCES doc_lib_file(file_id) ON DELETE CASCADE,
    page_no        INTEGER     NOT NULL,
    attempt        INTEGER     NOT NULL DEFAULT 1,       -- 同一页同一模型读第几次
    config_id      TEXT,                                 -- 用了哪条大模型配置
    model_name     TEXT        NOT NULL,                 -- 实际发出去的型号
    model_echo     TEXT,                                 -- 厂商回的型号（可能被网关降级）
    prompt_kind    TEXT        NOT NULL,                 -- table | text
    image_path     TEXT        NOT NULL,                 -- 发出去的就是这张 PNG
    image_bytes    INTEGER,
    ok             BOOLEAN     NOT NULL,
    err_kind       TEXT,
    message        TEXT,
    content        TEXT,                                 -- 模型回的全文（markdown 表）
    rows_parsed    INTEGER,                              -- 从中解析出几行
    unreadable     INTEGER,                              -- 模型自己标「看不清」的格子数
    latency_ms     INTEGER,
    prompt_tokens  INTEGER,
    completion_tokens INTEGER,
    verified_by    TEXT,                                 -- **人工签字前，数字不许进阈值**
    verified_at    TIMESTAMPTZ,
    verify_note    TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_pv_prompt CHECK (prompt_kind IN ('table', 'text')),
    CONSTRAINT ck_pv_ok CHECK (ok OR err_kind IS NOT NULL),
    -- 签字必须留下是谁签的和什么时候签的，缺一不可
    CONSTRAINT ck_pv_verified CHECK (
        (verified_by IS NULL AND verified_at IS NULL)
        OR (verified_by IS NOT NULL AND verified_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_pv_page ON doc_page_vision (file_id, page_no, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_pv_model ON doc_page_vision (model_name);

COMMENT ON TABLE doc_page_vision IS
  '多模态读图的逐页结果。**一页一模型一行，永不覆盖**——'
  '同一页用两个模型读是两份独立证据，两份都在才能互相印证；'
  '重读一次加一行（attempt 递增），这样「上次读出来的是什么」永远查得到。';

COMMENT ON COLUMN doc_page_vision.image_path IS
  '**发出去的就是这张图。** 它由 app/ocr.py 渲在 var/ocr/ 下（dpi=170），'
  '不是客户目录里的文件。记在这里是为了能回答「哪一页的图离开过本系统」。';

COMMENT ON COLUMN doc_page_vision.unreadable IS
  '模型自己标成看不清的格子数（提示词里要求用 ? 标记，不许猜）。'
  '**这个数比 content 更重要**：一个把所有格子都填满的回答，'
  '要么是真读清楚了，要么是把看不清的编出来了，而这两者在文本上长得一模一样。'
  '所以提示词必须给模型一条「可以说不知道」的出路，再用这个数把它量出来。';

COMMENT ON COLUMN doc_page_vision.verified_by IS
  '人工签字。**空值意味着：这页的数字不许进 standard_limit、不许进任何规则阈值。**'
  '模型读图不自动等于人眼读图——它同样会把看不清的格子编出来，而且编得通顺。'
  '项目可信度三档里只有原生文本与人眼读图能进硬规则，这一条不因为换了模型而改变。';

COMMENT ON COLUMN doc_page_vision.prompt_tokens IS
  '出网成本留痕。图按 token 计费，一页表格几千 token，重读几次就不是小钱。';
