-- ============================================================================
-- 0016 · 只读文件库：扫描批次与变化台账
-- ----------------------------------------------------------------------------
-- 0015 建的两张表只回答「现在库里有什么」。但真正要用的是下一个问题：
--   **「这次扫描，相对上次，多了什么、少了什么、哪些被改过」**，
--   以及「这些变化里，哪些还没被规则抽取消化掉」。
--
-- 为什么变化要**落台账**，不能只做成扫描接口的返回值：
--   返回值只活在那一次 HTTP 响应里。页面一刷新就没了，更别说「上周那次扫描
--   到底动了哪几份文件」。而规则抽取是**隔天、隔周**才做的事——做的时候要能
--   回头查「从我上次抽取到现在，制度库动过哪些」。所以变化必须是可查的历史。
--
-- 为什么「待抽取」用**指纹比对**算出来，不另设一个 pending 标志位：
--   标志位要靠代码记得去置位和清位，一旦哪条路径忘了清，就会出现
--   「永远待抽取」或者「改了却不提示」。两种都很难发现。
--   指纹比对是自愈的：抽取时把当时的指纹记下来，文件再变，两个值自然不等；
--   没变就自然相等。没有需要维护的状态。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 扫描批次
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_lib_scan (
    scan_id     TEXT        PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    mode        TEXT        NOT NULL,            -- 增量 | 强制重扫 | 仅登记
    root_cnt    INTEGER     NOT NULL DEFAULT 0,
    added       INTEGER     NOT NULL DEFAULT 0,
    updated     INTEGER     NOT NULL DEFAULT 0,
    unchanged   INTEGER     NOT NULL DEFAULT 0,
    missing     INTEGER     NOT NULL DEFAULT 0,
    in_lib      INTEGER     NOT NULL DEFAULT 0,  -- 本次实际走到的文件数
    warnings    TEXT,                            -- 闸门触发等告警，换行分隔
    CONSTRAINT ck_doclib_scan_mode CHECK (mode IN ('增量', '强制重扫', '仅登记'))
);

COMMENT ON TABLE doc_lib_scan IS
  '每次扫描一行。**第一次扫描也记**——没有第一次的基线，第二次就无从说「变了」。';

COMMENT ON COLUMN doc_lib_scan.mode IS
  '增量=按指纹跳过没动的文件；强制重扫=忽略指纹全部重解析（改过解析器后必须用它，'
  '否则文件没动就一份也跑不到，会得到「改了但看不出改了」的假象）；仅登记=不解析正文。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 变化明细
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_lib_change (
    id        BIGSERIAL   PRIMARY KEY,
    scan_id   TEXT        NOT NULL REFERENCES doc_lib_scan(scan_id) ON DELETE CASCADE,
    file_id   TEXT        NOT NULL,              -- 不设外键，见下
    abs_path  TEXT        NOT NULL,
    rel_path  TEXT        NOT NULL,
    file_name TEXT        NOT NULL,
    change    TEXT        NOT NULL,              -- 新增 | 修改 | 消失
    detail    TEXT,                              -- 大小/时间怎么变的、解析状态变没变
    noted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_doclib_change CHECK (change IN ('新增', '修改', '消失'))
);

CREATE INDEX IF NOT EXISTS ix_doclib_change_scan ON doc_lib_change (scan_id);
CREATE INDEX IF NOT EXISTS ix_doclib_change_file ON doc_lib_change (file_id);

COMMENT ON TABLE doc_lib_change IS
  '文件库变化台账：第二次及以后的扫描据此回答「新增/修改/消失了哪些」。'
  '一行就是一次「某份文件在某次扫描里发生了什么」，**只追加，不改写**。';

COMMENT ON COLUMN doc_lib_change.file_id IS
  '**刻意不建外键**到 doc_lib_file。文件夹被「移出文件库」时 doc_lib_file 的行会级联删掉，'
  '但「那天确实多了三份制度」这件事已经发生过，不该跟着消失——'
  '台账是用来回溯的，回溯的前提是它比被回溯的对象活得久。';

COMMENT ON COLUMN doc_lib_change.change IS
  '新增=上次扫描时不在库里；修改=指纹变了（大小或修改时间变了）；'
  '消失=上次在、这次不在了。**消失不等于被删**：可能是改名、移走、或盘没挂上，'
  'detail 里写明当时看到的情况，判断留给人。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 文件上的变化与抽取追踪
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE doc_lib_file
    ADD COLUMN IF NOT EXISTS last_change         TEXT,
    ADD COLUMN IF NOT EXISTS changed_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS extracted_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS extract_fingerprint TEXT;

COMMENT ON COLUMN doc_lib_file.last_change IS
  '最近一次扫描对这份文件的判定：新增 | 修改 | 未变 | 消失。清单上据此打标。';

COMMENT ON COLUMN doc_lib_file.extract_fingerprint IS
  '上一次做规则抽取时这份文件的指纹。与当前 fingerprint 不等 = 文件在抽取之后又被改过，'
  '**抽出来的规则可能已经对不上现在的制度**。'
  '「待抽取」就是这样算出来的（extracted_at 为空，或两个指纹不等），'
  '不设 pending 标志位——标志位忘了清就会永远待抽取或者改了却不提示，两种都很难发现。';
