<script setup lang="ts">
/**
 * 系统 · 大模型配置。
 *
 * 这个页面有五件事是**刻意做成这样**的，不是风格问题：
 *
 * 1. 用途 × 模型能力的交叉校验（最重要的一条）。
 *    vision_read 是拿模型读发票图。配一个纯文本模型上去不会报错——
 *    模型会按 prompt 里的文字线索编一段看起来合理的内容，
 *    而下游会把它当成「已核验的票面信息」。那种错误没有任何技术信号。
 *    判断逻辑在 data/llmState.ts（可测），后端 sys_llm.modality_warning 同一套。
 *
 * 2. 厂商下拉里「未核对」的那几条标红。
 *    它们的 base_url 没对过官方文档。不作区分，用户配不通时会先怀疑自己的 Key、
 *    再怀疑网络，最后才想到是地址错——那段时间全是白花的。
 *
 * 3.「没测过」和「测了不通」是两个状态，不许合成一个。
 *    合成一个灰色的「不可用」，用户就不知道该去测还是该去修。
 *
 * 4. 型号不写死在前端，按「拉取型号」从厂商现取。
 *    型号漂移很快（2026-10-18 核对实测：智谱已到 glm-5.3、Kimi 已到 kimi-k3）。
 *    写死的下拉半年后就是一张**错的**菜单，而错的下拉比空的下拉坏：
 *    用户会以为自己选了个有效型号。
 *
 * 5. 删除要点两次。它会连带删掉测试历史，而测试历史是排查
 *    「什么时候开始不通的」唯一依据。不用 window.confirm：
 *    原生弹窗会卡住页面自动化，而且页内二次确认看得更清楚删的是哪条。
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { LlmConfig, LlmPreset, LlmTestResult, LlmTestRow } from '../api'
import { ERR_LABEL, KEY_FORMAT, modalityWarning, testTag } from '../data/llmState'

const loading = ref(true)
const err = ref('')
const msg = ref('')
const warn = ref('')

const presets = ref<LlmPreset[]>([])
const presetNote = ref('')
const configs = ref<LlmConfig[]>([])
const purposes = ref<{ key: string; label: string; default: string | null }[]>([])
const missing = ref<string[]>([])
const policy = ref('')
const defaultsNote = ref('')
const keyStatus = ref<Awaited<ReturnType<typeof api.llmKeyStatus>> | null>(null)

function blankForm() {
  return {
    config_id: '',
    name: '',
    preset_id: '',
    provider: 'openai_compatible',
    base_url: '',
    model_name: '',
    api_key: '',
    purpose: 'rule_judge',
    temperature: 0,
    max_tokens: null as number | null,
    enable_thinking: false,
    concurrency: 3,
    timeout_s: 60,
    enabled: true,
  }
}
const form = ref(blankForm())
const editing = ref(false)

/** 「拉取型号」拿回来的列表。空数组 ≠ 没拉过，用 modelsFetched 区分 */
const models = ref<string[]>([])
const modelsFetched = ref(false)
const busy = ref('')                         // 正在跑的动作名，用来禁按钮防重复点
const probe = ref<LlmTestResult | null>(null)
const testResult = ref<Record<string, LlmTestResult>>({})
const history = ref<{ cid: string; rows: LlmTestRow[] } | null>(null)
const pendingDelete = ref('')                // 删除的二次确认

const preset = computed(() => presets.value.find((p) => p.preset_id === form.value.preset_id))
const isHttp = computed(() => form.value.base_url.startsWith('http://'))
// 把型号名和「已经配成文本用途的型号」一起带进去：2026-10-19 收紧后，
// 预置选对了但型号填成文本模型，这里也要报。
const textModels = computed(() =>
  configs.value
    .filter((c) => c.purpose === 'rule_judge' || c.purpose === 'extract')
    .map((c) => c.model_name)
    .filter((m): m is string => !!m),
)
const modalityWarn = computed(() =>
  modalityWarning(form.value.purpose, preset.value?.modality ?? null,
                  form.value.model_name, textModels.value,
                  preset.value?.suggest_model ?? null),
)
const legacyRows = computed(() => configs.value.filter((c) => c.key_format === 'legacy'))

/**
 * 「各用途的默认模型」那张表要能**就地设置**，不能只是个看板。
 *
 * 原来设默认只有一条路：往下翻到「已有配置」，自己认出哪条的用途是 vision_read，
 * 再点那一行的「设为默认」。而用户看见「未设置」的第一反应是去点它——
 * 点不动就只能认为功能坏了。中间这几步没有任何提示，等于把路藏起来了。
 *
 * 候选只列**用途对得上且已启用**的配置：
 * 停用的设成默认等于「默认模型不可用，界面上却看着是有的」，
 * 后端 set_default 本来也会拒（400），那就不该让人先选到再被拒。
 */
const pick = ref<Record<string, string>>({})

function candidates(purposeKey: string) {
  return configs.value.filter((c) => c.purpose === purposeKey && c.enabled)
}

/** 下拉选中值：优先保留用户刚选的，其次当前默认，再次第一条候选。 */
function syncPicks() {
  const next: Record<string, string> = {}
  for (const p of purposes.value) {
    const ids = candidates(p.key).map((c) => c.config_id)
    const prev = pick.value[p.key]
    next[p.key] =
      prev && ids.includes(prev)
        ? prev
        : p.default && ids.includes(p.default)
          ? p.default
          : ids[0] || ''
  }
  pick.value = next
}

/**
 * 这个用途一条候选都没有时，直接把下面的表单切到这个用途。
 * 否则用户得自己想明白「要先建一条 purpose=vision_read 的配置」——
 * 这件事界面上原来一个字都没写。
 */
const formPanel = ref<HTMLElement | null>(null)
function newForPurpose(key: string, label: string) {
  resetForm()
  form.value.purpose = key
  note(`下面的表单已切到用途「${label}」。填好地址、型号、Key 保存，再回到这里设为默认。`)
  formPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function loadAll() {
  loading.value = true
  err.value = ''
  try {
    const [p, c, k] = await Promise.all([
      api.llmPresets(),
      api.llmConfigs(),
      api.llmKeyStatus(),
    ])
    presets.value = p.items
    presetNote.value = p.note
    configs.value = c.items
    purposes.value = c.purposes
    missing.value = c.missing_defaults
    policy.value = c.policy
    defaultsNote.value = c.note
    keyStatus.value = k
    syncPicks()
  } catch (e) {
    // 取不到就说取不到。**不要退化成空列表**——
    // 空列表看着像「还没配过模型」，而实际是接口挂了，这两件事必须分得开。
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(loadAll)

function note(m: string, w = '') {
  msg.value = m
  warn.value = w
}

function pickPreset(pid: string) {
  form.value.preset_id = pid
  const p = presets.value.find((x) => x.preset_id === pid)
  if (!p) return
  form.value.base_url = p.base_url
  form.value.provider = p.protocol
  form.value.model_name = p.suggest_model || ''
  if (!form.value.name) form.value.name = p.label
  models.value = []
  modelsFetched.value = false
  probe.value = null
}

/** 把一行载入表单编辑。Key 不回填（接口只回掩码），留空表示不改动。 */
function edit(c: LlmConfig) {
  form.value = {
    config_id: c.config_id,
    name: c.config_name,
    preset_id: c.preset_id || '',
    provider: c.provider,
    base_url: c.base_url,
    model_name: c.model_name,
    api_key: '',
    purpose: c.purpose || 'rule_judge',
    temperature: Number(c.temperature ?? 0),
    max_tokens: c.max_tokens,
    enable_thinking: c.enable_thinking,
    concurrency: c.concurrency,
    timeout_s: c.timeout_s,
    enabled: c.enabled,
  }
  editing.value = true
  models.value = []
  modelsFetched.value = false
  probe.value = null
  note('')
}

function resetForm() {
  form.value = blankForm()
  editing.value = false
  models.value = []
  modelsFetched.value = false
  probe.value = null
  note('')
}

async function run(tag: string, fn: () => Promise<void>) {
  busy.value = tag
  err.value = ''
  try {
    await fn()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = ''
  }
}

const fetchModels = () =>
  run('models', async () => {
    probe.value = null
    const r = await api.probeLlm({ ...form.value }, 'models')
    probe.value = r
    models.value = r.models || []
    modelsFetched.value = true
    if (r.ok && models.value.length && !models.value.includes(form.value.model_name)) {
      form.value.model_name = models.value[0]
    }
  })

const probeChat = () =>
  run('probe', async () => {
    probe.value = await api.probeLlm({ ...form.value }, 'chat')
  })

const save = () =>
  run('save', async () => {
    const r = await api.saveLlmConfig({ ...form.value })
    note(
      `已保存 ${r.config_id}。${r.key_saved ? `Key 掩码 ${r.api_key_mask}。` : ''}${r.note}`,
      r.modality_warning,
    )
    form.value.api_key = ''
    form.value.config_id = r.config_id
    editing.value = true
    await loadAll()
  })

const runTest = (cid: string, kind: 'chat' | 'models') =>
  run(cid + ':' + kind, async () => {
    testResult.value = { ...testResult.value, [cid]: await api.testLlmConfig(cid, kind) }
    await loadAll()
  })

const makeDefault = (cid: string) =>
  run(cid + ':default', async () => {
    const r = await api.setLlmDefault(cid)
    note(
      `已把 ${cid} 设为「${r.purpose_label}」的默认模型` +
        (r.replaced ? `，换掉了原来的 ${r.replaced.config_id}` : '') +
        (r.warning ? `。${r.warning}` : ''),
      r.modality_warning,
    )
    await loadAll()
  })

const toggleEnabled = (c: LlmConfig) =>
  run(c.config_id + ':enabled', async () => {
    const r = await api.setLlmEnabled(c.config_id, !c.enabled)
    note(`${c.config_id} 已${r.enabled ? '启用' : '停用'}`, r.warning)
    await loadAll()
  })

const showHistory = (cid: string) =>
  run(cid + ':hist', async () => {
    history.value = { cid, rows: (await api.llmTestHistory(cid)).items }
  })

const remove = (cid: string) =>
  run(cid + ':del', async () => {
    const r = await api.deleteLlmConfig(cid)
    note(
      `已删除 ${cid}，连带删掉 ${(r as Record<string, unknown>).tests_removed} 条测试历史。`,
      String((r as Record<string, unknown>).note || ''),
    )
    pendingDelete.value = ''
    if (history.value?.cid === cid) history.value = null
    if (form.value.config_id === cid) resetForm()
    await loadAll()
  })

function fmtTime(s: string | null) {
  return s ? String(s).replace('T', ' ').slice(0, 19) : '—'
}
</script>

<template>
  <h2>大模型配置</h2>
  <div class="sub">
    系统级配置。支撑规则抽取、票据读图、规则研判三类用途。
    Key 只回掩码，明文永不出接口。
  </div>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>
  <div v-if="warn" class="err">{{ warn }}</div>
  <div v-if="loading" class="muted">读取中…</div>

  <template v-else>
    <!-- ── 两条脱敏口径：旧版页面就写着，不能在改版时丢掉 ──────────────────── -->
    <div class="note">
      <b>{{ policy }}</b>
      <div style="margin-top: 4px">
        另外一条同等重要：<b>送进模型前就要脱敏，不能只脱回来的</b>。
        请求体一旦发出就离开了本系统的控制范围。
        <code>llm_call_log.prompt_masked</code> 为 FALSE 的行出现在生产，
        说明有一条调用路径绕过了脱敏。
      </div>
    </div>

    <!-- ── 默认模型概览 ───────────────────────────────────────────────────── -->
    <div class="panel">
      <h3>各用途的默认模型</h3>
      <table>
        <thead>
          <tr><th>用途</th><th>默认模型</th><th>说明</th><th>设置</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in purposes" :key="p.key">
            <td class="mono">{{ p.key }}</td>
            <td>
              <span v-if="p.default" class="mono">{{ p.default }}</span>
              <span v-else class="tag t-pend">未设置</span>
            </td>
            <td class="muted">{{ p.label }}</td>
            <td>
              <template v-if="candidates(p.key).length">
                <select v-model="pick[p.key]" style="min-width: 240px">
                  <option v-for="c in candidates(p.key)" :key="c.config_id" :value="c.config_id">
                    {{ c.config_id }} · {{ c.model_name
                    }}{{ c.config_id === p.default ? '（当前默认）' : '' }}
                  </option>
                </select>
                <button
                  style="margin-left: 6px"
                  :disabled="!!busy || !pick[p.key] || pick[p.key] === p.default"
                  @click="makeDefault(pick[p.key])"
                >
                  {{ busy === pick[p.key] + ':default'
                    ? '设置中…'
                    : p.default ? '换成这条' : '设为默认' }}
                </button>
              </template>
              <template v-else>
                <span class="muted" style="font-size: 12px">没有这个用途的可用配置</span>
                <button style="margin-left: 6px" :disabled="!!busy"
                        @click="newForPurpose(p.key, p.label)">
                  新建一条
                </button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="missing.length" class="note" style="margin-top: 10px">{{ defaultsNote }}</div>
      <div v-else class="muted" style="margin-top: 8px">{{ defaultsNote }}</div>
      <div class="muted" style="margin-top: 8px; font-size: 12px">
        默认模型按用途各一个，不是全局一个：读图必须多模态，判规则要长文本推理。
        全局只允许一个默认就会出现<b>拿纯文本模型去读发票图</b>——
        它不会报错，会编一段看起来合理的内容，那种错误最难发现。
        库里有唯一索引 <code>ux_llm_default_per_purpose</code> 保证每个用途至多一条。
      </div>
    </div>

    <!-- ── Key 的加密状态 ─────────────────────────────────────────────────── -->
    <div v-if="keyStatus" class="panel">
      <h3>API Key 的存储</h3>
      <div class="kv">
        <b>加密密钥来源</b><span class="mono">{{ keyStatus.source || '不可用' }}</span>
        <b>够生产用吗</b>
        <span>
          <span class="tag" :class="keyStatus.production_ready ? 't-pass' : 't-pend'">
            {{ keyStatus.production_ready ? '是（走环境变量）' : '否（仅 POC）' }}
          </span>
        </span>
      </div>
      <div v-if="keyStatus.note" class="note"><MarkText :text="keyStatus.note" /></div>
      <div v-if="legacyRows.length" class="err">
        有 {{ legacyRows.length }} 条配置的 Key 是旧的单向摘要格式，取不回明文，测试必然失败：
        <b class="mono">{{ legacyRows.map((c) => c.config_id).join('、') }}</b>。
        请逐条重新填一次 API Key。
      </div>
    </div>

    <!-- ── 已有配置 ───────────────────────────────────────────────────────── -->
    <div class="panel">
      <h3>已有配置（{{ configs.length }} 条）</h3>
      <div v-if="!configs.length" class="muted">
        还没有配置。没有默认模型时，软规则输出的是「待人工：未调用模型」，
        <b>不是「判过了」</b>——这两者的区别必须保留，假装判过了就是声称复核了没复核的东西。
      </div>
      <table v-else>
        <thead>
          <tr>
            <th>配置</th><th>用途</th><th>地址 / 型号</th><th>Key</th>
            <th>最近测试</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in configs" :key="c.config_id">
            <td>
              <div class="mono">{{ c.config_id }}</div>
              <div class="muted" style="font-size: 11px">{{ c.config_name }}</div>
              <span v-if="c.is_default" class="tag t-demo">默认</span>
              <span v-if="!c.enabled" class="tag t-na">停用</span>
            </td>
            <td class="mono">{{ c.purpose || '—' }}</td>
            <td>
              <div class="mono" style="font-size: 11px">{{ c.base_url }}</div>
              <div class="mono">{{ c.model_name }}</div>
              <div
                v-if="c.last_test_model_echo && c.last_test_model_echo !== c.model_name"
                class="tag t-pend"
                style="margin-top: 2px"
              >
                厂商回的是 {{ c.last_test_model_echo }}
              </div>
              <!-- 存量配置的读图能力警告。实测躺过一个:预置选的是读图预置，
                   型号却填成文本模型，而原来这条检查只在保存那一刻跑。 -->
              <div v-if="c.modality_warning" class="err"
                   style="margin-top: 4px; font-size: 11px; line-height: 1.5">
                {{ c.modality_warning }}
              </div>
            </td>
            <td>
              <div class="mono">{{ c.api_key_mask || '—' }}</div>
              <span class="tag" :class="KEY_FORMAT[c.key_format]?.cls || 't-na'">
                {{ KEY_FORMAT[c.key_format]?.text || c.key_format }}
              </span>
            </td>
            <td>
              <span class="tag" :class="testTag(c.last_test_ok).cls">
                {{ testTag(c.last_test_ok).text }}
              </span>
              <div v-if="c.last_test_at" class="muted" style="font-size: 11px">
                {{ fmtTime(c.last_test_at) }}
                <span v-if="c.last_test_latency_ms !== null">· {{ c.last_test_latency_ms }}ms</span>
              </div>
              <div v-if="c.last_test_err_kind" class="muted" style="font-size: 11px">
                {{ ERR_LABEL[c.last_test_err_kind] || c.last_test_err_kind }}
              </div>
            </td>
            <td>
              <div class="row" style="margin: 0; gap: 5px">
                <button :disabled="!!busy" @click="runTest(c.config_id, 'chat')">
                  {{ busy === c.config_id + ':chat' ? '测试中…' : '测试' }}
                </button>
                <button :disabled="!!busy" @click="runTest(c.config_id, 'models')">
                  {{ busy === c.config_id + ':models' ? '…' : '列型号' }}
                </button>
                <button
                  :disabled="!!busy || c.is_default || !c.purpose || !c.enabled"
                  @click="makeDefault(c.config_id)"
                >
                  设为默认
                </button>
                <button :disabled="!!busy" @click="edit(c)">编辑</button>
                <button :disabled="!!busy" @click="toggleEnabled(c)">
                  {{ c.enabled ? '停用' : '启用' }}
                </button>
                <button :disabled="!!busy" @click="showHistory(c.config_id)">历史</button>
                <button v-if="pendingDelete !== c.config_id" :disabled="!!busy"
                        @click="pendingDelete = c.config_id">
                  删除
                </button>
                <template v-else>
                  <button :disabled="!!busy" @click="remove(c.config_id)">确认删除</button>
                  <button :disabled="!!busy" @click="pendingDelete = ''">取消</button>
                </template>
              </div>
              <div v-if="pendingDelete === c.config_id" class="err"
                   style="margin-top: 5px; font-size: 11px">
                会连带删掉这条的全部测试历史——那是排查「什么时候开始不通的」唯一依据。
              </div>
              <div v-if="testResult[c.config_id]" style="margin-top: 6px">
                <div class="tag" :class="testResult[c.config_id].ok ? 't-pass' : 't-hit'">
                  {{ testResult[c.config_id].ok ? '通了' : '失败' }}
                  <span v-if="testResult[c.config_id].latency_ms !== null">
                    {{ testResult[c.config_id].latency_ms }}ms
                  </span>
                </div>
                <div v-if="testResult[c.config_id].models?.length" class="muted"
                     style="font-size: 11px; margin-top: 2px">
                  厂商给出 {{ testResult[c.config_id].models.length }} 个型号：
                  {{ testResult[c.config_id].models.slice(0, 6).join('、') }}
                  <span v-if="testResult[c.config_id].models.length > 6">…</span>
                </div>
                <div v-if="testResult[c.config_id].reply_excerpt" class="muted"
                     style="font-size: 11px; margin-top: 2px">
                  模型回了：{{ testResult[c.config_id].reply_excerpt }}
                </div>
                <div v-if="testResult[c.config_id].message" class="muted"
                     style="font-size: 11px; margin-top: 2px">
                  {{ testResult[c.config_id].message }}
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="muted" style="margin-top: 10px; font-size: 12px">
        「测试」会<b>真的发一次请求到上面那个地址</b>。请求体里只有一句固定问候语，
        不含任何业务数据——不拿真实报账单去试一个还没验证过的地址。
        「列型号」只调 <code>/models</code>，不消耗 token。
        每次点击都会在 <code>llm_test_log</code> 里追加一条，谁在什么时候往哪儿发过，查得到。
        没有任何自动或定时调用。
      </div>
    </div>

    <!-- ── 测试历史 ───────────────────────────────────────────────────────── -->
    <details v-if="history" class="panel" open>
      <summary>{{ history.cid }} 的测试历史（{{ history.rows.length }} 条）</summary>
      <div class="muted" style="margin: 8px 0; font-size: 12px">
        只看「最近一次」只能回答「现在通不通」。排错时要回答的是
        <b>「从哪天开始不通的」</b>——跑批出一堆「待人工：未调用模型」时，
        要能对上是哪天 Key 过期的。
      </div>
      <table v-if="history.rows.length">
        <thead>
          <tr><th>时间</th><th>类型</th><th>结果</th><th class="num">耗时</th>
            <th>型号回显</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="t in history.rows" :key="t.id">
            <td class="mono">{{ fmtTime(t.tested_at) }}</td>
            <td class="mono">{{ t.kind }}</td>
            <td>
              <span class="tag" :class="t.ok ? 't-pass' : 't-hit'">
                {{ t.ok ? '通' : (ERR_LABEL[t.err_kind || ''] || t.err_kind || '失败') }}
              </span>
            </td>
            <td class="num">{{ t.latency_ms ?? '—' }}</td>
            <td class="mono">{{ t.model_echo || '—' }}</td>
            <td style="max-width: 420px">
              <div v-if="t.reply_excerpt">回复：{{ t.reply_excerpt }}</div>
              <div class="muted" style="font-size: 11px">{{ t.message }}</div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted">还没有测试记录。</div>
    </details>

    <!-- ── 新增 / 编辑 ────────────────────────────────────────────────────── -->
    <div ref="formPanel" class="panel">
      <h3>{{ editing ? '编辑 ' + form.config_id : '新增配置' }}</h3>

      <div class="kv">
        <b>厂商</b>
        <span>
          <select :value="form.preset_id" style="min-width: 300px"
                  @change="pickPreset(($event.target as HTMLSelectElement).value)">
            <option value="">— 手动填写 —</option>
            <option v-for="p in presets" :key="p.preset_id" :value="p.preset_id">
              {{ p.label }}{{ p.verified_on ? '' : '（地址未核对）' }}
            </option>
          </select>
        </span>

        <b>用途</b>
        <span>
          <select v-model="form.purpose">
            <option value="rule_judge">rule_judge · 规则判定（长文本推理）</option>
            <option value="vision_read">vision_read · 读票据/行程单图（必须多模态）</option>
            <option value="extract">extract · 从制度文件抽规则</option>
          </select>
        </span>

        <b>配置 ID</b>
        <span>
          <input v-model="form.config_id" placeholder="留空自动生成" style="width: 260px"
                 :readonly="editing" />
          <span v-if="editing" class="muted" style="margin-left: 8px; font-size: 11px">
            编辑中，ID 不可改
          </span>
        </span>

        <b>名称</b><span><input v-model="form.name" style="width: 260px" /></span>

        <b>Base URL</b>
        <span><input v-model="form.base_url" style="width: 420px" placeholder="https://…/v1" /></span>

        <b>型号</b>
        <span>
          <select v-if="models.length" v-model="form.model_name" style="min-width: 260px">
            <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
          </select>
          <input v-else v-model="form.model_name" style="width: 260px"
                 placeholder="点「拉取型号」从厂商取，或手填" />
          <button :disabled="!!busy || !form.base_url || preset?.model_list === false"
                  style="margin-left: 8px" @click="fetchModels">
            {{ busy === 'models' ? '拉取中…' : '拉取型号' }}
          </button>
        </span>

        <b>API Key</b>
        <span>
          <input v-model="form.api_key" type="password" style="width: 420px"
                 placeholder="明文填入，存成密文；留空表示不改动已存的 Key" />
        </span>

        <b>温度</b>
        <span>
          <input v-model.number="form.temperature" type="number" step="0.1" min="0" max="2"
                 style="width: 90px" />
          <span class="muted" style="margin-left: 8px; font-size: 11px">
            审核场景建议 0：同一张单两次跑出不同结论，没法向审核岗解释
          </span>
        </span>

        <b>最大输出</b>
        <span>
          <input v-model.number="form.max_tokens" type="number" style="width: 110px"
                 placeholder="留空用厂商默认" />
        </span>

        <b>并发</b>
        <span><input v-model.number="form.concurrency" type="number" min="1" style="width: 90px" /></span>

        <b>超时（秒）</b>
        <span><input v-model.number="form.timeout_s" type="number" min="1" style="width: 90px" /></span>

        <b>深度思考</b>
        <span>
          <input v-model="form.enable_thinking" type="checkbox" />
          <span class="muted" style="margin-left: 6px; font-size: 11px">
            只有支持的型号有效；开了会更慢更贵
          </span>
        </span>

        <b>启用</b>
        <span><input v-model="form.enabled" type="checkbox" /></span>
      </div>

      <!-- 用途 × 能力的交叉校验：这是本页最该看的一条提示 -->
      <div v-if="modalityWarn" class="err" style="margin-top: 12px">
        <b>⚠ 用途与模型能力不匹配</b>
        <div style="margin-top: 4px">{{ modalityWarn }}</div>
      </div>

      <!-- 选中厂商后的提示 -->
      <div v-if="preset" :class="preset.verified_on ? 'note' : 'err'" style="margin-top: 12px">
        <div>
          <b>{{ preset.label }}</b> ·
          <span class="tag" :class="preset.modality === 'text' ? 't-na' : 't-pass'">
            {{ preset.modality === 'text' ? '纯文本' : preset.modality === 'vision' ? '读图' : '文本+读图' }}
          </span>
          <template v-if="preset.verified_on">
            地址已对过官方文档（{{ preset.verified_on }}）
          </template>
          <template v-else>
            <b>地址没对过官方文档，是凭印象填的。</b>配不通请优先怀疑这里，不是你的 Key。
          </template>
        </div>
        <div v-if="preset.key_hint" style="margin-top: 4px">
          Key 形态：<code>{{ preset.key_hint }}</code>
        </div>
        <div v-if="preset.caveat" style="margin-top: 4px">{{ preset.caveat }}</div>
        <div v-if="preset.source_url" style="margin-top: 4px">
          依据：<a :href="preset.source_url" target="_blank" rel="noreferrer">官方文档</a>
        </div>
      </div>

      <div v-if="isHttp" class="note">
        这是 <code>http://</code> 地址，<b>API Key 会明文过网</b>。内网不等于安全。
        另外：差旅数据含姓名、工号、银行账号，走公网厂商就等于把这些交给第三方——
        这是要先问清楚客户允不允许的事，不是技术选型。
      </div>

      <div v-if="modelsFetched && !models.length" class="note">
        这家没回型号列表。可能是不支持 <code>/models</code>，也可能是 Key 不对——
        看下面的原文。火山方舟这类要填的是推理接入点 ID（<code>ep-</code> 开头），
        型号列表帮不上忙。
      </div>

      <div class="row" style="margin-top: 12px">
        <button :disabled="!!busy || !form.base_url || !form.model_name" @click="probeChat">
          {{ busy === 'probe' ? '测试中…' : '先测一下（不保存）' }}
        </button>
        <button :disabled="!!busy || !form.base_url || !form.model_name" @click="save">
          {{ busy === 'save' ? '保存中…' : editing ? '保存修改' : '保存' }}
        </button>
        <button v-if="editing" :disabled="!!busy" @click="resetForm">新建另一条</button>
      </div>
      <div class="muted" style="margin-top: 6px; font-size: 12px">
        先测通再存，不容易误解——「存下来」在人眼里往往等于「配好了」。
        试测不写测试历史（还没有配置 ID 可挂），保存后再测一次会留痕。
      </div>

      <!-- 试测结果 -->
      <div v-if="probe" :class="probe.ok ? 'note' : 'err'" style="margin-top: 12px">
        <div>
          <b>{{ probe.ok ? '通了' : '失败' }}</b>
          <span v-if="probe.err_kind"> · {{ ERR_LABEL[probe.err_kind] || probe.err_kind }}</span>
          <span v-if="probe.http_status"> · HTTP {{ probe.http_status }}</span>
          <span v-if="probe.latency_ms !== null"> · {{ probe.latency_ms }}ms</span>
        </div>
        <div v-if="probe.reply_excerpt" style="margin-top: 4px">
          模型回了：<b>{{ probe.reply_excerpt }}</b>
          <span class="muted">（看见模型真说了话才算通——网关在 Key 无效时也可能回 200）</span>
        </div>
        <div v-if="probe.models?.length" style="margin-top: 4px">
          取到 {{ probe.models.length }} 个型号
        </div>
        <div v-if="probe.message" class="mono" style="margin-top: 4px; font-size: 11px">
          {{ probe.message }}
        </div>
      </div>

      <div class="muted" style="margin-top: 10px; font-size: 12px">{{ presetNote }}</div>
    </div>
  </template>
</template>
