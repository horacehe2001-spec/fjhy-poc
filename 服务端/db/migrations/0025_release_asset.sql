-- ============================================================================
-- 0025 · 发布管理：文件 → 规则 → 三元本体，以版本的形式资产化
-- ----------------------------------------------------------------------------
-- 用户 2026-10-19 定的四条口径，这张表的形状就是照它来的：
--
--   ① 「三元本体」= 业务本体 / 规则本体 / 流程本体（不是 RDF 三元组）。
--      对应 本体_v0.1.yaml 的三个段：business / rule_schema / process。
--   ② **快照冻结**，不是只记版本指针。发布时把每一层的内容原样抄进
--      release_asset.payload 并算内容 sha256。v1.0 永远是 v1.0——
--      之后改 YAML、重装规则、文件库重扫，都动不了已发布的那一份。
--      只记指针的方案被否了，理由很直接：对外消费场景下，
--      「同一个版本号两次拉到不同内容」是最难查的一类事故。
--   ③ 允许被其他业务消费：只读 API + 可下载资产包，见 app/api/pub.py。
--   ④ **装载只动工作区**：规则资产装载与采纳改的是工作区那一份，
--      已发布的快照与 release_current 一律不动。
--
-- 0005 已经建好了 release / release_item / release_current 三张表，
-- 那一版只覆盖**规则**一层（rule_set_version + ontology_version 两个字段）。
-- 这一版补的是另外两层（文件、三元本体）与「冻结」本身。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- ① release 补三个字段
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE release ADD COLUMN IF NOT EXISTS note          TEXT;
ALTER TABLE release ADD COLUMN IF NOT EXISTS frozen_at     TIMESTAMPTZ;
ALTER TABLE release ADD COLUMN IF NOT EXISTS bundle_sha256 TEXT;

COMMENT ON COLUMN release.frozen_at IS
  '冻结时刻。**与 created_at 分开记**：建草稿和把八层内容抄成快照'
  '不是同一件事，中间可能隔着人工核对。frozen_at 为空的 release 是个空壳，'
  '不许进 reviewing——校验在 app/release_pack.py，不在这里，'
  '因为「八层都抄全了」这件事 CHECK 约束表达不了。';

COMMENT ON COLUMN release.bundle_sha256 IS
  '包级总校验：把各层的 content_sha256 按 layer 排序后拼起来再算一次 sha256。'
  '消费方拿到资产包后可以只比这一个值，不必逐层比。'
  '**它不是 zip 文件的哈希**——zip 里有时间戳，同样内容两次打包字节不同。';

COMMENT ON COLUMN release.note IS '发布说明。为什么发这一版、改了什么，人写的。';

-- ─────────────────────────────────────────────────────────────────────────────
-- ② 八层快照
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS release_asset (
    release_id     TEXT    NOT NULL REFERENCES release(release_id) ON DELETE CASCADE,
    layer          TEXT    NOT NULL,
    ord            SMALLINT NOT NULL,              -- 展示顺序：文件 → 规则 → 三元本体 → 派生
    source_kind    TEXT    NOT NULL,               -- 库 | 文件 | 库+文件
    source_ref     TEXT,                           -- 表名或文件名，查得到出处
    item_count     INTEGER NOT NULL,
    content_sha256 TEXT    NOT NULL,
    payload        JSONB   NOT NULL,
    note           TEXT,
    PRIMARY KEY (release_id, layer),
    CONSTRAINT ck_asset_layer CHECK (layer IN
        ('文件', '规则', '规则参数', '业务本体', '规则本体', '流程本体',
         '事实模型', '规则绑定')),
    CONSTRAINT ck_asset_kind CHECK (source_kind IN ('库', '文件', '库+文件'))
);

CREATE INDEX IF NOT EXISTS ix_release_asset_ord ON release_asset (release_id, ord);

COMMENT ON TABLE release_asset IS
  '一个发布版本的八层快照。**这张表是对外消费的唯一数据源**：'
  '/api/pub/v1 一律读它，不读 rule / ontology_* / doc_lib_file —— '
  '读活表就等于「版本号是假的」，因为活表随时在变。'
  '八层：文件、规则、规则参数、业务本体、规则本体、流程本体、事实模型、规则绑定。'
  '中间三层就是用户说的「三元本体」。';

COMMENT ON COLUMN release_asset.layer IS
  '文件     = doc_lib_file 的清单快照（路径/大小/修改时间/**内容 sha256**/解析状态）。'
  '           **冻结的是清单不是文件本身**：发布包里不塞客户的 14 份原始文档，'
  '           塞的是「用了哪些、内容指纹是什么」。这一条必须写明，'
  '           不然别人以为拿到包就等于拿到源文件。'
  '规则     = rule 表的全部行（含 expr、控制类型、来源条款），引擎执行的就是这些。'
  '规则参数 = 标准表 / 敏感词表 / 必要附件决策表 / 内部单位名单 四份 YAML 原文。'
  '           **它们和规则同等重要**：限额数字改了，规则一个字没动，判定结果照样变。'
  '业务本体 = YAML business 段（实体 / 概念 / 枚举 / 实体关系）。'
  '规则本体 = YAML rule_schema 段（规则的元模型）+ 枚举当前取值。'
  '流程本体 = YAML process 段（节点 / 迁移 / 旁路）。'
  '事实模型 / 规则绑定 = 两份**派生**资产，由 规则资产/gen/ 下的脚本从本体生成。'
  '           派生物也冻：消费方要的是「这一版引擎吃的契约」，不是重新生成一遍。';

COMMENT ON COLUMN release_asset.content_sha256 IS
  '对 payload 的规范化 JSON（sort_keys、ensure_ascii=False、无空格）算的 sha256。'
  '**规范化是必须的**：同一份内容两次 json.dumps 若键序不同，哈希就不同，'
  '那这个字段就证明不了任何事。';

COMMENT ON COLUMN release_asset.payload IS
  '冻结下来的内容本体。**不含文件正文、不含单据业务数据**——'
  '发布的是口径资产（规则与本体），不是客户的业务数据。'
  '这条边界写在这里，省得日后有人往里塞 bill 表。';

-- ─────────────────────────────────────────────────────────────────────────────
-- ③ 拆掉 release_item → rule 的外键
-- ─────────────────────────────────────────────────────────────────────────────
-- 这不是偷懒，是上面第 ②④ 两条口径的必然结果：
--   发布是**快照**，已发布版本的内容在 release_asset 里自成一份；
--   而工作区的 rule 表会被装载脚本整批删了重建。
-- 留着这个外键，等于让「已发布的版本」依赖「工作区还留着那条规则」——
-- 装载时 DELETE FROM rule 会被外键顶回来，于是只剩两条出路：
-- 要么连已发布的 release_item 一起删（发布就不是冻结了），
-- 要么禁止装载（日常迭代卡死）。两条都和用户定的口径冲突。
--
-- 代价说清楚：release_item.rule_id 从此是**弱引用**，可能指向工作区里
-- 已经不存在的规则。这正是「历史版本」该有的样子，不是数据损坏。
-- 要知道某一版到底有哪些规则，看 release_asset 里 layer='规则' 的快照，
-- 不要去 JOIN rule 表。
ALTER TABLE release_item DROP CONSTRAINT IF EXISTS release_item_rule_id_version_fkey;

COMMENT ON TABLE release_item IS
  '一个版本里逐条规则的变更标记（added/modified/retired/unchanged），给人看差异用。'
  '**它不是该版本的内容**：内容在 release_asset(layer=''规则'') 的快照里。'
  '0025 拆掉了它到 rule 表的外键——发布是快照，不能被工作区的删除拖着走。';

COMMENT ON COLUMN release_item.rule_id IS
  '**弱引用**：不保证 rule 表里还有这一行（工作区重装过就可能没了）。'
  '查历史版本的规则内容请走 release_asset 快照。';

-- ─────────────────────────────────────────────────────────────────────────────
-- ④ 消费台账
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS release_access_log (
    id          BIGSERIAL PRIMARY KEY,
    release_id  TEXT REFERENCES release(release_id) ON DELETE SET NULL,
    resource    TEXT        NOT NULL,              -- releases|current|rules|ontology:业务|...|bundle
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    client      TEXT,                              -- 调用方自报（X-Client）或 User-Agent
    bytes       INTEGER,
    http_status SMALLINT    NOT NULL DEFAULT 200
);

CREATE INDEX IF NOT EXISTS ix_release_access ON release_access_log (release_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_release_access_at ON release_access_log (at DESC);

COMMENT ON TABLE release_access_log IS
  '谁在什么时候消费了哪个版本的哪一块。「允许被其他业务消费」这句话'
  '要在页面上能看见才算做到——没有台账，就只能说「接口开了」，'
  '说不出「有没有人用、用的是哪一版」。'
  '**它只记元数据**（版本号、资源名、调用方自报的名字、字节数），'
  '不记请求参数、不记响应内容，也不记 IP：POC 里没有授权体系，'
  '记 IP 属于白收集一份不该存的东西。';

COMMENT ON COLUMN release_access_log.client IS
  '调用方自报的身份（HTTP 头 X-Client），没报就落 User-Agent。'
  '**自报的东西不能当凭据**，只用于看「大概谁在拉」。';
