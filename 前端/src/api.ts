/**
 * 后端接口客户端。
 *
 * 两条纪律：
 *
 * 1. **一律用相对路径 `/api`**，由 vite 代理到后端。写死 host 之后换环境就要改代码，
 *    而改代码部署是最容易忘的一步。
 *
 * 2. **错误不吞。** 请求失败时抛出带状态码与后端 detail 的错误，由页面显示出来。
 *    前端把错误吞掉（catch 之后 return []）是最常见的隐藏故障方式：
 *    页面上看着是「没有数据」，实际是「接口挂了」——这两件事必须分得开。
 */

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  return handle<T>(path, resp)
}

/**
 * 上传文件（multipart）。**刻意不走 request()**：
 * 那里写死了 `Content-Type: application/json`，而 multipart 的
 * Content-Type 必须由浏览器自己生成——它要往里塞一个 boundary，
 * 手写一个没有 boundary 的 multipart 头，后端解不出任何字段，
 * 报的还是一个和上传无关的 422。
 */
async function upload<T>(path: string, form: FormData): Promise<T> {
  const resp = await fetch(path, { method: 'POST', body: form })
  return handle<T>(path, resp)
}

async function handle<T>(path: string, resp: Response): Promise<T> {
  const text = await resp.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = { detail: text.slice(0, 300) }
  }
  if (!resp.ok) {
    const d = (body as { detail?: string })?.detail ?? text.slice(0, 300)
    throw new ApiError(resp.status, d || resp.statusText)
  }
  // 后端的脱敏中间件会在每个 JSON 响应上打这个头。
  // 没有它说明响应绕过了中间件——联调时靠它一眼看出来，不要静默忽略。
  if (!resp.headers.get('x-fjhy-masked')) {
    console.warn(`[脱敏] ${path} 的响应没有 x-fjhy-masked 头，可能绕过了脱敏中间件`)
  }
  return body as T
}

function qs(params: Record<string, unknown>): string {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') u.set(k, String(v))
  }
  const s = u.toString()
  return s ? `?${s}` : ''
}

// ── 类型 ─────────────────────────────────────────────────────────────────────
// ── 栏目五 发布管理 ──────────────────────────────────────────────────────────
// 文件 → 规则 → 三元本体，以版本的形式资产化（2026-10-19 用户口径）。
// 三元本体 = 业务 / 规则 / 流程；外加一层带注释的本体源文件原文。
export interface RelLayer {
  layer: string
  ord: number
  source_kind: string
  source_ref: string | null
  item_count: number
  content_sha256: string
  is_triple: boolean
  changed?: boolean
  alias?: string | null
  note: string | null
}
export interface RelGate { gate: string; ok: boolean; detail: string; how: string }
export interface RelDiff {
  added: number; modified: number; retired: number; unchanged?: number
  layers_changed: string[]; base: string | null; note?: string
}
export interface RelWorkspace {
  layers: RelLayer[]
  triple: string[]
  bundle_sha256: string
  diff: RelDiff
  gates: RelGate[]
  /** 引擎实际在用的那一版。is_pointer=false 表示没人点过「切为当前」，
   *  是按「最新已发布版」自动认的（why 里有原话）。 */
  current: {
    release_id: string; rule_set_version: string; published_at: string | null
    is_pointer: boolean; why: string
  } | null
  suggest_version: string
  note: string
}
export interface RelRow {
  release_id: string; rule_set_version: string; ontology_version: string; status: string
  rule_count: number; compiled_count: number; executable_count: number
  diff_summary: RelDiff | null
  created_by: string | null; created_at: string; frozen_at: string | null
  reviewed_by: string | null; reviewed_at: string | null; published_at: string | null
  rolled_back_at: string | null; rollback_reason: string | null
  note: string | null; bundle_sha256: string | null
  is_current: boolean; is_effective: boolean; layers: number; accesses: number
}
export interface RelDetail extends RelRow {
  assets: RelLayer[]
  items: { rule_id: string; change: string }[]
}
export interface RelAccess {
  id: number; release_id: string | null; resource: string; at: string
  client: string | null; bytes: number | null; http_status: number
  rule_set_version: string | null
}
export interface PubIndex {
  /** service_name 不是 name：叫 name 会被人名脱敏吃成「福****…」 */
  service_name: string
  triple_ontology: string[]
  layers: { layer: string; ord: number; alias: string | null; note: string }[]
  endpoints: { path: string; what: string }[]
  rules_of_the_road: Record<string, string>
}

// ── 系统 · 智能体工作台 ──────────────────────────────────────────────────────
// 契约在 config/agents/*.yaml，状态在 agent_state，痕迹在 agent_signal_log /
// cycle_log，编排分散在四条链上。这一组类型是把这四处拼成一页用的。
// **字段一律 agent_name / metric_name / param_name，不叫 name**：
// 叫 name 会被人名脱敏吃成「素***」。
export interface AgentRow {
  agent_id: string
  agent_name: string
  agent_version: string
  layer: string
  execution_strategy: string
  triggers: string[]
  heartbeat_seconds: number | null
  listens: string[]
  emits: string | null
  reads: string[]
  writes: string[]
  params: number
  forbidden: number
  no_judgement: boolean
  chain_id: string | null
  chain_name: string | null
  has_state: boolean
  enabled: boolean | null
  last_beat_at: string | null
  signals: number
  last_signal_at: string | null
  cycles: number
  last_cycle_at: string | null
  desc: string | null
}
export interface AgentFact { fact: string; why: string }
export interface AgentOverview {
  items: AgentRow[]
  totals: {
    agents: number; by_layer: Record<string, number>; with_state: number
    with_trace: number; checks: number; failed: number
  }
  facts: AgentFact[]
}
export interface AgentDetail {
  agent_id: string; agent_name: string; agent_version: string; layer: string
  execution_strategy: string; desc: string | null
  chain_id: string | null; chain_name: string | null
  sdafi: {
    S: { tables: string[]; select: string | null; snapshot: boolean | null }
    D: {
      kind: string | null; conclusion_values: string[]; pending_counts_as: string | null
      pending_why: string | null; no_judgement: boolean; no_judgement_why: string | null
    }
    A: {
      writes: string[]; signal: Record<string, string>
      idempotency: string | null; idempotency_why: string | null
    }
    F: { metrics: { metric_name: string; desc: string; source: string }[] }
    I: {
      params: { param_name: string; default: unknown; lo: unknown; hi: unknown
                why: string | null }[]
      forbidden: string[]
    }
  }
  triggers: Record<string, Record<string, unknown>>
  state: {
    has_state: boolean; enabled: boolean | null; params: Record<string, unknown>
    params_from: string; last_beat_at: string | null; last_cycle_id: string | null
  }
  contract_file: { file: string; lines: number; comment_lines: number; text: string }
  note: string
}
export interface AgentChain {
  chain_id: string; chain_name: string; kind: string; page: string; note: string
  agents: { agent_id: string; agent_name: string; layer: string; kind: string
            desc: string }[]
  status?: string; pct?: number; job_id?: string | null; elapsed?: number | null
  /** signals = 库里的台账累计；signals_in_job = 本次作业内存里投了几条。两者不同 */
  signals?: number; signals_in_job?: number; cycles?: number; error?: string | null
  log_errors?: { from: string; channel: string; error: string }[]
}
export interface AgentSignalRow {
  id: number; emitted_at: string; signal_id: string; cycle_id: string; agent_id: string
  action: string; target: string | null; payload: Record<string, unknown> | null
  written_tables: Record<string, number> | null; idempotency_key: string; masked: boolean
}
export interface AgentCycleRow {
  cycle_id: string; agent_id: string; agent_version: string
  rule_set_version: string | null; ontology_version: string | null
  subject_id: string | null; trigger_kind: string; trigger_reason: string | null
  decision: string | null; status: string; error: string | null
  phase_timings_ms: Record<string, number> | null
  prompt_tokens: number | null; completion_tokens: number | null
  started_at: string; finished_at: string | null
}

// ── 多模态读图（OCR 丢结构的那几页）────────────────────────────────────────
export interface VisionPage {
  file_id: string; file_name: string; page_no: number; chars: number
  mean_conf: number | null; has_table: boolean; image_path: string; read_times: number
}
export interface VisionRead {
  model_name: string; attempt: number; ok: boolean; content: string | null
  rows_parsed: number | null; unreadable: number | null; latency_ms: number | null
  prompt_tokens: number | null; completion_tokens: number | null; created_at: string
}
export interface VisionCross {
  file_id: string; page_no: number; models: number
  agree: boolean | null; verdict: string; note: string
  a?: { model_name: string; numbers: number; rows: number | null; unreadable: number | null }
  b?: { model_name: string; numbers: number; rows: number | null; unreadable: number | null }
  only_a?: string[]; only_b?: string[]
}

// ── 制度文件库 ───────────────────────────────────────────────────────────────
// 文件有两种进库方式（迁移 0034）：
//   托管  客户端上传 → 服务器的 var/doclib/{领域id}/{实例id}/，可写可删
//   引用  服务器上已有的目录 → 原地只读扫描，**一个字节都不写**
// 页面上这两种必须分得开：引用目录里的文件明天可能被客户改了或移走了。
export interface DlRoot {
  root_id: string; abs_path: string; label: string; enabled: boolean
  note: string | null; added_at: string; last_scan_at: string | null
  files: number; exists: boolean
  /** 托管 | 引用 —— 决定能不能往这个目录写东西，是只读纪律的边界 */
  kind: string
  /** kind === '托管'。后端算好给，前端不要自己按 kind 推（两处推法迟早分叉） */
  writable: boolean
  instance_id: string
  instance_label: string | null
  domain_label: string | null
}
export interface DlBrowse {
  path: string | null; parent: string | null
  drives: { name: string; path: string }[]
  dirs: { name: string; path: string }[]; files: number
}
export interface DlAddResult {
  path: string; ok: boolean; reason?: string; root_id?: string; note?: string | null
}
export interface DlRecognize {
  /** 可识别 | 部分可识别 | 待识别 | 待重跑 | 需转格式 | 不可识别 | 无内容 */
  label: string
  /** 下一步该走哪条路：原生文本 / 本机 OCR / OCR + 需多模态 / 转存后重扫 */
  how: string
  cls: string
  note: string
}
export interface DlFile {
  file_id: string; root_id: string; abs_path: string; rel_path: string
  file_name: string; ext: string; file_size: number; mtime: string
  parse_state: string; parse_reason: string | null; text_chars: number
  first_seen_at: string; last_seen_at: string; missing: boolean
  ocr_state: string | null; ocr_pages: number; ocr_chars: number
  ocr_table_pages: number; ocr_mean_conf: number | null
  ocr_note: string | null; ocr_at: string | null
  /** 制度 | 范本 | 单据 | 其他 —— 文件级属性，按文件名判，判不出是「其他」 */
  doc_kind: string | null
  /** 文号，如 闽港财智〔2025〕20号。只从文件名取，取不到就是 null，不猜 */
  doc_number: string | null
  doc_title: string | null
  issued_year: number | null
  recognize: DlRecognize
  /** 上传 | 扫描 */
  origin: string
  /** 所在根的类型：托管 | 引用 */
  root_kind: string
  root_label: string
  /** 能不能删。**只看根的类型**，不要按 origin 推 */
  deletable: boolean
  /** 同一实例里同名的在库文件有几份。>1 说明有同名的另一个版本 */
  name_copies: number
  /** 内容 sha256。**只有上传的文件有**，扫描登记的是 null——
   *  扫描刻意不读全文件算哈希（0015：重扫要快到可以随时重跑）。
   *  null 不代表「没校验过」，只代表它是引用来的。 */
  sha256: string | null
  uploaded_by: string | null
  uploaded_at: string | null
  instance_id: string
}
/** 上传台账的一行。**被拒的也有行**，这是台账存在的主要理由。 */
export interface DlUploadItem {
  file_name: string
  rel_path: string | null
  stored_name: string | null
  file_size: number | null
  sha256: string | null
  /** 已入库 | 同名另存 | 同内容已存在 | 扩展名不支持 | 超过大小上限 |
   *  文件名不合法 | 写盘失败 */
  result: string
  reason: string | null
  file_id: string | null
}
export interface DlUploadResult {
  batch_id: string
  instance_id: string; instance_label: string; domain_label: string
  root_id: string; root_label: string; dir: string
  items: DlUploadItem[]
  ok_count: number
  rejected: DlUploadItem[]
  scan: DlScanResult | null
  note: string
}
export interface DlStorage {
  dirs: { root_id: string; abs_path: string; label: string; instance_id: string
          instance_label: string | null; domain_label: string | null
          files: number; bytes: number; size_text: string; exists: boolean }[]
  total_files: number; total_bytes: number
  root_dir: string; staging_dir: string
  staging_stale: { file_name: string; bytes: number }[]
  limits: { max_upload_bytes: number; max_upload_bytes_text: string
            max_upload_files: number; allowed_ext: string[] }
  note: string
}
export interface DlOcrPage {
  page_no: number; chars: number; mean_conf: number | null
  low_conf_words: number; word_cnt: number; has_table: boolean
  table_note: string | null; engine: string; dpi: number
  image_path: string | null; ocr_at: string; preview: string
}
export interface DlOcrResult {
  engine: string; variant: string; dpi: number
  tried: number; done: number; failed: number; elapsed_s: number
  items: { file_id: string; file_name: string; ok: boolean; pages?: number
           chars?: number; mean_conf?: number | null; table_pages?: number
           empty_pages?: number; note?: string | null; error?: string }[]
  note: string
}
export interface DlOcrEngine {
  ok: boolean; exe: string | null; version: string | null; langs: string[]
  tessdata: string | null; variant: string | null; lang_arg?: string
  dpi?: number; reason: string; note?: string
}
export interface DlChange {
  id: number; scan_id: string; file_id: string; abs_path: string; rel_path: string
  file_name: string; change: string; detail: string | null; noted_at: string
}
export interface DlPending {
  file_id: string; rel_path: string; file_name: string; ext: string
  text_chars: number; parse_state: string; why: string
  last_change: string | null; changed_at: string | null
}
export interface DlScanResult {
  scan_id: string; scanned_at: string; mode: string
  roots: { root_id: string; path: string; files: number; note?: string }[]
  added: number; updated: number; unchanged: number; missing: number; in_lib: number
  changes: DlChange[]; changes_total: number; warnings: string[]; note: string
}
export interface DlStats {
  files: number; bytes: number; missing: number; roots: number; readable: number
  by_ext: { ext: string; n: number }[]
  by_state: { state: string; n: number }[]
  /** 按「能否识别」分组——界面该拿它来筛，不是拿 by_state（那是磁盘事实，OCR 完也不变） */
  by_recognize: { label: string; n: number }[]
  by_kind: { kind: string; n: number }[]
  by_origin: { origin: string; n: number }[]
  instance_id: string | null
  instance_label?: string
  domain_label?: string
  /** 「本栏按实例过滤，其余三栏是全库数字」。**必须显示出来**：
   *  让人以为全站都过滤过，等于埋一个将来说不清的坑（迁移 0034 的过滤边界）。 */
  scope_note?: string
  note: string
}

// ── 智能体协同 ───────────────────────────────────────────────────────────────
export interface AgentPhaseTimings { [code: string]: number }
export interface AgentSnap {
  id: string; agent_name: string; layer: string; kind: string; desc: string
  listens: string | null; emits: string | null; pool_keys: string[]
  /** waiting | running | done | failed | skipped */
  status: string
  phase: string | null
  phase_elapsed?: number
  timings: AgentPhaseTimings
  elapsed: number | null
  outputs: Record<string, string | number>
  note: string | null
  error: string | null
  /** 「规则引擎 · 不消耗」或「已调用 N 次 · 单价待核对」——不编 0 */
  cost: string | null
}
export interface AgentSignal {
  from: string; channel: string; at: number; payload: Record<string, unknown>
}
export interface ExProgress {
  job_id: string | null
  /** idle | running | done | failed */
  status: string
  pct: number
  elapsed: number
  agents: AgentSnap[]
  signals: AgentSignal[]
  measured_baseline: boolean
  totals: { agents: number; agents_done: number }
  error?: string | null
  note?: string
}
export interface ExMeta {
  agents: AgentSnap[]
  phases: { code: string; phase_name: string; cn: string }[]
  bus: string
  note: string
}
/**
 * 「增量抽取」点下去会动什么、会花多少钱。
 *
 * 增量 = **从没抽过的 + 抽过之后又被改过的**（按文件指纹比对），
 * 不是「只取最近一次扫描的变化台账」——那个在「重扫一次没变、
 * 但有文件一直没抽过」时会变成 0 份，而那种漏是静默的。
 *
 * **这个预览是必须显示的，不是装饰。** 增量抽取的最后一步是 AI 草稿，
 * 真出网、真计费；一个只写着「增量抽取」的按钮，点之前使用者不知道
 * 这一下是动 0 份还是 14 份、买 0 条草稿还是 8 条。
 */
export interface ExIncremental {
  files: { file_id: string; file_name: string; rel_path: string; ext: string
           text_chars: number; parse_state: string; origin: string
           /** 从未抽取 | 抽取后文件又被改过 */
           why: string
           last_change: string | null; changed_at: string | null }[]
  total: number
  file_ids: string[]
  by_why: { why: string; n: number }[]
  /** 取不到正文的文件：抽取覆盖不到它们，和「增量」是两件事 */
  blocked: { parse_state: string; n: number }[]
  blocked_total: number
  ai_batch_limit: number
  /** 已有条款的那几份里的候选数。**这是下限**，见 candidates_unknown_files */
  candidates_known: number
  /** 还没切过条款的文件名：它们的候选数要切完才知道，**不编总数** */
  candidates_unknown_files: string[]
  /** 本次最多会对多少条候选调模型 */
  will_call_model: number
  cost_note: string
  note: string
}
export interface ExGroup {
  group_key: string; n: number; files: number; kinds: string[]
  has_ocr: boolean; evidence: string
}
export interface ExConflict {
  conflict_id: string; group_key: string; kind: string; detail: string
  left_clause: string; right_clause: string; state: string
  resolved_by: string | null; resolved_at: string | null; resolution: string | null
  created_at: string
}

// -- 栏目三 · 规则本体 · 统一口径 --------------------------------------------
// verdict / state 的档位都比直觉多一档，**那一档是有意的**：
//   verdict 的「判不出」   规则清单的来源写的是类别代号，对不到唯一文件时
//                         既不能判已落地也不能判未落地
//   state 的「编号对不上」 来源写的是款级/表内行/问答序号，不是没有依据
// 前端不许把它们折叠成两档显示——折叠就等于替后端下了它不肯下的断言。
export interface OntoAlign {
  group_key: string
  /** 已落地 | 未落地 | 判不出 */
  verdict: string
  rule_ids: string[]
  match_basis: string
  n_sources: number
  /** 多源印证 | 单一来源 */
  evidence: string
  has_conflict: boolean
  has_ocr: boolean
  aligned_at: string
  last_action: string | null
  adopted_rule: string | null
  sources?: OntoSource[]
  suggest?: {
    name: string
    control_type: string | null
    control_why: string
    numbers: string[]
    blocked_by_conflict: boolean
  }
}
/** 本体工场的一步。**字段叫 step 不叫 name**：脱敏按字段名选策略，
 *  name 会被当人名打星（实测八步名字全变成「素***」）。 */
export interface WsStep {
  no: number
  step: string
  /** 智能体 | 智能体×3 | 装载器 | 人。**这一栏不许省**：
   *  省了页面就变成「AI 全自动搭本体」的幻觉。 */
  actor: string
  why: string
  covers: string[]
  one_click: boolean
  units: { id: string; agent_name: string; layer: string; kind: string }[]
}
export interface WsSteps {
  steps: WsStep[]
  one_click_last: number
  note: string
}
export interface WsState {
  source: { path: string; lines: number; sha256: string; written_by: string
            in_sync: boolean; db_version: string | null }
  onto: Record<string, number | string | null>
  assets: Record<string, number>
  candidates: Record<string, number>
  unify: Record<string, number>
  gate: OntoGate
  /** 库里这几个数**对不对得上**。和 gate 不是一回事：gate 管「能不能往下装」，
   *  这里管「现在看到的数是不是这一轮算出来的」。
   *  反向覆盖台账会被第③步级联清空而只有第⑦步重建，显示成 0 时
   *  看着像「一条都没覆盖」，实际是「还没算」——两者处置完全相反。 */
  freshness: OntoGateCheck[]
}

/** 装载前那道闸的一项。level 照抄后端：blocker 拦住、warning 只报不拦。
 *  **前端不许把两档折成一档**——warning 折进 blocker 会把能用的功能锁死，
 *  blocker 折进 warning 会让坏数据静默进库。 */
export interface OntoGateCheck {
  /** **叫 check 不叫 name**：脱敏按字段名选策略，name 会被当人名打星。 */
  check: string
  level: 'blocker' | 'warning'
  ok: boolean
  detail: string
}
export interface OntoGate {
  ok: boolean
  checks: OntoGateCheck[]
  reason: string | null
}
export interface OntoSource {
  source_kind: string; confidence: string; excerpt: string
  clause_no: string; title: string | null; numbers: string[]; file_name: string
}
export interface OntoCover {
  rule_id: string; version: number
  /** 有制度依据 | 仅规则表 | 编号对不上 | 查无依据 | 来源未写明 */
  state: string
  group_keys: string[]; basis: string
  rule_name: string; dimension: string; control_type: string; rule_class: string
  src_file: string | null; src_clause: string | null
}
export interface OntoSummary {
  forward: Record<string, number>
  reverse: Record<string, number>
  reverse_total: number
  conflicts: Record<string, number>
  adoptions: Record<string, number>
  rules: number
  adopted_rules: number
  note: string
  /** 反向那一列**是不是这一轮算出来的**。
   *  第③步重建 rule 表会把 onto_rule_cover 级联清空（ON DELETE CASCADE），
   *  只有第⑦步会重建它——实测一次页面上的「装载规则资产」就够了。
   *  那之后这一列全是 0，看着像「82 条规则一条依据都没有」。 */
  freshness: OntoGateCheck[]
}
export interface OntoAdoption {
  adoption_id: string; group_key: string; action: string
  rule_id: string | null; rule_version: number | null
  adopted_by: string; adopted_at: string; reason: string | null
  rule_name: string | null; rule_status: string | null
}
export interface OntoRelations {
  ontology_version: string
  entity_relations: { from_entity: string; to_entity: string; rel_type: string
                      cardinality: string | null; source: string | null; note: string | null
                      from_name: string | null; to_name: string | null }[]
  rule_relations: { rule_id: string; rel_type: string; other_rule: string
                    needs_confirm: boolean; gap_id: string | null; note: string | null
                    rule_name: string | null }[]
  concept_refs: { concept_id: string; concept_name: string | null; role: string
                  n: number; rules: string[] }[]
  multi_source: { group_key: string; rule_ids: string[]; n_sources: number
                  evidence: string; verdict: string; kinds: string[] }[]
  counts: Record<string, number>
  note: string
}
export interface OntoGraph {
  ontology_version: string
  entities: { entity_id: string; entity_name: string; source: string | null; concepts: number
              avail: number | null; gap: number | null; degree: number; isolated: boolean }[]
  entity_edges: { from_entity: string; to_entity: string; rel_type: string
                  cardinality: string | null; source: string | null; note: string | null }[]
  rule_nodes: { rule_id: string; rule_name: string; dimension: string; control_type: string
                rule_class: string; compiled: boolean; demo_executable: boolean }[]
  rule_edges: { rule_id: string; rel_type: string; other_rule: string
                needs_confirm: boolean; gap_id: string | null }[]
  note: string
}
export interface DomainInstance {
  instance_id: string
  domain_id: string
  /** 展示名。后端列名刻意不叫 name——脱敏按字段名会把 name 打成星号 */
  label: string
  is_default: boolean
  sort_no: number
  note: string | null
}

export interface DomainRow {
  domain_id: string
  label: string
  sort_no: number
  enabled: boolean
  note: string | null
  instances: DomainInstance[]
}

export interface DomainCurrent {
  domain_id: string | null
  domain_label: string | null
  instance_id: string | null
  instance_label: string | null
  /** 当前实例不属于当前领域。后端如实暴露，不静默纠正 */
  mismatch: boolean
}

export interface StorageRow {
  table_name: string; what: string; written_by: string
  /** 不清 | 整表重算 | 随规则 | 保留已忽略 | 永不清 —— 这一列是这张表唯一必须看的 */
  on_recalc: string
  why: string; rows: number | null
}
export interface ProcBuild {
  version: string
  source: Record<string, number>
  built_by: string
  drift: { measured_node: string; n: number; bills: number; match: string
           node_id: string | null; onto_name: string | null }[]
  unseen_nodes: { node_id: string; node_name: string }[]
  drift_summary: Record<string, number>
  note: string
}
// -- 栏目三 · 业务本体 · 建立 ------------------------------------------------
// 这一段对应「所有东西都摆在明面上操作」：源文件是谁写的、改了哪一行、
// 装载按钮按下去会发生什么，全部在接口里给出来，不留在命令行和对话里。
export interface OntoCandidate {
  cand_id: string
  /** 实体 | 概念 | 枚举取值 | 流程节点 | 流程迁移 */
  kind: string
  cand_key: string
  cand_name: string
  /** 单据列头 | 真实取值 | 审批记录 | 条款 */
  source_kind: string
  /** 硬证据：出自哪、出现几次。没有出处的候选不进表 */
  evidence: string
  occurrences: number | null
  /** 建议新增 | 层间不一致 | 已在本体 —— 「层间不一致」不指定修哪一层 */
  verdict: string
  existing_ref: string | null
  basis: string
  yaml_target: string | null
  found_at: string
  last_action: string | null
  yaml_line: number | null
}
export interface OntoPatchPreview {
  kind: string
  line: number
  patch: string
  replace_line: boolean
  new_id: string | null
  context_before: string
  context_after: string
  note: string
}
export interface OntoCandAction {
  action_id: string; cand_id: string; cand_kind: string; cand_key: string
  action: string; acted_by: string; acted_at: string; reason: string | null
  yaml_file: string | null; yaml_line: number | null; yaml_patch: string | null
  backup_path: string | null; reloaded: boolean; onto_version: string | null
}
/** 本体**源文件**的信息。原来它也叫 OntoSource，和上面「规则出处」那个重名——
 *  TS 的接口声明合并让两个定义悄悄并成了一个，编译不报错，类型却是错的。 */
export interface OntoSourceFile {
  path: string
  abs_path: string
  lines: number
  bytes: number
  sha256: string
  version: string
  maturity: string | null
  date: string
  sections: { section_name: string; line: number; level: number }[]
  counts: Record<string, number>
  /** 「人工（AI 助手读客户材料后手写）」—— 不许含糊过去 */
  written_by: string
  written_how: string
  derived: { file: string; by: string; note: string }[]
  loaded: {
    version: string | null; source_hash: string | null
    loaded_at: string | null; stats: unknown
    /** 库里记的 hash 与当前文件一致才算没漂移 */
    in_sync: boolean
  }
  in_sync_note: string
}
export interface OntoBuildSummary {
  by_verdict: Record<string, number>
  by_kind: Record<string, number>
  actions: Record<string, number>
  source: OntoSourceFile
  note: string
}
export interface ProcGraph {
  version: string
  nodes: { node_id: string; node_name: string; is_bypass: boolean; note: string | null
           measured: number | null; rank: number | null; off_path: boolean }[]
  edges: { from_node: string; to_node: string; cond: string | null
           blocking: boolean; note: string | null }[]
  note: string
}

// ── 制度抽取 ─────────────────────────────────────────────────────────────────
export interface ExClause {
  clause_id: string; file_id: string; seq: number; clause_no: string
  title: string | null; body: string; snippet: string; char_pos: number
  features: string[]; numbers: string[]; doc_kind: string; covered_by: string[]
  file_name: string; rel_path: string; stale: boolean; is_candidate: boolean
  /** 原生文本 | OCR —— 必须一路传到规则草稿上：只有原生文本和人眼读图能进硬规则 */
  text_source: string
  src_conf: number | null
  from_table_page: boolean
  trust_warning: string
}
export interface ExSplitResult {
  task_id: string; scope: string; files: number; clauses: number; candidates: number
  per_file: { file_id: string; file_name: string; kind: string; clauses: number
              candidates: number; note: string | null }[]
  coverage_blind: { rule_id: string; why: string }[]
  coverage_blind_total: number; note: string
}
export interface ExStats {
  clauses: number; covered: number; candidates: number; stale: number; drafts: number
  by_kind: { doc_kind: string; n: number }[]
  by_feature: { feature: string; n: number }[]
  coverage_blind: number; note: string
}
export interface ExDraft {
  draft_id: string; task_id: string; clause_id: string; rulable: boolean | null
  draft: Record<string, unknown>
  number_check: { ok: boolean | null; suspect: { field: string; value: string }[]; note: string }
  sent_chars: number; truncated: boolean; model: string | null; latency_ms: number | null
  review_state: string; reviewer: string | null; reviewed_at: string | null
  reject_reason: string | null
  clause_no: string; title: string | null; clause_body: string; file_name: string
}
/**
 * 规则明细 —— 这一栏真正的产物：**一行一条规则**，不是条款文本。
 *
 * 一行三块：规则 / 出处 / 分类（标签）。
 * 字段名刻意和后端 rule 表对齐，采纳时不用再翻译一遍；
 * **规则名叫 rule_name 不叫 name**——叫 name 会被按字段名选策略的脱敏
 * 当成人名打星号（这个坑在本项目踩过三次：规则名、本体名、服务名）。
 */
export interface ExDetailTag {
  dim: string; value: string
  /** 凭什么打这个标签（哪一列给的 / 哪个模型给的 / 谁改的）。没有依据的标签等于没有 */
  src: string
  /** true=确定性映射出来的；false=模型给的或待人填 */
  sure: boolean
}
export interface ExDetail {
  detail_id: string; clause_id: string; file_id: string
  /** 列映射 | AI草稿 —— 可信度不是一档 */
  origin: string
  source_kind: string
  rule_name: string
  dimension: string | null; rule_class: string | null; control_type: string | null
  scope_text: string | null; logic_text: string | null; exception_text: string | null
  risk_text: string | null; impl_path: string | null; feasibility: string | null
  algo_code: string | null; algo_type: string | null; configurator_status: string | null
  numbers: string[]; note: string | null
  /** 逐列留痕：表头名 → 这一行的原值。映射对不对，不用翻 xlsx 就能核 */
  col_map: Record<string, string> | null
  /** false = 列没对齐，没有按列映射，只留了原文等人看 */
  mapped_ok: boolean
  map_warning: string | null
  confidence: string; model: string | null; draft_id: string | null
  review_state: string; reviewer: string | null; reviewed_at: string | null
  reject_reason: string | null; adopted_rule_id: string | null
  file_name: string; clause_no: string; clause_body: string; split_strategy: string
  source: { file_name: string; clause_no: string; excerpt: string; confidence: string
            strategy: string; source_kind: string; origin: string; model: string | null }
  tags: ExDetailTag[]
  /** 离进本体还差哪几项（维度/规则类型/控制类型/风险类别） */
  missing_for_adopt: string[]
}
export interface ExDetailStats {
  total: number
  /** 确定性两路（列映射 + 场景拆条）映射成功的条数。**不含 AI 草稿** */
  mapped: number
  flagged: number; adopted: number; into_onto: number
  by: { origin: string; source_kind: string; n: number; mapped: number; adopted: number }[]
  note: string
}
export interface ExAiResult {
  task_id: string; model: string; config_id: string; tried: number; drafted: number
  failed: number; number_suspect: number
  items: { draft_id: string; clause_id: string; clause_no: string; file_name: string
           ok: boolean; rulable: boolean | null; draft: Record<string, unknown>
           number_check: { ok: boolean | null; suspect: { field: string; value: string }[] } }[]
  note: string
}

export interface Page<T> {
  total: number
  page: number
  size: number
  items: T[]
  banner?: DemoBanner
  summary?: RulesSummary
}

export interface DemoBanner {
  demo_bills: number
  real_bills: number
  prefix: string
  note: string
}

export interface NavCount {
  key: string
  label: string
  order: number
  desc: string
  count: number
}

export interface RulesSummary {
  total: number
  compiled: number
  executable: number
  note: string
  by_dimension: { dimension: string; n: number; c: number; e: number }[]
  by_class: { rule_class: string; n: number }[]
  by_control: { control_type: string; n: number }[]
}

export interface Rule {
  rule_id: string
  /** rule_name 不是 name：叫 name 会被人名脱敏吃成「付**********」 */
  rule_name: string
  dimension: string
  risk_class: string
  rule_class: 'hard' | 'mixed' | 'soft'
  control_type: '禁止' | '警告'
  priority: number
  algo_code: string | null
  algo_type: string | null
  configurator_status: string | null
  compiled: boolean
  compiled_reason: string | null
  demo_executable: boolean
  demo_blocker: string | null
  demo_scope: string | null
  binding_node: string | null
  impl_path: string | null
  feasibility: string | null
  external_table: string | null
  owner: string | null
}

export interface Bill {
  bill_no: string
  doc_type: string
  doc_date: string | null
  biz_type: string
  amount: number | null
  summary: string | null
  is_demo: boolean
  applicant_rank: string | null
  has_remark: boolean
  run_id: string | null
  conclusion: 'pass' | 'review' | 'reject' | null
  hit_count: number | null
  pending_count: number | null
  rule_executed: number | null
  rule_candidates: number | null
  finished_at: string | null
}

/**
 * 单据附件。**attach_type 为 null 表示「还没判」，不是「没有类型」**——
 * 从一张 PNG 判它是出差审批单还是住宿费发票必须读图
 * （必要附件决策表 15 种细类里 7 种 ocr_needed）。
 */
export interface Attachment {
  id: number
  attach_type: string | null
  file_name: string
  pages: number | null
  seq: number | null
  byte_size: number | null
  content_type: string | null
  /** xlsx内嵌 | 演示合成 | 上传 */
  source_kind: string
  source_ref: string | null
  /** A01..A15 细类编码；与 attach_type（EN17 值）不是 1:1，见后端 TYPES 映射 */
  type_code: string | null
  /** 读图判定 | 人工指定 | 演示合成 */
  type_source: string | null
  /** 高 | 中 | 低。**只有「高」会回写 attach_type** */
  type_conf: string | null
  /**
   * 有没有图可取（后端由 stored_path 是否为空推导，不回绝对路径）。
   * **要判「能不能点开」一律用这个，不要用 source_kind。**
   * 踩过：缩略图条件写成 `source_kind === 'xlsx内嵌'`，等演示单配上合成示意图
   * 之后页面照旧显示「无原图」——条件写死在来源上就跟不上数据变化。
   */
  has_file: boolean
}
/** 一次读图判类型的结果。可能是台账留痕但没有采信（applied=false）。 */
export interface AttachmentClassifyItem {
  attachment_id: number
  bill_no?: string
  file_name?: string
  vision_id?: number
  ok: boolean
  err_kind?: string | null
  message?: string | null
  type_code: string | null
  type_label?: string | null
  confidence?: string | null
  reason?: string | null
  note?: string | null
  /** 是否已写进 attachment.attach_type（只有把握「高」才会是 true） */
  applied: boolean
  model?: string
  latency_ms?: number | null
}
export interface AttachmentClassifyResult {
  tried: number
  applied: number
  failed: number
  items: AttachmentClassifyItem[]
  note: string
}
export interface AttachmentClassifyStatus {
  total: number
  typed: number
  untyped: number
  by_type: { type_code: string | null; type_source: string; n: number }[]
  by_confidence: { confidence: string; n: number }[]
  calls: number
  calls_failed: number
  prompt_tokens: number
  completion_tokens: number
  read_but_unresolved: number
  type_menu: { code: string; label: string; en17: string; ocr_needed: boolean }[]
  note: string
}

/** 抽字段台账的进度与成本。判类型之后的第二步，**范围比判类型小**（只四类进）。 */
export interface AttachmentExtractStatus {
  eligible: number
  extracted: number
  applied: number
  pending: number
  calls: number
  calls_failed: number
  prompt_tokens: number
  completion_tokens: number
  by_confidence: { confidence: string; n: number }[]
  note: string
}
/** 一条失败记录。`later_ok` 是关键一列：这张图后来重试成了没。 */
export interface PerceptionFailureRow {
  id: number
  attachment_id: number
  bill_no: string | null
  file_name: string | null
  attempt: number
  err_kind: string | null
  message: string | null
  latency_ms: number | null
  model_name: string | null
  created_at: string
  later_ok: boolean
}
/**
 * 一步读图的失败视图。
 *
 * **`calls_failed` 和 `lost_imgs` 是两个数，不许只显示前一个。**
 * 前者是花过的钱（失败的调用也计费），后者才是待办。实测两步加起来
 * 49 次失败、0 张丢图——只报 49 会让人去重跑一轮已经跑完的读图。
 */
export interface PerceptionFailures {
  stage: string
  table: string
  calls_failed: number
  by_kind: { err_kind: string | null; n: number; sample: string | null }[]
  failed_imgs: number
  recovered_imgs: number
  lost_imgs: number
  all_network: boolean
  recent: PerceptionFailureRow[]
  note: string
}
export interface AttachmentPerception {
  classify: AttachmentClassifyStatus
  extract: AttachmentExtractStatus
  classify_failures: PerceptionFailures
  extract_failures: PerceptionFailures
  chain_note: string
  cost_note: string
}

export interface Finding {
  rule_id: string
  rule_class: string
  control_type: string
  outcome: 'hit' | 'pass' | 'pending' | 'excluded'
  message: string
  evidence: Record<string, unknown>
  clause_ref: Record<string, unknown> | null
  matched_text: string | null
  pending_reason: string | null
  llm_involved: boolean
  llm_rationale: string | null
  needs_human: boolean
}

export interface ReviewReport {
  run: Record<string, unknown> & { run_id: string; bill_no: string; conclusion: string }
  coverage: {
    candidates: number
    executed: number
    excluded: number
    hit: number
    pending: number
    passed: number
    not_applicable: number
    note: string
  }
  findings: {
    hit: Finding[]
    pending: Finding[]
    passed: Finding[]
    not_applicable: Finding[]
  }
  excluded_rules: { rule_id: string; rule_name: string; control_type: string; why: string }[]
  excluded_note: string
  ai_is_advisory: { value: boolean; why: string }
}

// ── 大模型配置 ───────────────────────────────────────────────────────────────
export interface LlmPreset {
  preset_id: string
  vendor: string
  label: string
  base_url: string
  protocol: string
  suggest_model: string | null
  modality: 'text' | 'vision' | 'both'
  key_hint: string | null
  model_list: boolean
  /** 为空 = 地址没对过官方文档。界面上必须标出来，见 LlmConfig.vue 里的说明 */
  verified_on: string | null
  source_url: string | null
  caveat: string | null
  sort_no: number
}

export interface LlmConfig {
  config_id: string
  /** config_name 不是 name：叫 name 会被人名脱敏吃成「阿****」 */
  config_name: string
  provider: string
  preset_id: string | null
  base_url: string
  model_name: string
  api_key_mask: string | null
  temperature: number
  max_tokens: number | null
  enable_thinking: boolean
  concurrency: number
  timeout_s: number
  purpose: string | null
  enabled: boolean
  /** 列表接口也算这条警告：原来只在保存那一刻算，于是存量的错配置永远不报 */
  modality_warning?: string
  is_default: boolean
  last_test_at: string | null
  /** null = 从没测过。**不要把它当成 false** —— 
   *  「没测过」和「测了不通」是两件事，混成一个状态就看不出该去测还是该去修 */
  last_test_ok: boolean | null
  last_test_latency_ms: number | null
  last_test_http_status: number | null
  last_test_err_kind: string | null
  last_test_model_echo: string | null
  last_test_message: string | null
  /** 'ok' | 'none' | 'legacy'。legacy 是旧的单向摘要，取不回明文，必须重填 */
  key_format: 'ok' | 'none' | 'legacy'
}

export interface LlmTestResult {
  kind: 'chat' | 'models'
  ok: boolean
  http_status: number | null
  latency_ms: number | null
  model_echo: string | null
  reply_excerpt: string | null
  err_kind: string | null
  message: string | null
  models: string[]
  persisted?: boolean
  outbound?: { base_url: string; path: string; sent_business_data: boolean
               prompt: string | null }
}

export interface LlmTestRow {
  id: number
  tested_at: string
  kind: string
  ok: boolean
  http_status: number | null
  latency_ms: number | null
  model_echo: string | null
  reply_excerpt: string | null
  err_kind: string | null
  message: string | null
}

// ── 规则资产 ─────────────────────────────────────────────────────────────────
export const api = {
  health: () => request<Record<string, unknown>>('/health'),
  startupReport: () => request<Record<string, unknown>>('/api/startup-report'),

  assetNav: () => request<{ items: NavCount[]; banner: DemoBanner }>('/api/asset/nav'),

  docs: (page = 1, size = 20) =>
    request<Page<Record<string, unknown>>>(`/api/asset/docs${qs({ page, size })}`),
  doc: (id: string) => request<Record<string, unknown>>(`/api/asset/docs/${encodeURIComponent(id)}`),

  extractTasks: () => request<Page<Record<string, unknown>>>('/api/asset/extract/tasks'),
  extractCandidates: (p: Record<string, unknown> = {}) =>
    request<Page<Record<string, unknown>>>(`/api/asset/extract/candidates${qs(p)}`),
  extractPolicy: () => request<Record<string, unknown>>('/api/asset/extract/policy'),

  rules: (p: Record<string, unknown> = {}) => request<Page<Rule>>(`/api/asset/rules${qs(p)}`),
  rulesSummary: () => request<RulesSummary>('/api/asset/rules/summary'),
  rule: (id: string) =>
    request<Record<string, unknown>>(`/api/asset/rules/${encodeURIComponent(id)}`),

  lookupTables: () => request<{ items: Record<string, unknown>[] }>('/api/asset/lookup-tables'),
  lookupTable: (id: string) =>
    request<Record<string, unknown>>(`/api/asset/lookup-tables/${encodeURIComponent(id)}`),

  ontology: () => request<Record<string, unknown>>('/api/asset/ontology'),
  process: () => request<Record<string, unknown>>('/api/asset/process'),

  releases: () => request<{ items: Record<string, unknown>[]; policy: Record<string, unknown> }>(
    '/api/asset/releases'),
  release: (id: string) =>
    request<Record<string, unknown>>(`/api/asset/releases/${encodeURIComponent(id)}`),

  // ── 系统 · 大模型配置 ───────────────────────────────────────────────────────
  // 2026-10-18 从 /api/asset/ 搬到 /api/sys/。老路径后端留了 307 跳转，
  // 但前端直接用新路径——跟着跳转走会多一个来回，而且跳转是给外部链接留的后路，
  // 不是自家代码该依赖的东西。
  llmPresets: () => request<{
    items: LlmPreset[]; verified: number; unverified: number
    purposes: { key: string; label: string }[]; note: string
  }>('/api/sys/llm-presets'),

  llmKeyStatus: () => request<{
    ok: boolean; source: string | null; production_ready: boolean
    note: string; legacy_rows: number; legacy_note?: string
  }>('/api/sys/llm-key-status'),

  llmConfigs: () => request<{
    items: LlmConfig[]
    defaults: Record<string, string>
    purposes: { key: string; label: string; default: string | null }[]
    missing_defaults: string[]
    policy: string; note: string
  }>('/api/sys/llm-configs'),

  saveLlmConfig: (cfg: Record<string, unknown>) =>
    request<{ config_id: string; api_key_mask: string | null; key_saved: boolean
              modality_warning: string; note: string }>('/api/sys/llm-configs', {
      method: 'POST',
      body: JSON.stringify(cfg),
    }),

  setLlmEnabled: (cid: string, on: boolean) =>
    request<{ config_id: string; enabled: boolean; warning: string }>(
      `/api/sys/llm-configs/${encodeURIComponent(cid)}/enabled?on=${on}`,
      { method: 'POST' }),

  deleteLlmConfig: (cid: string) =>
    request<Record<string, unknown>>(
      `/api/sys/llm-configs/${encodeURIComponent(cid)}?confirm=yes`,
      { method: 'DELETE' }),

  // 这两个会**真的出网**到用户配的第三方地址。只在点击时调，不做任何轮询。
  testLlmConfig: (cid: string, kind: 'chat' | 'models') =>
    request<LlmTestResult>(
      `/api/sys/llm-configs/${encodeURIComponent(cid)}/test?kind=${kind}`,
      { method: 'POST' }),

  probeLlm: (cfg: Record<string, unknown>, kind: 'chat' | 'models') =>
    request<LlmTestResult>(`/api/sys/llm-probe?kind=${kind}`, {
      method: 'POST',
      body: JSON.stringify(cfg),
    }),

  setLlmDefault: (cid: string) =>
    request<{ config_id: string; purpose: string; purpose_label: string
              replaced: { config_id: string; name: string } | null
              warning: string; modality_warning: string }>(
      `/api/sys/llm-configs/${encodeURIComponent(cid)}/default`, { method: 'POST' }),

  llmTestHistory: (cid: string) =>
    request<{ items: LlmTestRow[]; total: number }>(
      `/api/sys/llm-configs/${encodeURIComponent(cid)}/tests`),

  // ── 审核端 ─────────────────────────────────────────────────────────────────
  bills: (p: Record<string, unknown> = {}) => request<Page<Bill>>(`/api/bills${qs(p)}`),
  bill: (no: string) => request<Record<string, unknown>>(`/api/bills/${encodeURIComponent(no)}`),

  // ── 单据附件：读图判类型 ───────────────────────────────────────────────────
  // **真出网、真计费**，而且发出去的是真实发票与出差申请单（含姓名、身份证号片段）。
  // 单独一组方法，调用方必须先看 pending/status 再决定要不要点。
  attClassifyPending: (limit = 500) =>
    request<{ items: { id: number; bill_no: string; file_name: string
              seq: number | null; byte_size: number | null; attempts: number }[]
              total: number; note: string }>(
      `/api/attachments/classify/pending${qs({ limit })}`),
  /**
   * 读图判类型。**这一下会真出网、真计费。**
   * attachment_ids 传空数组表示「这次不处理任何一条」，不是「全部」；
   * 不传则按「还没判类型的」顺序挑，一次最多 limit 条（上限 100）。
   */
  attClassify: (body: { attachment_ids?: number[]; limit?: number; config_id?: string } = {}) =>
    request<AttachmentClassifyResult>('/api/attachments/classify',
      { method: 'POST', body: JSON.stringify(body) }),
  attClassifyStatus: () => request<AttachmentClassifyStatus>('/api/attachments/classify/status'),
  /**
   * 附件感知链路：判类型 + 抽字段的进度、成本、失败，一次拉全。
   * **只读，不出网、不计费**——和 attClassify / attExtract 那两个会真花钱的
   * 动作区分开，这个可以随页面自由加载。
   */
  attPerception: (failures = 50) =>
    request<AttachmentPerception>(`/api/attachments/perception${qs({ failures })}`),
  billReviews: (no: string) =>
    request<{ items: Record<string, unknown>[] }>(`/api/bills/${encodeURIComponent(no)}/reviews`),
  review: (runId: string) =>
    request<ReviewReport>(`/api/reviews/${encodeURIComponent(runId)}`),
  submitManual: (runId: string, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/reviews/${encodeURIComponent(runId)}/manual`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  deviation: () => request<Record<string, unknown>>('/api/deviation'),

  // ── AI 智能审核 ────────────────────────────────────────────────────────────
  /**
   * 跑一轮演示审核。**这一下真出网、真计费。**
   *
   * 后端对模型补判设了单轮预算（时间 + 条数，见 config.AI_REVIEW_LLM_*），
   * 所以这个请求是**有时间上界**的，不会再像以前那样一挂二十几分钟。
   * 补判可续跑：`llm.remaining > 0` 时再点一次接着跑。
   *
   * `signal` 用来挂 AbortController。**取消只是不等了，服务端那一条还会跑完**——
   * 这是同步接口，客户端断开不会回滚已经落库的补判结果。
   */
  aiReviewBatch: (signal?: AbortSignal) =>
    request<{ ok: boolean; error?: string
              stats: { total: number; reject: number; review: number
                       pass: number; failed: number; skipped: number }
              llm?: {
                called?: number; ok?: number; failed?: number
                /** 还剩多少条没补判。> 0 就该再点一次 */
                remaining?: number
                /** done | budget | max_calls | no_config */
                stopped_by?: string
                elapsed_s?: number; budget_s?: number; max_calls?: number
                progress_note?: string
                errors?: string[]
                [k: string]: unknown
              }
              results: Record<string, unknown>[] }>(
      '/api/ai-review/batch', { method: 'POST', body: '{}', signal }),
  aiReviewStatus: () =>
    request<{ total: number; reviewed: number; reject: number; review: number
              pass: number; llm_assisted: number
              bills: Record<string, unknown>[] }>('/api/ai-review/status'),

  // ── 审计 / 调试（旁路出口，同样过脱敏）─────────────────────────────────────
  trace: (subjectId: string) =>
    request<Record<string, unknown>>(`/api/audit/trace/${encodeURIComponent(subjectId)}`),
  cycles: (p: Record<string, unknown> = {}) =>
    request<Page<Record<string, unknown>>>(`/api/audit/cycles${qs(p)}`),
  snapshots: (p: Record<string, unknown> = {}) =>
    request<Page<Record<string, unknown>>>(`/api/audit/snapshots${qs(p)}`),
  consistency: () => request<Record<string, unknown>>('/api/audit/consistency'),
  quarantine: () => request<Record<string, unknown>>('/api/audit/quarantine'),

  // ── 栏目一 · 制度文件库 ─────────────────────────────────────────────────────
  // 写盘只发生在**托管目录**（var/doclib/{领域id}/{实例id}/，上传落在那里）。
  // 引用目录（客户在服务器上的资料目录）一个字节都不写——dlAddRoots 只登记路径，
  // 扫描只读文件内容，dlRemoveRoot 只删登记。
  dlBrowse: (path?: string) =>
    request<DlBrowse>(`/api/doclib/browse${qs({ path: path ?? '' })}`),
  dlRoots: () =>
    request<{ items: DlRoot[]; managed: DlRoot[]; instance_id: string
              instance_label: string; domain_label: string
              scope_note: string; note: string }>('/api/doclib/roots'),
  dlAddRoots: (paths: string[], label?: string) =>
    request<{ items: DlAddResult[]; ok_count: number; failed: DlAddResult[]; note: string }>(
      '/api/doclib/roots', { method: 'POST', body: JSON.stringify({ paths, label }) }),

  /**
   * 上传制度文件到服务器的托管目录。
   *
   * `files` 里每个 File 如果带 webkitRelativePath（选整个文件夹时浏览器会给），
   * 就一并传给后端，子目录结构照样保留。**两个数组必须一一对应**，
   * 后端在长度不等时会直接 400——错位会把文件放进别的子目录，落盘之后很难查。
   */
  dlUpload: (files: File[], opt: { subdir?: string; by?: string } = {}) => {
    const form = new FormData()
    for (const f of files) {
      form.append('files', f)
      // 没有相对路径就补文件名，保证两个数组长度始终相等
      form.append('rel_paths', (f as File & { webkitRelativePath?: string })
        .webkitRelativePath || f.name)
    }
    if (opt.subdir) form.append('subdir', opt.subdir)
    if (opt.by) form.append('by', opt.by)
    return upload<DlUploadResult>('/api/doclib/upload', form)
  },
  // 台账每行还 LEFT JOIN 了 doc_lib_file：parse_state / text_chars 是解析结果，
  // missing=true 表示这份文件后来被删了（登记行按「消失」保留，不删行）。
  // 三者都可能为 null —— 被拒的那些行根本没有对应的 file_id。
  dlUploads: (p: Record<string, unknown> = {}) =>
    request<{ items: (DlUploadItem & { batch_id: string; uploaded_at: string
                                       uploaded_by: string | null
                                       parse_state: string | null
                                       text_chars: number | null
                                       missing: boolean | null })[]
              total: number; by_result: { result: string; n: number }[]
              note: string }>(`/api/doclib/uploads${qs(p)}`),
  dlStorage: () => request<DlStorage>('/api/doclib/storage'),
  /** 删托管目录里的一份文件。引用目录的会被后端拒（400），
   *  已产出条款/规则明细/已签字读图的会被拒（409）。 */
  dlDeleteFile: (fileId: string, by?: string) =>
    request<{ file_id: string; file_name: string; removed_from_disk: boolean
              refs: Record<string, number>; note: string }>(
      `/api/doclib/files/${encodeURIComponent(fileId)}${qs({ confirm: 'yes', by })}`,
      { method: 'DELETE' }),
  dlSetRoot: (rootId: string, patch: { enabled?: boolean; label?: string }) =>
    request<Record<string, unknown>>(`/api/doclib/roots/${encodeURIComponent(rootId)}`,
      { method: 'PATCH', body: JSON.stringify(patch) }),
  dlRemoveRoot: (rootId: string) =>
    request<{ files_unregistered: number; note: string }>(
      `/api/doclib/roots/${encodeURIComponent(rootId)}?confirm=yes`, { method: 'DELETE' }),
  dlScan: (body: { root_ids?: string[]; force?: boolean } = {}) =>
    request<DlScanResult>('/api/doclib/scan', { method: 'POST', body: JSON.stringify(body) }),
  dlStats: () => request<DlStats>('/api/doclib/stats'),
  dlFiles: (p: Record<string, unknown> = {}) =>
    request<{ items: DlFile[]; total: number; truncated: boolean; note: string }>(
      `/api/doclib/files${qs(p)}`),
  dlFile: (fileId: string) =>
    request<DlFile & { preview: string; truncated: boolean }>(
      `/api/doclib/files/${encodeURIComponent(fileId)}`),
  dlScans: () => request<{ items: Record<string, unknown>[]; note: string }>('/api/doclib/scans'),
  dlChanges: (p: Record<string, unknown> = {}) =>
    request<{ scan_id: string | null; items: DlChange[]; total: number
              scan?: Record<string, unknown>; note?: string }>(`/api/doclib/changes${qs(p)}`),
  dlOcrEngine: () => request<DlOcrEngine>('/api/doclib/ocr/engine'),
  dlOcrStats: () => request<{ ocr_files: number; ocr_pages: number; ocr_chars: number
                              table_pages: number; mean_conf: number | null
                              pending: number; stale: number; engine: DlOcrEngine }>(
    '/api/doclib/ocr/stats'),
  dlOcr: (body: { file_ids?: string[]; limit?: number; dpi?: number } = {}) =>
    request<DlOcrResult>('/api/doclib/ocr', { method: 'POST', body: JSON.stringify(body) }),
  dlOcrPages: (fileId: string) =>
    request<{ file_id: string; pages: DlOcrPage[]; total: number; note: string }>(
      `/api/doclib/ocr/${encodeURIComponent(fileId)}`),
  dlPending: () =>
    request<{ items: DlPending[]; total: number
              blocked: { parse_state: string; n: number }[]; note: string }>(
      '/api/doclib/pending-extract'),

  // ── 栏目二 · 制度抽取 ───────────────────────────────────────────────────────
  // split 不出网、不计费；ai 真调模型。**刻意是两个接口**：
  // 合成一个，用户点「抽取」时不知道这一下会不会出网、会不会产生费用。
  // 智能体协同：手工启动一次，4 个 Agent 一路跑到底
  /**
   * 启动抽取协同。**默认增量**（only_changed=true）：四个 Agent 全链只处理
   * 「待抽取」的那几份，范围由 L0 一次算定、全链共用。
   * 增量为 0 份时四步都空跑并如实报 0，不会退化成全量，也不会调模型。
   *
   * `only_changed: false` 是**全量重跑**——改过切分策略或正则之后必须这样跑一次，
   * 否则文件没动、指纹没变，一份也跑不到。
   */
  exRun: (body: { only_changed?: boolean } = {}) =>
    request<{ ok: boolean; job_id: string; reason?: string }>(
      '/api/extract/run', { method: 'POST', body: JSON.stringify(body) }),
  exIncremental: () => request<ExIncremental>('/api/extract/incremental'),
  exProgress: () => request<ExProgress>('/api/extract/progress'),
  exMeta: () => request<ExMeta>('/api/extract/agents'),
  // 归并组与口径冲突的客户端方法改叫 onto*（见文件末尾）：
  // 统一口径是本体那一栏的事，接口路径与方法名都跟着归属走。
  exSplit: (body: { file_ids?: string[]; only_changed?: boolean } = {}) =>
    request<ExSplitResult>('/api/extract/split',
      { method: 'POST', body: JSON.stringify(body) }),
  exClauses: (p: Record<string, unknown> = {}) =>
    request<{ items: ExClause[]; total: number; note: string }>(`/api/extract/clauses${qs(p)}`),
  exStats: () => request<ExStats>('/api/extract/stats'),
  exTasks: () => request<{ items: Record<string, unknown>[] }>('/api/extract/tasks'),
  exAi: (body: { clause_ids?: string[]; limit?: number } = {}) =>
    request<ExAiResult>('/api/extract/ai', { method: 'POST', body: JSON.stringify(body) }),
  exDrafts: (p: Record<string, unknown> = {}) =>
    request<{ items: ExDraft[]; total: number; note: string }>(`/api/extract/drafts${qs(p)}`),
  // 规则明细：**列映射那一路不出网、不计费**，和 exAi 刻意分开两个接口。
  exDetailRun: (body: { file_ids?: string[] } = {}) =>
    request<{ table: { mapped: number; flagged: number
                       per_file: { file_id: string; file_name: string; source_kind: string
                                   mapped: number; flagged: number }[]; note: string }
              scenario: { mapped: number; flagged: number; note: string }
              draft: { projected: number; note: string }; total: number }>(
      '/api/extract/details', { method: 'POST', body: JSON.stringify(body) }),
  exDetails: (p: Record<string, unknown> = {}) =>
    request<{ items: ExDetail[]; total: number; note: string }>(
      `/api/extract/details${qs(p)}`),
  exDetailStats: () => request<ExDetailStats>('/api/extract/details/stats'),
  exDetailTags: () => request<{ items: { tag: string; n: number }[]; note: string }>(
    '/api/extract/details/tags'),
  exDetailReview: (detailId: string, state: string, reviewer: string, reason?: string) =>
    request<Record<string, unknown>>(
      `/api/extract/details/${encodeURIComponent(detailId)}/review`,
      { method: 'POST', body: JSON.stringify({ state, reviewer, reason }) }),
  exDetailTagAdd: (detailId: string, tag: string, by: string, why: string) =>
    request<Record<string, unknown>>(
      `/api/extract/details/${encodeURIComponent(detailId)}/tags`,
      { method: 'POST', body: JSON.stringify({ tag, by, why }) }),
  exDetailTagDel: (detailId: string, tag: string) =>
    request<Record<string, unknown>>(
      `/api/extract/details/${encodeURIComponent(detailId)}/tags/${encodeURIComponent(tag)}`,
      { method: 'DELETE' }),
  exDetailStorage: () => request<{ items: StorageRow[]; note: string }>(
    '/api/extract/details/storage'),
  exReview: (draftId: string, state: string, reviewer: string, reason?: string) =>
    request<Record<string, unknown>>(
      `/api/extract/drafts/${encodeURIComponent(draftId)}/review`,
      { method: 'POST', body: JSON.stringify({ state, reviewer, reason }) }),
  signals: () => request<Record<string, unknown>>('/api/debug/signals'),
  agentState: () => request<Record<string, unknown>>('/api/debug/agent-state'),

  // -- 栏目三 · 规则本体 · 统一口径 --------------------------------------------
  // 这条链全部是确定性 Agent：**不出网、不计费**，和抽取那条链刻意不同。
  ontoRun: () => request<{ ok: boolean; job_id: string; reason?: string }>(
    '/api/onto/run', { method: 'POST', body: '{}' }),
  ontoProgress: () => request<ExProgress>('/api/onto/progress'),
  ontoMeta: () => request<ExMeta>('/api/onto/agents'),
  ontoSummary: () => request<OntoSummary>('/api/onto/summary'),
  ontoGroups: (p: { verdict?: string; limit?: number } = {}) =>
    request<{ items: OntoAlign[]; note: string }>(`/api/onto/groups${qs(p)}`),
  ontoCover: (p: { state?: string; limit?: number } = {}) =>
    request<{ items: OntoCover[]; note: string }>(`/api/onto/cover${qs(p)}`),
  ontoConflicts: (state?: string) =>
    request<{ items: ExConflict[]; note: string }>(`/api/onto/conflicts${qs({ state })}`),
  ontoResolveConflict: (id: string, state: string, who: string, resolution?: string) =>
    request<Record<string, unknown>>(
      `/api/onto/conflicts/${encodeURIComponent(id)}/resolve`,
      { method: 'POST', body: JSON.stringify({ state, who, resolution }) }),
  ontoPending: () => request<{ items: OntoAlign[]; note: string }>('/api/onto/pending'),
  ontoOptions: () => request<{ dimensions: string[]; rule_classes: string[]
                               control_types: string[]; risk_classes: string[] }>(
    '/api/onto/options'),
  // 采纳会**真的往本体里写一条规则**（status=draft、未编译、不在执行集）。
  // 失败时后端给的是看得懂的 400 文案，直接显示，不要自己编一句「操作失败」。
  ontoAdopt: (body: Record<string, unknown>) =>
    request<{ adoption_id: string; rule_id: string; group_key: string; sources: number
              status: string; note: string }>('/api/onto/adopt',
      { method: 'POST', body: JSON.stringify(body) }),
  ontoDispose: (body: { group_key: string; action: string; by: string; reason: string }) =>
    request<{ adoption_id: string; group_key: string; action: string }>('/api/onto/dispose',
      { method: 'POST', body: JSON.stringify(body) }),
  ontoAdoptions: () =>
    request<{ items: OntoAdoption[]; note: string }>('/api/onto/adoptions'),
  ontoRelations: () => request<OntoRelations>('/api/onto/relations'),
  ontoGraph: () => request<OntoGraph>('/api/onto/graph'),
  ontoStorage: () => request<{ items: StorageRow[]; note: string }>('/api/onto/storage'),

  // -- 栏目四 · 流程本体 -------------------------------------------------------
  procBuild: () => request<ProcBuild>('/api/onto/process/build'),
  procGraph: () => request<ProcGraph>('/api/onto/process/graph'),
  procStorage: () => request<{ items: StorageRow[]; note: string }>('/api/onto/process/storage'),

  // -- 栏目三 · 业务本体 · 建立 ------------------------------------------------
  // 全部确定性 Agent：不出网、不计费。产出是**候选**，采纳是人的动作。
  ontoBuildRun: () => request<{ ok: boolean; job_id: string; reason?: string }>(
    '/api/onto/build/run', { method: 'POST', body: '{}' }),
  ontoBuildProgress: () => request<ExProgress>('/api/onto/build/progress'),
  ontoBuildMeta: () => request<ExMeta>('/api/onto/build/agents'),
  ontoBuildSummary: () => request<OntoBuildSummary>('/api/onto/build/summary'),
  ontoBuildStorage: () => request<{ items: StorageRow[]; current_version: string
                                    versions: { version: string; is_current: boolean
                                                loaded_at: string }[]; note: string }>(
    '/api/onto/build/storage'),
  ontoBuildActions: () =>
    request<{ items: OntoCandAction[]; note: string }>('/api/onto/build/actions'),
  ontoCandidates: (p: { kind?: string; verdict?: string; limit?: number } = {}) =>
    request<{ items: OntoCandidate[]; note: string }>(`/api/onto/candidates${qs(p)}`),
  // 预览只算不写：采纳是改用户手写的源文件，必须先看清要插什么。
  ontoCandPreview: (id: string, yamlTarget?: string) =>
    request<OntoPatchPreview>(
      `/api/onto/candidates/${encodeURIComponent(id)}/preview${qs({ yaml_target: yamlTarget })}`),
  ontoCandAdopt: (id: string, body: Record<string, unknown>) =>
    request<{ action_id: string; new_id: string | null; yaml_line: number; patch: string
              backup: string; sha256: string; reloaded: boolean
              onto_version: string | null; reload_error: string | null; note: string }>(
      `/api/onto/candidates/${encodeURIComponent(id)}/adopt`,
      { method: 'POST', body: JSON.stringify(body) }),
  ontoCandDispose: (id: string, body: Record<string, unknown>) =>
    request<Record<string, unknown>>(
      `/api/onto/candidates/${encodeURIComponent(id)}/dispose`,
      { method: 'POST', body: JSON.stringify(body) }),
  ontoSource: () => request<OntoSourceFile>('/api/onto/source'),
  ontoSourceSection: (name: string) =>
    request<{ section_name: string; line_start: number; line_end: number
              truncated: boolean; text: string }>(
      `/api/onto/source/section${qs({ name })}`),
  // 把命令行的 `python -m app.ontology.loader` 搬到明面上。
  // check_only 只比对指纹不写库——两件事刻意分成两个入口。
  ontoReload: (checkOnly = false) =>
    request<{ checked: boolean; file_sha256: string; db_source_hash: string | null
              in_sync: boolean; loaded?: boolean; version?: string
              stats?: unknown; note: string }>(
      `/api/onto/reload${qs({ check_only: checkOnly })}`, { method: 'POST', body: '{}' }),
  // 本体工场第③步。**闸在后端的装载器身上**，这里只是把它的结论拿来显示：
  // 按钮灰不灰、以及灰着的理由——不给理由的灰按钮和没有按钮一样挡路。
  // ── 本体工场：八步一条流水线 ──────────────────────────────────────────────
  // **顺序和「谁干的」都由后端给**，前端不自己排：顺序本身是口径
  //（③ 必须在 ② 之后），前端排一遍就是第二份口径，两份迟早不一样。
  wsSteps: () => request<WsSteps>('/api/onto/workshop/steps'),
  wsState: () => request<WsState>('/api/onto/workshop/state'),
  wsRun: () => request<{ ok: boolean; job_id?: string; reason?: string }>(
    '/api/onto/workshop/run', { method: 'POST', body: '{}' }),
  wsProgress: () => request<ExProgress>('/api/onto/workshop/progress'),
  wsAgents: () => request<ExMeta>('/api/onto/workshop/agents'),
  ontoLoadAssetsReady: () => request<OntoGate>('/api/onto/load-assets/ready'),
  ontoLoadAssets: () =>
    request<{ rules: number; compiled: number; executable: number; release: string
              standards: number; before_rules: number; adopted_kept: number
              release_current_untouched: number; warnings: OntoGateCheck[] }>(
      '/api/onto/load-assets', { method: 'POST', body: '{}' }),
  ontoLoadAssetsNote: () =>
    request<{ command: string; api: string; touches: string; never_touches: string
              gate: string; current: Record<string, number> }>(
      '/api/onto/load-assets-note'),
  // ── 栏目五 发布管理：管理面 ────────────────────────────────────────────────
  // 管理面 /api/publish 会改状态、能看草稿；消费面 /api/pub/v1 只读、
  // 只给已发布版本。**两个前缀刻意分开**，日后给消费面加鉴权不用逐个接口挑。
  relWorkspace: () => request<RelWorkspace>('/api/publish/workspace'),
  relReleases: () => request<{ items: RelRow[]; policy: Record<string, string> }>(
    '/api/publish/releases'),
  relRelease: (id: string) =>
    request<RelDetail>(`/api/publish/releases/${encodeURIComponent(id)}`),
  relAsset: (id: string, layer: string) =>
    request<{ payload: unknown; content_sha256: string; item_count: number
              source_ref: string | null }>(
      `/api/publish/releases/${encodeURIComponent(id)}/asset/${encodeURIComponent(layer)}`),
  relFreeze: (body: Record<string, unknown>) =>
    request<{ release_id: string; rule_set_version: string; bundle_sha256: string
              layers: number; diff: RelDiff; note: string }>(
      '/api/publish/freeze', { method: 'POST', body: JSON.stringify(body) }),
  relReview: (id: string, by: string) =>
    request<Record<string, unknown>>(
      `/api/publish/releases/${encodeURIComponent(id)}/review`,
      { method: 'POST', body: JSON.stringify({ by }) }),
  relPublish: (id: string, by: string) =>
    request<{ status: string; reviewed_by: string; same_person: boolean; note: string }>(
      `/api/publish/releases/${encodeURIComponent(id)}/publish`,
      { method: 'POST', body: JSON.stringify({ by }) }),
  relSwitch: (id: string, by: string) =>
    request<Record<string, unknown>>(
      `/api/publish/releases/${encodeURIComponent(id)}/current`,
      { method: 'POST', body: JSON.stringify({ by }) }),
  relRollback: (id: string, body: Record<string, unknown>) =>
    request<{ status: string; current: string | null; note: string }>(
      `/api/publish/releases/${encodeURIComponent(id)}/rollback`,
      { method: 'POST', body: JSON.stringify(body) }),
  relDiscard: (id: string, by: string) =>
    request<Record<string, unknown>>(
      `/api/publish/releases/${encodeURIComponent(id)}/discard`,
      { method: 'POST', body: JSON.stringify({ by }) }),
  relAccessLog: (limit = 50) =>
    request<{ items: RelAccess[]; note: string }>(`/api/publish/access-log${qs({ limit })}`),
  relStorage: () =>
    request<{ items: StorageRow[]; note: string }>('/api/publish/storage'),

  // ── 消费面：页面上把它原样展示出来，集成方照着抄 ──────────────────────────
  pubIndex: () => request<PubIndex>('/api/pub/v1'),
  pubBundleManifest: (version: string) =>
    request<{ files: { file_name: string; layer: string | null
                       sha256: string; bytes: number }[]
              verify: string; masking: string; packed_at: string }>(
      `/api/pub/v1/releases/${encodeURIComponent(version)}/bundle-manifest`),
  /** 资产包是 zip，不走 request()（它按 JSON 解析）。给页面一个直链即可。 */
  pubBundleUrl: (version: string) =>
    `/api/pub/v1/releases/${encodeURIComponent(version)}/bundle`,
  // ── 系统 · 智能体工作台 ──────────────────────────────────────────────────
  // **只读**：这一组里没有改参数、投信号、停 Agent 的接口。
  // 改参数要走 improve 的 bounds 校验与 improve_log，不能从页面绕过去。
  agentOverview: () => request<AgentOverview>('/api/agents'),
  agentChecks: () =>
    request<{ items: { check: string; ok: boolean; detail: string }[]
              total: number; failed: number; note: string }>('/api/agents/checks'),
  agentDetail: (id: string) =>
    request<AgentDetail>(`/api/agents/${encodeURIComponent(id)}`),
  agentSignals: (limit = 50) =>
    request<{ items: AgentSignalRow[]
              by_agent: { agent_id: string; n: number; first: string; last: string }[]
              by_action: { action: string; n: number }[]
              pool: { channel: string; status: string; n: number; last: string }[]
              backlog: number; facts: AgentFact[]; note: string }>(
      `/api/agents/signals/recent${qs({ limit })}`),
  agentCycles: (limit = 50) =>
    request<{ items: AgentCycleRow[]
              by_agent: { agent_id: string; n: number; bad: number; last: string }[]
              by_trigger: { trigger_kind: string; n: number }[]; note: string }>(
      `/api/agents/cycles/recent${qs({ limit })}`),
  agentChains: () =>
    request<{ items: AgentChain[]; note: string }>('/api/agents/chains/list'),
  agentChainRun: (chainId: string) =>
    request<{ ok: boolean; job_id?: string; reason?: string }>(
      `/api/agents/chains/${encodeURIComponent(chainId)}/run`,
      { method: 'POST', body: '{}' }),
  agentChainProgress: (chainId: string) =>
    request<ExProgress>(`/api/agents/chains/${encodeURIComponent(chainId)}/progress`),
  agentStorage: () =>
    request<{ items: StorageRow[]; note: string }>('/api/agents/storage/tables'),
  // ── 多模态读图 ──────────────────────────────────────────────────────────
  // **和 OCR 刻意分成两组**：OCR 本机跑不出网不计费；读图会把那一页的 PNG
  // 发到厂商、按图计费。合成一个按钮，用户就不知道这一下会不会把文件发出去。
  visionPages: (limit = 40) =>
    request<{ items: VisionPage[]; total: number; note: string }>(
      `/api/doclib/vision/pages${qs({ limit })}`),
  visionRead: (body: Record<string, unknown>) =>
    request<{ ok: boolean; model_name: string; rows: number | null
              unreadable: number | null; latency_ms: number | null
              tokens: Record<string, number | null>; err_kind?: string
              message?: string; content: string | null }>(
      '/api/doclib/vision/read', { method: 'POST', body: JSON.stringify(body) }),
  visionPage: (fileId: string, pageNo: number) =>
    request<{ reads: VisionRead[]; cross_check: VisionCross }>(
      `/api/doclib/vision/page${qs({ file_id: fileId, page_no: pageNo })}`),
  visionSummary: () =>
    request<{ pages: Record<string, unknown>[]; totals: Record<string, number>
              note: string }>('/api/doclib/vision/summary'),
  visionStorage: () =>
    request<{ items: StorageRow[]; note: string }>('/api/doclib/vision/storage'),
  visionVerify: (id: number, by: string, note?: string) =>
    request<{ id: number; verified_by: string; note: string }>(
      '/api/doclib/vision/verify',
      { method: 'POST', body: JSON.stringify({ id, by, note }) }),

  // ── 领域与实例 ────────────────────────────────────────────────────────────
  // 当前领域/实例是**全局单例**：库里一行，所有人共用，不存浏览器。
  domainState: () =>
    request<{ current: DomainCurrent; domains: DomainRow[]; note: string }>(
      '/api/domain/state'),
  domainSetCurrent: (domainId: string, instanceId?: string | null, who?: string) =>
    request<{ ok: boolean; current: DomainCurrent; note: string }>(
      '/api/domain/current',
      { method: 'POST',
        body: JSON.stringify({ domain_id: domainId, instance_id: instanceId, who }) }),
  domainOverview: () =>
    request<{ current: DomainCurrent; counts: Record<string, number>
              current_release: string | null; current_release_why: string
              rule_set_version: string | null; onto_version: string | null
              scope_note: string }>('/api/domain/overview'),
}
