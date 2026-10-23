-- ============================================================================
-- 0014 · 大模型配置：默认模型 + 连通性测试 + 国内厂商预置
-- ----------------------------------------------------------------------------
-- 为什么**不把型号名写死**在预置表里：
--   2026-10-18 核对各家官方文档时发现，型号名漂移得比想象中快——
--   智谱已到 glm-5.3、Kimi 已到 kimi-k3、MiniMax 的域名从 api.minimax.chat
--   换成了 api.minimaxi.com。把型号写进预置表，半年后这张表就是一张**错的**
--   下拉菜单，而错的下拉菜单比空的下拉菜单坏：用户会以为选了个有效型号。
--   所以预置表只给 base_url + 一个「建议型号」做提示，真正的型号列表
--   由界面现场调厂商的 GET /v1/models 拉回来。
--
-- 为什么默认模型是**按用途各一个**，不是全局一个：
--   本系统有三种用途：vision_read（读票据图）/ rule_judge（判规则）/ extract（抽规则）。
--   读图必须多模态，判规则要长文本推理。全局只允许一个默认，就会出现
--   「拿 DeepSeek 去读发票图片」——它没有视觉能力，结果不是报错而是
--   一段看似合理的编造。那种错误最难发现。
--   所以唯一索引建在 purpose 上，而不是建成全表唯一。
--
-- 为什么测试要**存历史**不只存最近一次：
--   只存最近一次，看到的永远是「现在通不通」。而真正要回答的问题是
--   「从什么时候开始不通的」——跑批出一堆「待人工：未调用模型」时，
--   要能对上是哪天 Key 过期的。所以 llm_test_log 按次追加。
--
-- 为什么 llm_test_log.message 上有 CHECK：
--   厂商返回的 4xx 报文里**经常把 Authorization 头原样回显**。
--   这条错误原文要显示给用户看（不显示就没法排错），于是它成了第 10 个出口。
--   代码里先过一遍 mask，这条 CHECK 是最后一道防线——
--   它触发就说明 mask 漏了，宁可让测试按钮报 500，也不要把 Key 写进库。
-- ============================================================================

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. llm_config 扩字段
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE llm_config
    ADD COLUMN IF NOT EXISTS is_default            BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS preset_id             TEXT,
    ADD COLUMN IF NOT EXISTS last_test_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_test_ok          BOOLEAN,
    ADD COLUMN IF NOT EXISTS last_test_latency_ms  INTEGER,
    ADD COLUMN IF NOT EXISTS last_test_http_status INTEGER,
    ADD COLUMN IF NOT EXISTS last_test_err_kind    TEXT,
    ADD COLUMN IF NOT EXISTS last_test_model_echo  TEXT,
    ADD COLUMN IF NOT EXISTS last_test_message     TEXT;

COMMENT ON COLUMN llm_config.is_default IS
  '该用途下的默认模型。约束由 ux_llm_default_per_purpose 保证：每个 purpose 至多一条。'
  '注意**不是全局唯一**——读图和判规则需要不同能力的模型，'
  '全局唯一会导致拿纯文本模型去读发票图，而那种错误表现为编造而非报错。';

COMMENT ON COLUMN llm_config.last_test_model_echo IS
  '厂商在响应里回的 model 字段。它和 model_name 不一致时要显眼提示：'
  '网关静默降级到别的型号是常见事（配的 glm-5.3，实际服务 glm-4-flash），'
  '不比对就会拿着一个降级后的结果当成配置型号的能力来评估。';

COMMENT ON COLUMN llm_config.last_test_err_kind IS
  '失败归类：dns | tls | timeout | auth | quota | model_not_found | http | parse | no_key。'
  '分类的意义在于「能不能自己解决」——auth/quota 要去厂商控制台，'
  'dns/timeout 是网络，model_not_found 是型号填错。混成一句"调用失败"等于没说。';

-- 每个用途至多一个默认。COALESCE 是必须的：
-- 唯一索引里 NULL 彼此不相等，purpose 为空时会放进来任意多条默认。
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_default_per_purpose
    ON llm_config (COALESCE(purpose, '-')) WHERE is_default;

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. 国内厂商预置
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_provider_preset (
    preset_id     TEXT PRIMARY KEY,
    vendor        TEXT NOT NULL,                  -- 厂商/平台名
    label         TEXT NOT NULL,                  -- 下拉里显示的名字
    base_url      TEXT NOT NULL,
    protocol      TEXT NOT NULL DEFAULT 'openai_compatible',
    suggest_model TEXT,                           -- 仅提示，真实型号从 /models 拉
    modality      TEXT NOT NULL DEFAULT 'text',   -- text | vision | both
    key_hint      TEXT,                           -- Key 的典型形态，帮用户确认粘对了
    model_list    BOOLEAN NOT NULL DEFAULT TRUE,  -- 是否支持 GET /v1/models
    verified_on   DATE,                           -- 核对过官方文档的日期；NULL = 没核对
    source_url    TEXT,
    caveat        TEXT,                           -- 踩坑点，界面上要显示
    sort_no       SMALLINT NOT NULL DEFAULT 99,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT ck_preset_modality CHECK (modality IN ('text', 'vision', 'both')),
    -- http 也放行：财务共享中心很可能要求模型不出内网，
    -- 私有化部署（vLLM / Ollama / 一体机）常是 http://内网IP:端口/v1。
    -- 卡死成 https 就等于逼用户绕开预置自己填，那预置表就没用了。
    -- 代价是 http 下 Key 明文过网——界面上要就这一点给出警告。
    CONSTRAINT ck_preset_base_url CHECK (base_url ~ '^https?://')
);

COMMENT ON COLUMN llm_provider_preset.verified_on IS
  '这条预置的 base_url 最后一次对着厂商官方文档核对的日期。'
  'NULL 表示**没核对过，是凭印象填的**，界面上必须显著标出来。'
  '把没核对的和核对过的混在一个下拉里而不作区分，'
  '用户配不通时会怀疑自己的 Key，而真正的原因是我给的地址是错的。';

COMMENT ON COLUMN llm_provider_preset.caveat IS
  '这家的特殊之处。例：火山方舟不能直接填型号名，要先在控制台建推理接入点'
  '再把接入点 ID（ep-xxxx）当 model 填。不写出来，用户会反复试型号名都 404。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. 测试记录（按次追加）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_test_log (
    id            BIGSERIAL PRIMARY KEY,
    tested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    config_id     TEXT REFERENCES llm_config(config_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,                  -- models（列型号）| chat（真发一句）
    ok            BOOLEAN NOT NULL,
    http_status   INTEGER,
    latency_ms    INTEGER,
    model_echo    TEXT,
    reply_excerpt TEXT,                           -- 回复前 200 字，证明真的通了
    err_kind      TEXT,
    message       TEXT,
    tested_by     TEXT,
    CONSTRAINT ck_llm_test_kind CHECK (kind IN ('models', 'chat')),
    -- 最后一道防线，见文件头注释
    CONSTRAINT ck_llm_test_no_key CHECK (
        (message IS NULL OR message NOT LIKE '%sk-%') AND
        (reply_excerpt IS NULL OR reply_excerpt NOT LIKE '%sk-%')
    )
);

CREATE INDEX IF NOT EXISTS ix_llm_test_log_config
    ON llm_test_log (config_id, tested_at DESC);

COMMENT ON TABLE llm_test_log IS
  '每次点「测试」都追加一条。只留最近一次就只能回答「现在通不通」，'
  '而排错时要回答的是「从哪天开始不通的」——跑批出一堆'
  '「待人工：未调用模型」时，要能对上是哪天 Key 过期的。';

COMMENT ON COLUMN llm_test_log.reply_excerpt IS
  '模型回复的前若干字。测试不该只看 HTTP 200——'
  '网关在 Key 无效时也可能回 200 带一个错误体。要看见模型真的说了话。';
