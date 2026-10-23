-- ============================================================================
-- 0018 · 只读文件库：本机 OCR
-- ----------------------------------------------------------------------------
-- 解决的是文件库里那批「需OCR」：纯扫描件 PDF 没有文字层，抽取根本够不着它们。
-- 实扫 材料\ 时 14 份里有 4 份是这种，而且**恰恰是最核心的 4 份制度**
-- （27 页《差旅管理实施细则》、16 页《业务接待管理办法》、12 页《国资委差旅制度》、
-- 4 页《闽港集团纪23号》）。不接 OCR，这条链就只能抽到边角料。
--
-- 三条口径写进表里，因为它们决定了 OCR 结果能不能用：
--
-- 1. **OCR 文本不进 doc_lib_file.body。**
--    body 是「磁盘上这份文件本来就有的字」，OCR 是我们加工出来的。
--    混在一起，「这份文件当时是什么样」就再也说不清；而且重跑 OCR
--    （换引擎、换 dpi、换语言包）会把快照改掉，快照就不是快照了。
--
-- 2. **逐页存，并且逐页记置信度。**
--    整份一个平均分没有用：27 页里有 3 页糊，平均分照样好看，
--    而规则阈值恰恰可能就在那 3 页上。要能指着页说「这一页不可信」。
--
-- 3. **表格页必须单独标记。**
--    这是本项目付过学费的一条：附件1 住宿费标准表 OCR 后数字全在
--    （460/420/430/310/480/440），但**城市↔职级↔金额的对应关系全乱了**。
--    OCR 丢的不是字，是**结构**。而差旅规则的全部价值就在那个对应关系上。
--    数字对了关系错了，比识别不出来更危险——它会生成一条看起来有据可查的错规则。
--    所以表格页的 OCR 文本只能当线索，不能当依据，界面上必须红标。
-- ============================================================================

CREATE TABLE IF NOT EXISTS doc_lib_ocr (
    id          BIGSERIAL   PRIMARY KEY,
    file_id     TEXT        NOT NULL REFERENCES doc_lib_file(file_id) ON DELETE CASCADE,
    page_no     INTEGER     NOT NULL,
    text        TEXT        NOT NULL DEFAULT '',
    chars       INTEGER     NOT NULL DEFAULT 0,
    mean_conf   NUMERIC(5,2),                  -- tesseract 逐词置信度的均值，0~100
    low_conf_words INTEGER  NOT NULL DEFAULT 0,-- 置信度低于阈值的词数
    word_cnt    INTEGER     NOT NULL DEFAULT 0,
    has_table   BOOLEAN     NOT NULL DEFAULT FALSE,
    table_note  TEXT,
    engine      TEXT        NOT NULL,          -- tesseract 5.4.0.20240606 + chi_sim+eng
    dpi         INTEGER     NOT NULL,
    image_path  TEXT,                          -- 渲出来的 PNG，见下
    ocr_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (file_id, page_no)
);

CREATE INDEX IF NOT EXISTS ix_doclib_ocr_file ON doc_lib_ocr (file_id, page_no);

COMMENT ON TABLE doc_lib_ocr IS
  'OCR 结果，逐页一行。**与 doc_lib_file.body 分开存**：body 是磁盘上本来就有的字，'
  '这里是我们加工出来的。混在一起，重跑 OCR 就会改掉快照，快照就不是快照了。';

COMMENT ON COLUMN doc_lib_ocr.image_path IS
  '渲成 PNG 的落盘路径。**一律写在 var/ocr/ 下，绝不写回被扫描的目录**——'
  '只读纪律不止管「不改文件」，也管「不往人家目录里拉屎」。'
  '客户的资料目录是证据，多出一个 .png 就说不清这份资料原本有什么。';

COMMENT ON COLUMN doc_lib_ocr.mean_conf IS
  '**逐页记，不记整份平均**：27 页里 3 页糊，整份平均分照样好看，'
  '而规则阈值可能恰恰就在那 3 页上。要能指着页说「这一页不可信」。';

COMMENT ON COLUMN doc_lib_ocr.has_table IS
  '这一页检测到表格（fitz find_tables）。**表格页的 OCR 文本只能当线索，不能当依据。**'
  '实测：住宿费标准表 OCR 后数字全在（460/420/430/310/480/440），'
  '但城市↔职级↔金额的对应关系全乱了。OCR 丢的不是字是结构，'
  '而差旅规则的全部价值就在那个对应关系上——数字对了关系错了，'
  '比识别不出来更危险。这类页要么人眼读图，要么上多模态模型，不能直接拿去抽规则。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 文件上的 OCR 汇总
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE doc_lib_file
    ADD COLUMN IF NOT EXISTS ocr_state       TEXT,
    ADD COLUMN IF NOT EXISTS ocr_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ocr_chars       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ocr_pages       INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ocr_table_pages INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ocr_mean_conf   NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS ocr_engine      TEXT,
    ADD COLUMN IF NOT EXISTS ocr_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS ocr_note        TEXT;

COMMENT ON COLUMN doc_lib_file.ocr_fingerprint IS
  '跑 OCR 时这份文件的指纹。与当前 fingerprint 不等 = 文件在 OCR 之后又被换过，'
  '**现有 OCR 文本是旧版的**。同 extract_fingerprint 一个道理：用比对算，'
  '不设「OCR 已过期」标志位——标志位忘了清，就会拿旧版文本去抽现行制度的规则。';

COMMENT ON COLUMN doc_lib_file.ocr_state IS
  '已OCR | OCR失败 | 未OCR。**与 parse_state 分开**：parse_state 说的是'
  '「这份文件本身有没有文字层」，是磁盘上的事实；ocr_state 说的是'
  '「我们有没有替它补过字」，是加工状态。合并成一个状态，'
  '就再也分不清「这份制度原本就是可读的」和「这份是我们 OCR 出来的」——'
  '而这两者的可信度差一整档。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 条款的文本来源
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE doclib_clause
    ADD COLUMN IF NOT EXISTS text_source TEXT NOT NULL DEFAULT '原生文本',
    ADD COLUMN IF NOT EXISTS src_conf    NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS from_table_page BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN doclib_clause.text_source IS
  '这条条款的字是哪儿来的：原生文本 | OCR。**必须跟着条款一路传到规则草稿上。**'
  '项目纪律：可信度分三档（原生文本 / 人眼读图 / OCR），'
  '**只有前两档能进硬规则**。OCR 来的数字一律标待人工复核——'
  '阈值错一位，整条硬规则就从保护变成伤害。';
