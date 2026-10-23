-- ============================================================================
-- 0034 · 制度文件库：客户端上传 + 按领域/实例分区存储
-- ----------------------------------------------------------------------------
-- 用户决策（2026-10-21，四问四答）：
--   ① 旧的「浏览服务器本机目录 → 纳入为只读根」**保留**，与上传双模式并存。
--   ② 上传文件按领域/实例**落盘隔离**，并且**文件清单真的按当前实例过滤**。
--      这改掉了 0032 的「本期只归属不过滤」口径——**仅限 /api/doclib/* 这一栏**，
--      其余栏目（抽取、本体、发布）仍是全库数字，见下「过滤边界」。
--   ③ 同内容（sha256 相同）重复上传**拒绝**；同名不同内容**存为新版本**。
--   ④ 托管目录里的文件**允许真删**，但已产出条款/规则明细/已签字读图的**拒删**。
--
-- 【为什么不另起一套表装上传的文件】
--   下游全部挂在 doc_lib_file 上：doclib_clause(0017)、doc_lib_ocr(0018)、
--   doc_page_vision(0027)、doclib_rule_detail(0029) 四张表都是
--   REFERENCES doc_lib_file(file_id) ON DELETE CASCADE，另外还有 9 个 Python 模块
--   直接查它。另起一张「上传文件表」就要把 OCR、读图、条款切分、规则明细
--   四条链路各写第二遍，而两套口径摆在一起，下一个人分不清
--   「一条规则的依据文件」该去哪张表找——0015 已经为 policy_doc 与 doc_lib_*
--   的并存付过这笔学费（前端 2026-10-21 删掉了那个页签就是因为两套口径分不清）。
--
-- 【只读纪律怎么办：不放松，只收窄作用域】
--   0015 把「绝不向被扫描目录写入任何东西」写成了对**所有**根的纪律。
--   本迁移给 doc_lib_root 加一列 kind：
--       引用 = 客户的资料目录，原地扫描。**纪律原样生效，一个字节都不写。**
--       托管 = var/doclib/{domain_id}/{instance_id}/，我们自己的工作区。
--              上传往这里落盘、删除也只删这里——这是工作区，不是证据目录。
--   纪律没有被放松，它的边界被写清楚了。代码里上传与删除的第一行就卡 kind，
--   引用根上调用一律 400；这个闸有单测钉住（test_doclib_upload.py）。
--
-- 【过滤边界：哪些接口按实例过滤，哪些不过滤】
--   过滤：/api/doclib/{roots,files,stats,scan,changes,pending-extract,uploads,storage}
--   不过滤：/api/extract/*、/api/onto/*、/api/pub/*（仍是全库数字）
--   **这不是漏做，是刻意的**：抽取/本体/发布那三栏的现有资产全部属于
--   「审核 / 福建海运集团」，现在就给它们加 WHERE 等于在没有第二份资产的情况下
--   改掉三栏的口径，而那三栏的页面上还没有「当前实例」这个概念。
--   代价必须明写在接口的 scope_note 里，否则下一个人会以为那些数字已经过滤过了。
--
-- 【为什么目录名用 domain_id/instance_id 而不是中文 label】
--   label 是展示名，允许改（0032 的 ON CONFLICT DO NOTHING 就是为了不覆盖人改过的
--   展示名）。label 一改，磁盘目录不会跟着改，abs_path 就对不上了。
--   id 是稳定的，目录用 id；界面上显示 label。
--
-- 【为什么展示名字段仍然叫 label 不叫 name】
--   脱敏按**字段名**选策略，name 一律当人名打星号。本项目已为此踩过四次。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 一、根：托管 / 引用 两种，并归属到实例
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE doc_lib_root
    ADD COLUMN IF NOT EXISTS kind        TEXT,
    ADD COLUMN IF NOT EXISTS instance_id TEXT;

-- 回填：本迁移之前登记的根全部是「浏览本机目录纳入」的引用根，
-- 且现有全部资产属于「审核 / 福建海运集团」（0032/0033 口径）。
UPDATE doc_lib_root SET kind        = '引用' WHERE kind IS NULL;
UPDATE doc_lib_root SET instance_id = 'fjhy' WHERE instance_id IS NULL;

ALTER TABLE doc_lib_root
    ALTER COLUMN kind        SET NOT NULL,
    ALTER COLUMN kind        SET DEFAULT '引用',
    ALTER COLUMN instance_id SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE doc_lib_root ADD CONSTRAINT ck_doclib_root_kind
        CHECK (kind IN ('托管', '引用'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE doc_lib_root ADD CONSTRAINT fk_doclib_root_instance
        FOREIGN KEY (instance_id) REFERENCES domain_instance(instance_id)
        ON DELETE RESTRICT;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 一个实例**最多一个**托管根。没有这条约束，两个托管根会让「上传该落到哪」
-- 取决于查询顺序——那种错不报错，只是偶尔传到另一个目录去，事后查不出来。
CREATE UNIQUE INDEX IF NOT EXISTS ux_doclib_root_managed
    ON doc_lib_root (instance_id) WHERE kind = '托管';

CREATE INDEX IF NOT EXISTS ix_doclib_root_instance ON doc_lib_root (instance_id);

COMMENT ON COLUMN doc_lib_root.kind IS
  '托管 | 引用。**这一列决定「能不能往这个目录写东西」，是只读纪律的边界。**'
  '引用=客户的资料目录，原地递归扫描，0015 的纪律原样生效：绝不写入任何东西，'
  '连临时文件都不落，「移出文件库」只删登记。'
  '托管=var/doclib/{domain_id}/{instance_id}/，客户端上传落在这里，'
  '是本系统自己的工作区，所以允许写入、允许删除文件。'
  '上传与删除接口的第一行就校验这一列，引用根上调用一律 400——'
  '只把纪律写在注释里而不在代码入口卡住，等于没有纪律。';

COMMENT ON COLUMN doc_lib_root.instance_id IS
  '归属实例。托管根由系统按「当前实例」自动创建，一个实例一个；'
  '引用根沿用加入时的当前实例。**/api/doclib/* 这一栏的查询真的按它过滤**'
  '（与 0032 的「只归属不过滤」口径不同，见本迁移头部「过滤边界」）。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 二、文件：来源、内容指纹、上传人
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE doc_lib_file
    ADD COLUMN IF NOT EXISTS instance_id TEXT,
    ADD COLUMN IF NOT EXISTS origin      TEXT,
    ADD COLUMN IF NOT EXISTS sha256      TEXT,
    ADD COLUMN IF NOT EXISTS uploaded_by TEXT,
    ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMPTZ;

UPDATE doc_lib_file SET origin      = '扫描' WHERE origin IS NULL;
UPDATE doc_lib_file f SET instance_id = r.instance_id
  FROM doc_lib_root r WHERE r.root_id = f.root_id AND f.instance_id IS NULL;

ALTER TABLE doc_lib_file
    ALTER COLUMN origin      SET NOT NULL,
    ALTER COLUMN origin      SET DEFAULT '扫描',
    ALTER COLUMN instance_id SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE doc_lib_file ADD CONSTRAINT ck_doclib_file_origin
        CHECK (origin IN ('上传', '扫描'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS ix_doclib_file_instance ON doc_lib_file (instance_id);

-- 同一实例内，同内容只许有一份**在库的**文件。
-- 带 NOT missing 是刻意的：文件被删过（标记 missing）之后，同一份内容允许重新上传，
-- 否则「误删一份就永远传不回来」。
CREATE UNIQUE INDEX IF NOT EXISTS ux_doclib_file_sha
    ON doc_lib_file (instance_id, sha256)
    WHERE sha256 IS NOT NULL AND NOT missing;

COMMENT ON COLUMN doc_lib_file.instance_id IS
  '归属实例。**冗余自 doc_lib_root.instance_id，以根为准**——'
  'scan() 每次都按根的值回写，所以两者不会漂移。'
  '刻意冗余是为了 list_files/stats 不必 join 根表：这两个接口是页面主路径，'
  '而 join 一旦写漏一处，过滤就会静默失效（漏过滤比报错难发现得多）。';

COMMENT ON COLUMN doc_lib_file.origin IS
  '上传 | 扫描。上传=客户端通过 /api/doclib/upload 传到托管目录的；'
  '扫描=在引用目录里原地登记的。**清单上必须能看出一行是哪一种**：'
  '引用目录里的文件可能明天就被客户改了或移走了（只能标 missing），'
  '托管目录里的文件在我们自己手里，两者的可靠性不是一回事。';

COMMENT ON COLUMN doc_lib_file.sha256 IS
  '内容哈希，**只有上传的文件有**，扫描登记的一律为 NULL。'
  '为什么扫描不算：0015 已经把这件事讲死——扫描要能随时重跑，'
  '读全文件算哈希会让上千份资料的重扫从几秒变成几分钟，重扫一慢就没人跑。'
  '上传不一样：那一份字节反正要流过一遍才能落盘，顺手算哈希不多花一次 IO。'
  '它的用处是去重（同一份文件重复上传直接拒绝，不落第二份）。'
  '**不要据此认为「没有 sha256 的文件没校验过」**——那只是说它是引用来的。';

COMMENT ON COLUMN doc_lib_file.uploaded_by IS
  '上传人。本 POC 没有登录体系，取前端传的署名，默认 ui。'
  '留这一列是因为上传是**会改变服务器磁盘状态**的动作，'
  '而「这份制度文件是谁什么时候传进来的」在客户现场一定会被问到。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 三、上传台账：每份文件一行，**被拒的也要留一行**
-- ─────────────────────────────────────────────────────────────────────────────
-- 为什么被拒的也记：客户传了 20 份、库里只多了 17 份，差的那 3 份去哪了，
-- 必须答得上来。只记成功的，就只能靠使用者记住当时弹窗上写了什么——
-- 而弹窗一刷新就没了。
CREATE TABLE IF NOT EXISTS doc_lib_upload (
    id           BIGSERIAL   PRIMARY KEY,
    batch_id     TEXT        NOT NULL,            -- 一次上传动作一个批次号
    instance_id  TEXT        NOT NULL REFERENCES domain_instance(instance_id)
                             ON DELETE RESTRICT,
    root_id      TEXT,                            -- 落入的托管根；被拒时为 NULL
    file_name    TEXT        NOT NULL,            -- 客户端给的原始文件名
    rel_path     TEXT,                            -- 客户端给的相对路径（整文件夹上传时有）
    stored_name  TEXT,                            -- 实际落盘名；同名另存时与 file_name 不同
    file_size    BIGINT,
    sha256       TEXT,
    result       TEXT        NOT NULL,
    reason       TEXT,                            -- result 非「已入库」时必须有话说
    file_id      TEXT,                            -- 入库成功后对应的 doc_lib_file
    uploaded_by  TEXT,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_doclib_upload_result CHECK (result IN
        ('已入库', '同名另存', '同内容已存在', '扩展名不支持', '超过大小上限',
         '文件名不合法', '写盘失败')),
    -- 纪律第 9 条的另一面：判出来了却不说为什么，等于没判。
    CONSTRAINT ck_doclib_upload_reason CHECK (result = '已入库' OR reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_doclib_upload_batch    ON doc_lib_upload (batch_id);
CREATE INDEX IF NOT EXISTS ix_doclib_upload_instance ON doc_lib_upload (instance_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS ix_doclib_upload_file     ON doc_lib_upload (file_id);

COMMENT ON TABLE doc_lib_upload IS
  '上传台账，一份文件一行，**被拒的也留一行**。'
  '客户传了 20 份库里只多 17 份时，差的 3 份必须查得出是「同内容重复」'
  '还是「格式不支持」还是「写盘失败」——这三件事的下一步动作完全不同。';

COMMENT ON COLUMN doc_lib_upload.result IS
  '七态。**同名另存与同内容已存在是两件相反的事，不许合并**：'
  '同名另存=文件名一样但内容不同，已按新版本落盘（制度换了一版，必须留着）；'
  '同内容已存在=字节完全相同，没有落盘（重复点了上传，落第二份只是浪费磁盘'
  '并让「这条规则依据哪一份」多出一个无意义的分叉）。'
  '合成一个「重复」，使用者就不知道自己刚传的新版本到底进没进库。';

COMMENT ON COLUMN doc_lib_upload.stored_name IS
  '实际落盘的文件名。同名不同内容时形如「差旅管理办法(2).pdf」。'
  '**不覆盖原文件**：覆盖会让已经抽出的规则指向一份不存在的原文，'
  '而证据链断在这里基本查不出来——页面上那条规则看着仍然有出处。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 四、扫描批次也归属实例
-- ─────────────────────────────────────────────────────────────────────────────
-- 扫描默认只扫当前实例的根，所以批次本身也有实例归属。
-- 不加这一列的话，「变化台账」那一页切换实例后会显示别的实例的扫描批次。
ALTER TABLE doc_lib_scan
    ADD COLUMN IF NOT EXISTS instance_id TEXT;

UPDATE doc_lib_scan SET instance_id = 'fjhy' WHERE instance_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_doclib_scan_instance
    ON doc_lib_scan (instance_id, started_at DESC);

COMMENT ON COLUMN doc_lib_scan.instance_id IS
  '这次扫描扫的是哪个实例的根。**允许为 NULL**：显式传 root_ids 跨实例扫时'
  '算不出单一归属，这时如实留空，不挑一个填上去。'
  '历史批次（本迁移之前的）回填为 fjhy——那时全部资产都属于它。';
