-- ============================================================================
-- 0015 · 只读文件库（资料库快照）
-- ----------------------------------------------------------------------------
-- 这一步解决的问题是：**规则是从哪堆资料里抽出来的，那堆资料后来变没变。**
-- 没有快照，「规则抽取」就只能对着一个说不清边界、说不清版本的目录工作；
-- 等资料被人加了一份、删了一份、换了一版，谁也答不上来"当初抽的是哪一版"。
--
-- 为什么**另起两张表**，不扩 policy_doc：
--   policy_doc 的口径是「上传进系统的制度文件副本」——stored_path 与 file_hash
--   都是 NOT NULL，因为那条链路要把文件复制进来、按内容去重、再 OCR 成条款。
--   本表的口径是「磁盘上某个文件夹里**原地**存在过的文件」：不复制、不改名、
--   不写回，只登记路径与指纹。两种口径混进一张表，NOT NULL 约束要么放松、
--   要么塞假值，而放松之后就再也分不清一行到底是副本还是引用。
--   边界讲死：doc_lib_* = 磁盘只读快照；policy_doc = 正式入库的制度文件。
--   以后从快照里挑文件正式入库，是**另写一条登记**，不是把这张表改个状态。
--
-- 为什么**只读**要写进表注释而不只写在代码里：
--   这是对客户资料目录动手的唯一一道约束。代码可以被改，注释留在库里，
--   下一个人改 scan 之前至少看得到这条纪律。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 纳入的文件夹（根）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_lib_root (
    root_id      TEXT PRIMARY KEY,
    abs_path     TEXT        NOT NULL UNIQUE,     -- 绝对路径，规范化后存
    label        TEXT        NOT NULL,            -- 显示名，默认取文件夹名
    enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
    note         TEXT,                            -- 如「与已有文件夹嵌套」
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_scan_at TIMESTAMPTZ
);

COMMENT ON TABLE doc_lib_root IS
  '只读文件库的根目录登记。**本系统绝不向这些目录写入任何东西**，连临时文件都不落：'
  '客户的资料目录是证据，不是工作区。「移出文件库」= 删登记，磁盘文件一个不动。';

COMMENT ON COLUMN doc_lib_root.abs_path IS
  '绝对路径。选中的文件夹连同其**所有子文件夹**都算文件库。'
  '两个文件夹嵌套时不拒绝，但扫描按绝对路径去重、归属先到先得，并在 note 里写明——'
  '拒绝会挡掉「先加大目录、再单独标注其中一个子目录」这种正常用法，'
  '而静默算两遍会让文件数对不上。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 扫描登记的文件
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_lib_file (
    file_id       TEXT        PRIMARY KEY,
    root_id       TEXT        NOT NULL REFERENCES doc_lib_root(root_id) ON DELETE CASCADE,
    abs_path      TEXT        NOT NULL UNIQUE,
    rel_path      TEXT        NOT NULL,           -- 相对根的路径，清单按它排序
    file_name     TEXT        NOT NULL,
    ext           TEXT        NOT NULL,           -- 小写，含点
    file_size     BIGINT      NOT NULL,
    mtime         TIMESTAMPTZ NOT NULL,
    fingerprint   TEXT        NOT NULL,           -- 路径+大小+修改时间 的摘要，见下
    parse_state   TEXT        NOT NULL,
    parse_reason  TEXT,                           -- 非「已解析」时必须有话说
    text_chars    INTEGER     NOT NULL DEFAULT 0,
    body          TEXT,                           -- 轻解析取到的正文，供抽取与检索
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    missing       BOOLEAN     NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_doclib_state CHECK (parse_state IN
        ('已解析', '需OCR', '未解析', '空文件', '解析失败', '未扫描'))
);

CREATE INDEX IF NOT EXISTS ix_doclib_file_root  ON doc_lib_file (root_id);
CREATE INDEX IF NOT EXISTS ix_doclib_file_state ON doc_lib_file (parse_state);
CREATE INDEX IF NOT EXISTS ix_doclib_file_rel   ON doc_lib_file (root_id, rel_path);

COMMENT ON COLUMN doc_lib_file.parse_state IS
  '六态，**少一态就会把两件不同的事混成一件**：'
  '已解析=取到正文；'
  '需OCR=PDF 有页但没有文字层（纯扫描件），本机装了 tesseract，这是「下一步能办」'
  '而不是「办不了」，所以必须与「未解析」分开——混了就没人知道该去转格式还是该去跑 OCR；'
  '未解析=格式本身在轻解析阶段取不到（.doc/.xls/.ofd/.wps/.et/.rtf），带原因；'
  '空文件=0 字节，或解析成功但一个字都没有；'
  '解析失败=读取或解包出错（加密、损坏、改过扩展名），带原因；'
  '未扫描=登记了但本次没解析。'
  '**任何一态都不许记成「正文为空」**：下游全文检索据此会以为「制度里没写过这件事」，'
  '漏检比没收录危险得多。';

COMMENT ON COLUMN doc_lib_file.fingerprint IS
  '路径+大小+修改时间 的 sha1 前 16 位，用来判断「这份文件变没变」。'
  '**刻意不用内容哈希**：扫描要能随时重跑，读全文件算哈希会让上千份资料的重扫'
  '从几秒变成几分钟，而重扫一慢就没人跑，快照也就不再反映现状。'
  '代价写在明处：内容改了而大小与修改时间都没变的情况扫不出来（改完回改、'
  '或工具刻意保留时间戳）。要抓这种情况用「强制重扫」，它忽略指纹全量重解析。';

COMMENT ON COLUMN doc_lib_file.missing IS
  '文件在上次扫描时还在、这次不见了。**标记而不删行**：删掉就再也答不上来'
  '「这份资料曾经在库里、某条规则是依据它抽的」。文件库是证据链的上游，'
  '上游的行只能作废，不能蒸发。';

COMMENT ON COLUMN doc_lib_file.body IS
  '轻解析取到的正文。OCR 结果**不进这里**：OCR 是加工产物，不是磁盘上的事实，'
  '混进快照会让「这份文件当时是什么样」变得说不清。';
