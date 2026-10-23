<script setup lang="ts">
/**
 * 栏目三 · 规则本体。四段：本体建设 / 本体展示 / 本体关系 / 本体图谱。 *
 * 2026-10-21 隐藏了「存储」页签（按表名/行数列出本体落在哪些库表）。
 * 接口 `/api/onto/storage` 与数据都保留，只是不在这一页出现；
 * 恢复 = 把页签按钮、Tab 类型里的 'store'、pickTab 的那一支和模板块加回来。
 *
 * **本体建设**：统一口径由三个确定性 Agent 协同跑出来——
 * 口径感知 → 多源归并与冲突 → 双向对账。**不出网、不计费**（和抽取链刻意不同：
 * 口径对不对是能复核的硬判断，让模型来判只会给出一条「看起来有据可查」的证据链）。
 * 采纳入本体是**人的动作**：Agent 连写 rule 表的能力都没有。
 *
 * **本体展示**：三个数必须并列——总数 / 可编译 / 执行集。
 * 只显示「82 条规则」会让人以为 82 条都在工作；只显示「可编译 37」会让人
 * 以为 37 条都在跑。「可编译」是规则资产层面，「执行集」是手上有没有数据。
 * 双向对账同理：**不合成一个覆盖率**，左右两侧缺口的责任方完全不同。
 *
 * **本体关系 / 本体图谱**：本体没有关系就只是一张清单。
 * 图谱不是关系表的美化版，它回答表格回答不了的问题：
 * 哪个节点是枢纽、哪条边悬着、哪个点是孤的。
 *
 * **存储**：只给表名和行数没有意义，人要知道的是「我点了重算，什么会消失」。
 *
 * 档位一律照后端显示，**前端不许折叠**：
 * verdict 的「判不出」和 state 的「编号对不上」都是后端刻意不下断言的档位，
 * 折叠成两档等于替后端下了它不肯下的结论。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AgentRuntime from '../components/AgentRuntime.vue'
import Async from '../components/Async.vue'
import { api } from '../api'
import type {
  ExConflict, ExMeta, ExProgress, OntoAdoption, OntoAlign, OntoCover, OntoGraph,
  OntoRelations, OntoSummary, Page, Rule, StorageRow,
} from '../api'

type Tab = 'build' | 'show' | 'rel' | 'graph'
const tab = ref<Tab>('build')

const prog = ref<ExProgress | null>(null)
const meta = ref<ExMeta | null>(null)
const sum = ref<OntoSummary | null>(null)
const pending = ref<OntoAlign[]>([])
const conflicts = ref<ExConflict[]>([])
const adoptions = ref<OntoAdoption[]>([])
const groups = ref<OntoAlign[]>([])
const cover = ref<OntoCover[]>([])
const rel = ref<OntoRelations | null>(null)
const graph = ref<OntoGraph | null>(null)
const opts = ref<{ dimensions: string[]; rule_classes: string[]
                   control_types: string[]; risk_classes: string[] } | null>(null)

/** 库里的数对不对得上——只报对不上的。对得上时数字本身就是证据。 */
const staleWarns = computed(() => (sum.value?.freshness || []).filter((c) => !c.ok))

const busy = ref('')
const err = ref('')
const msg = ref('')
let timer: number | undefined

/** 采纳表单。**一律空着开始**：预填一个控制类型，人就会直接点确认。 */
const form = ref<Record<string, string>>({})
const openGroup = ref('')
const who = ref('')

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

async function poll() {
  try {
    prog.value = await api.ontoProgress()
    if (prog.value.status !== 'running') {
      stopPoll()
      await reloadBuild()
    }
  } catch {
    // 轮询失败不清空已有快照：清空会让页面在网络抖一下时整个塌掉，
    // 看起来像作业消失了。
    stopPoll()
  }
}
function startPoll() {
  stopPoll()
  timer = window.setInterval(poll, 1000)
}
function stopPoll() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

const reloadBuild = () =>
  run('build', async () => {
    const [s, p, c, a] = await Promise.all([
      api.ontoSummary(), api.ontoPending(), api.ontoConflicts(), api.ontoAdoptions(),
    ])
    sum.value = s
    pending.value = p.items
    conflicts.value = c.items
    adoptions.value = a.items
  })

const startJob = () =>
  run('run', async () => {
    const r = await api.ontoRun()
    if (!r.ok) {
      msg.value = r.reason || '已有作业在运行'
      return
    }
    msg.value = '已启动统一口径作业 ' + r.job_id
    await poll()
    startPoll()
  })

function pickTab(t: Tab) {
  tab.value = t
  if (t === 'build' && !sum.value) reloadBuild()
  if (t === 'show' && !groups.value.length) loadShow()
  if (t === 'rel' && !rel.value) run('rel', async () => { rel.value = await api.ontoRelations() })
  if (t === 'graph' && !graph.value) run('graph', async () => { graph.value = await api.ontoGraph() })
}

const loadShow = () =>
  run('show', async () => {
    const [g, c, s] = await Promise.all([
      api.ontoGroups({ limit: 300 }), api.ontoCover({ limit: 300 }),
      sum.value ? Promise.resolve(sum.value) : api.ontoSummary(),
    ])
    groups.value = g.items
    cover.value = c.items
    sum.value = s
  })

function openAdopt(g: OntoAlign) {
  openGroup.value = openGroup.value === g.group_key ? '' : g.group_key
  err.value = ''
  if (openGroup.value && !opts.value) {
    run('opts', async () => { opts.value = await api.ontoOptions() })
  }
  // 名称可以带出来（它只是个标题），**控制类型/维度/风险类别一律空着**。
  form.value = { name: g.suggest?.name || g.group_key.replace('·', ''), priority: '50' }
}

const doAdopt = (g: OntoAlign) =>
  run('adopt', async () => {
    const r = await api.ontoAdopt({ ...form.value, group_key: g.group_key, by: who.value })
    msg.value = '已采纳为 ' + r.rule_id + '（' + r.sources + ' 个出处）。' + r.note
    openGroup.value = ''
    await reloadBuild()
  })

const doDispose = (g: OntoAlign, action: '驳回' | '暂缓') =>
  run('dispose', async () => {
    await api.ontoDispose({
      group_key: g.group_key, action, by: who.value, reason: form.value.reason || '',
    })
    msg.value = g.group_key + ' 已标「' + action + '」'
    openGroup.value = ''
    await reloadBuild()
  })

const resolveConflict = (c: ExConflict, state: '已裁决' | '已忽略') =>
  run('conf', async () => {
    await api.ontoResolveConflict(c.conflict_id, state, who.value, form.value['r_' + c.conflict_id])
    msg.value = c.conflict_id + ' → ' + state
    await reloadBuild()
  })

// ── 图谱布局：**在前端只做几何，不做判断** ───────────────────────────────────
// 节点位置是圆周等分（确定性，两次打开一模一样）；
// 谁是孤点、谁是枢纽由后端给的 degree / isolated 决定，前端不自己算。
const W = 760
const H = 420
const entPos = computed(() => {
  const es = graph.value?.entities || []
  const m: Record<string, { x: number; y: number }> = {}
  es.forEach((e, i) => {
    const t = (i / Math.max(1, es.length)) * Math.PI * 2 - Math.PI / 2
    m[e.entity_id] = { x: W / 2 + Math.cos(t) * 300, y: H / 2 + Math.sin(t) * 165 }
  })
  return m
})
const ruleCols = computed(() => {
  const g = graph.value
  if (!g) return { left: [] as string[], right: [] as string[] }
  const srcs = Array.from(new Set(g.rule_edges.map((e) => e.rule_id))).sort()
  const right = Array.from(new Set(g.rule_edges.map((e) => e.other_rule)))
    .filter((x) => !srcs.includes(x)).sort()
  return { left: srcs, right }
})
function rulePos(id: string) {
  const { left, right } = ruleCols.value
  const i = left.indexOf(id)
  if (i >= 0) return { x: 120, y: 30 + (i * 330) / Math.max(1, left.length - 1 || 1) }
  const j = right.indexOf(id)
  return { x: 620, y: 30 + (j * 330) / Math.max(1, right.length - 1 || 1) }
}
const REL_COLOR: Record<string, string> = {
  overrides: '#2f6fb5', priority_over: '#7a9ec4', conflicts_with: '#c0392b',
  包含: '#2f6fb5', 关联: '#7a9ec4', 流转: '#2e8b57', 归属: '#8e6fb5',
}

const verdictTag = (v: string) =>
  v === '已落地' ? 't-pass' : v === '未落地' ? 't-hit' : 't-pend'
const stateTag = (s: string) =>
  s === '有制度依据' ? 't-pass' : s === '查无依据' ? 't-hit'
    : s === '来源未写明' ? 't-na' : 't-pend'

const DIMS = ['D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8', 'D9', 'D10']
const dim = ref('')
const cls = ref('')
const q = ref('')
const deps = computed(() => [dim.value, cls.value, q.value].join('|'))
function loadRules(): Promise<Page<Rule>> {
  return api.rules({
    dimension: dim.value || undefined, rule_class: cls.value || undefined,
    q: q.value || undefined, size: 200,
  })
}

onMounted(async () => {
  meta.value = await api.ontoMeta().catch(() => null)
  await poll()
  if (prog.value?.status === 'running') startPoll()
  await reloadBuild()
})
onUnmounted(stopPoll)
</script>

<template>
  <h2>规则本体</h2>

  <div class="row">
    <button :class="{ on: tab === 'build' }" @click="pickTab('build')">建立</button>
    <button :class="{ on: tab === 'show' }" @click="pickTab('show')">展示</button>
    <button :class="{ on: tab === 'rel' }" @click="pickTab('rel')">关系</button>
    <button :class="{ on: tab === 'graph' }" @click="pickTab('graph')">图谱</button>
    <span class="muted" style="margin-left: auto">
      本体 {{ sum?.rules ?? '—' }} 条规则<template v-if="sum?.adopted_rules">
        （其中采纳入本体 {{ sum.adopted_rules }} 条）</template>
    </span>
  </div>

  <!-- ① 本体建设 ------------------------------------------------------------>
  <template v-if="tab === 'build'">
    <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy"
                  start-label="开始统一口径" running-label="协同进行中…" @start="startJob" />

    <div v-if="sum" class="panel">
      <h3>统一口径的结果</h3>
      <div class="stat">
        <div><b>{{ sum.forward['已落地'] }}</b><span>已落地</span></div>
        <div><b style="color: var(--hit)">{{ sum.forward['未落地'] }}</b><span>未落地</span></div>
        <div><b style="color: #b8860b">{{ sum.forward['判不出'] }}</b><span>判不出</span></div>
        <div><b style="color: var(--hit)">{{ sum.conflicts['待裁决'] }}</b><span>待裁决冲突</span></div>
        <div><b>{{ sum.adoptions['采纳'] }}</b><span>已采纳</span></div>
      </div>
      <div class="note"><MarkText :text="sum.note" /></div>
    </div>

    <div class="panel">
      <h3>口径冲突裁决（{{ conflicts.filter((c) => c.state === '待裁决').length }} 待裁决）</h3>
      <div class="row" style="margin-bottom: 6px">
        <input v-model="who" placeholder="裁决人署名（必填）" style="width: 180px" />
      </div>
      <div v-for="c in conflicts" :key="c.conflict_id" class="cf" :class="c.state">
        <div class="row" style="margin: 0">
          <span class="tag" :class="c.state === '待裁决' ? 't-hit' : 't-pass'">{{ c.state }}</span>
          <b>{{ c.group_key }}</b>
          <span class="tag t-pend">{{ c.kind }}</span>
          <code class="muted" style="font-size: 11px">{{ c.conflict_id }}</code>
        </div>
        <div style="font-size: 12px; margin: 4px 0"><MarkText :text="c.detail" /></div>
        <div v-if="c.state === '待裁决'" class="row" style="margin: 0">
          <input v-model="form['r_' + c.conflict_id]" placeholder="裁决结论（已裁决时必填）"
                 style="flex: 1" />
          <button :disabled="!!busy" @click="resolveConflict(c, '已裁决')">已裁决</button>
          <button :disabled="!!busy" @click="resolveConflict(c, '已忽略')">忽略</button>
        </div>
        <div v-else class="muted" style="font-size: 12px">
          {{ c.resolved_by }} · {{ c.resolution || '（忽略，未写结论）' }}
        </div>
      </div>
      <div v-if="!conflicts.length" class="muted">没有冲突记录。先跑一次统一口径。</div>
    </div>

    <div class="panel">
      <h3>待采纳入本体（{{ pending.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 150px">归并组</th><th style="width: 62px">判定</th>
            <th style="width: 48px">出处</th><th style="width: 70px">证据</th>
            <th>依据</th><th style="width: 86px"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="g in pending" :key="g.group_key">
            <tr>
              <td><b>{{ g.group_key }}</b></td>
              <td><span class="tag" :class="verdictTag(g.verdict)">{{ g.verdict }}</span></td>
              <td class="num">{{ g.n_sources }}</td>
              <td>
                <span class="tag" :class="g.evidence === '多源印证' ? 't-pass' : 't-na'">
                  {{ g.evidence }}</span>
                <span v-if="g.has_ocr" class="tag t-pend">OCR</span>
              </td>
              <td class="muted" style="font-size: 12px"><MarkText :text="g.match_basis" /></td>
              <td>
                <span v-if="g.has_conflict" class="tag t-hit" title="先裁决冲突">冲突待裁</span>
                <button v-else-if="g.last_action === '暂缓'" @click="openAdopt(g)">暂缓·再处置</button>
                <button v-else @click="openAdopt(g)">处置</button>
              </td>
            </tr>
            <tr v-if="openGroup === g.group_key">
              <td colspan="6" class="formcell">
                <div v-if="g.suggest?.control_why" class="muted" style="font-size: 12px"><b>{{ g.suggest?.control_why }}</b></div>
                <div v-if="g.sources?.length" class="srcs">
                  <div v-for="(s, i) in g.sources" :key="i" class="src">
                    <span class="tag" :class="s.confidence === 'OCR' ? 't-pend' : 't-pass'">
                      {{ s.confidence }}</span>
                    <span class="tag t-na">{{ s.source_kind }}</span>
                    <b>{{ s.file_name }}</b> <code>{{ s.clause_no }}</code>
                    <div class="muted" style="font-size: 12px">{{ s.excerpt.slice(0, 170) }}</div>
                  </div>
                </div>
                <div class="row">
                  <input v-model="form.name" placeholder="规则名称" style="width: 210px" />
                  <select v-model="form.dimension">
                    <option value="">审核维度…</option>
                    <option v-for="d in (opts?.dimensions || [])" :key="d" :value="d">{{ d }}</option>
                  </select>
                  <select v-model="form.rule_class">
                    <option value="">规则类型…</option>
                    <option value="hard">硬规则</option>
                    <option value="mixed">混合规则</option>
                    <option value="soft">软规则</option>
                  </select>
                  <select v-model="form.control_type">
                    <option value="">控制类型…</option>
                    <option value="禁止">禁止</option>
                    <option value="警告">警告</option>
                  </select>
                  <select v-model="form.risk_class">
                    <option value="">风险类别…</option>
                    <option v-for="r in (opts?.risk_classes || [])" :key="r" :value="r">{{ r }}</option>
                  </select>
                </div>
                <div class="row">
                  <input v-model="form.exception" placeholder="例外（八要素⑦，没有就留空）"
                         style="flex: 1" />
                  <input v-model="form.owner" placeholder="责任部门" style="width: 130px" />
                </div>
                <div class="row">
                  <input v-model="form.reason" placeholder="驳回 / 暂缓的理由（这两项必填）"
                         style="flex: 1" />
                  <button :disabled="!!busy" @click="doAdopt(g)">采纳入本体</button>
                  <button :disabled="!!busy" @click="doDispose(g, '暂缓')">暂缓</button>
                  <button :disabled="!!busy" @click="doDispose(g, '驳回')">驳回</button>
                </div>
                <div v-if="g.suggest?.numbers.length" class="muted" style="font-size: 12px">
                  组内出现的数值：{{ g.suggest.numbers.join('、') }}
                  —— <b>OCR 来源的数值不得直接当硬阈值</b>，契约校验卡着
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div v-if="!pending.length" class="muted">
        没有待采纳的组。要么都已落地，要么都处置过了。
      </div>
    </div>

    <div v-if="adoptions.length" class="panel">
      <h3>处置台账（{{ adoptions.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 60px">动作</th><th style="width: 150px">归并组</th>
            <th style="width: 92px">规则</th><th style="width: 90px">署名</th>
            <th style="width: 140px">时间</th><th>理由 / 说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in adoptions" :key="a.adoption_id">
            <td><span class="tag" :class="a.action === '采纳' ? 't-pass'
              : a.action === '驳回' ? 't-hit' : 't-pend'">{{ a.action }}</span></td>
            <td>{{ a.group_key }}</td>
            <td class="mono">
              <router-link v-if="a.rule_id" :to="`/asset/rules/${a.rule_id}`">
                {{ a.rule_id }}</router-link>
              <span v-else class="muted">—</span>
            </td>
            <td>{{ a.adopted_by }}</td>
            <td class="muted" style="font-size: 12px">{{ a.adopted_at?.slice(0, 19) }}</td>
            <td class="muted" style="font-size: 12px">
              <MarkText :text="a.reason || (a.rule_name ? a.rule_name + '（' + a.rule_status + '）' : '')" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ② 本体展示 ------------------------------------------------------------>
  <template v-else-if="tab === 'show'">
    <Async :load="loadRules" :deps="deps">
      <template #default="{ data }">
        <div v-if="data.summary" class="panel">
          <h3>本体现状</h3>
          <div class="stat">
            <div><b>{{ data.summary.total }}</b><span>规则总数</span></div>
            <div><b>{{ data.summary.compiled }}</b><span>可编译</span></div>
            <div>
              <b style="color: var(--accent)">{{ data.summary.executable }}</b>
              <span>执行集（当前数据真能跑）</span>
            </div>
            <div>
              <b style="color: var(--muted)">
                {{ data.summary.total - data.summary.compiled }}</b><span>未编译</span>
            </div>
          </div>
          <div class="bar3" style="margin-top: 10px">
            <i :style="{ width: (data.summary.executable / data.summary.total * 100) + '%',
                         background: 'var(--accent)' }" />
            <i :style="{ width: ((data.summary.compiled - data.summary.executable)
                         / data.summary.total * 100) + '%', background: '#9ec5f0' }" />
            <i :style="{ width: ((data.summary.total - data.summary.compiled)
                         / data.summary.total * 100) + '%', background: '#dfe3e8' }" />
          </div>
          <div class="note"><MarkText :text="data.summary.note" /></div>
        </div>

        <div v-if="sum" class="panel">
          <h3>双向对账</h3>
          <div class="two">
            <div>
              <h4>制度 → 落地（{{ sum.forward['总计'] }} 个归并组）</h4>
              <table>
                <tbody>
                  <tr v-for="(v, k) in sum.forward" :key="k" v-show="k !== '总计'">
                    <td><span class="tag" :class="verdictTag(String(k))">{{ k }}</span></td>
                    <td class="num"><b>{{ v }}</b></td>
                    <td class="muted" style="font-size: 12px">
                      {{ k === '判不出' ? '来源写的是类别代号，对不到唯一文件——不下断言'
                        : k === '未落地' ? '有可比对文件里的条款没被任何规则引用' : '条款号精确命中' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div>
              <h4>规则 → 依据（{{ sum.reverse_total }} 条规则）</h4>
              <!-- **这一列全 0 的时候必须说清是「没算」还是「真没依据」。**
                   实测：页面上点一次「装载规则资产」（第③步）就会把
                   onto_rule_cover 级联清空，这一列立刻全变 0——
                   而真的 0 条有依据要去查规则，没算只要补跑一次统一口径。 -->
              <div v-for="c in staleWarns" :key="c.check" class="stale">
                <span class="tag t-hit">{{ c.check }}</span>
                <span><MarkText :text="c.detail" /></span>
              </div>
              <table>
                <tbody>
                  <tr v-for="(v, k) in sum.reverse" :key="k">
                    <td><span class="tag" :class="stateTag(String(k))">{{ k }}</span></td>
                    <td class="num"><b>{{ v }}</b></td>
                    <td class="muted" style="font-size: 12px">
                      {{ k === '编号对不上' ? '来源是款级/表内行/问答序号，不是没有依据'
                        : k === '查无依据' ? '文件与编号都对上、那一条确实没引用它'
                        : k === '来源未写明' ? '来源写的是类别代号而不是文件名，无法比对'
                        : k === '仅规则表' ? '只有 xlsx 规则表引用，可能是业务惯例' : '证据链通了' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel">
          <h3>规则清单</h3>
          <div class="row">
            <select v-model="dim">
              <option value="">全部维度</option>
              <option v-for="d in DIMS" :key="d" :value="d">{{ d }}</option>
            </select>
            <select v-model="cls">
              <option value="">全部类型</option>
              <option value="hard">硬规则</option>
              <option value="mixed">混合规则</option>
              <option value="soft">软规则（必须人工复核）</option>
            </select>
            <input v-model="q" placeholder="搜规则号或名称" style="width: 180px" />
            <span class="muted">共 {{ data.total }} 条</span>
          </div>
          <table>
            <thead>
              <tr>
                <th style="width: 92px">规则</th><th>名称</th>
                <th style="width: 50px">类型</th><th style="width: 50px">控制</th>
                <th style="width: 78px">配置器</th><th style="width: 104px">状态</th>
                <th style="width: 96px">依据</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in data.items" :key="r.rule_id">
                <td class="mono">
                  <router-link :to="`/asset/rules/${r.rule_id}`">{{ r.rule_id }}</router-link>
                </td>
                <td>{{ r.rule_name }}</td>
                <td>
                  <span class="tag" :class="r.rule_class === 'soft' ? 't-pend' : 't-na'">
                    {{ { hard: '硬', mixed: '混合', soft: '软' }[r.rule_class] }}</span>
                </td>
                <td>
                  <span class="tag" :class="r.control_type === '禁止' ? 't-block' : 't-warn'">
                    {{ r.control_type }}</span>
                </td>
                <td class="mono">{{ r.algo_code || '—' }}</td>
                <td>
                  <span v-if="r.demo_executable" class="tag t-pass">执行集</span>
                  <span v-else-if="r.compiled" class="tag t-pend">已编译·缺数据</span>
                  <span v-else class="tag t-na">未编译</span>
                </td>
                <td>
                  <span v-for="c in cover.filter((x) => x.rule_id === r.rule_id)" :key="c.rule_id"
                        class="tag" :class="stateTag(c.state)">{{ c.state }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </Async>

    <div class="panel">
      <h3>归并组与多源证据（{{ groups.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 150px">归并组</th><th style="width: 62px">判定</th>
            <th style="width: 44px">出处</th><th style="width: 70px">证据</th>
            <th style="width: 120px">落在哪条规则</th><th>依据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in groups" :key="g.group_key">
            <td><b>{{ g.group_key }}</b></td>
            <td><span class="tag" :class="verdictTag(g.verdict)">{{ g.verdict }}</span></td>
            <td class="num">{{ g.n_sources }}</td>
            <td>
              <span class="tag" :class="g.evidence === '多源印证' ? 't-pass' : 't-na'">
                {{ g.evidence }}</span>
            </td>
            <td class="mono" style="font-size: 12px">
              <router-link v-for="rid in g.rule_ids" :key="rid" :to="`/asset/rules/${rid}`"
                           style="margin-right: 6px">{{ rid }}</router-link>
              <span v-if="!g.rule_ids.length" class="muted">—</span>
            </td>
            <td class="muted" style="font-size: 12px"><MarkText :text="g.match_basis.slice(0, 150)" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <Async :load="() => api.lookupTables()">
      <template #default="{ data: lt }">
        <div class="panel">
          <h3>查找表（改表不改规则）</h3>
          <table>
            <thead>
              <tr>
                <th>表</th><th>类型</th><th class="num">行数</th>
                <th class="num">源表行数</th><th>未命中行为</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in (lt as any).items" :key="t.table_id">
                <td>{{ t.table_id }}</td>
                <td class="mono">{{ t.kind }}</td>
                <td class="num">{{ t.row_count }}</td>
                <td class="num">{{ t.source_rows ?? '—' }}</td>
                <td><span class="tag t-pend">{{ t.on_miss }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </Async>
  </template>

  <!-- ③ 本体关系 ------------------------------------------------------------>
  <template v-else-if="tab === 'rel'">
    <div v-if="rel" class="panel">
      <h3>关系总览（本体 {{ rel.ontology_version }}）</h3>
      <div class="stat">
        <div v-for="(v, k) in rel.counts" :key="k">
          <b :style="k === '待确认的冲突关系' && v ? 'color: var(--hit)' : ''">{{ v }}</b>
          <span>{{ k }}</span>
        </div>
      </div>
      <div class="note"><MarkText :text="rel.note" /></div>
    </div>

    <div v-if="rel" class="panel">
      <h3>规则 ↔ 规则（{{ rel.rule_relations.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 92px">规则</th><th style="width: 100px">关系</th>
            <th style="width: 180px">对方</th><th style="width: 68px">缺口</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rel.rule_relations" :key="i" :class="{ warn: r.needs_confirm }">
            <td class="mono">
              <router-link :to="`/asset/rules/${r.rule_id}`">{{ r.rule_id }}</router-link>
            </td>
            <td>
              <span class="tag" :class="r.rel_type === 'conflicts_with' ? 't-hit' : 't-na'">
                {{ { overrides: '覆盖', priority_over: '优先于',
                     conflicts_with: '口径冲突' }[r.rel_type] || r.rel_type }}</span>
              <span v-if="r.needs_confirm" class="tag t-pend">待确认</span>
            </td>
            <td style="font-size: 12px">{{ r.other_rule }}</td>
            <td class="mono">{{ r.gap_id || '—' }}</td>
            <td class="muted" style="font-size: 12px"><MarkText :text="(r.note || '').slice(0, 160)" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="rel" class="panel">
      <h3>多源印证组（{{ rel.multi_source.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 150px">归并组</th><th style="width: 44px">出处</th>
            <th style="width: 62px">判定</th><th style="width: 190px">来源类型</th>
            <th>落在哪条规则</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in rel.multi_source" :key="m.group_key">
            <td><b>{{ m.group_key }}</b></td>
            <td class="num">{{ m.n_sources }}</td>
            <td><span class="tag" :class="verdictTag(m.verdict)">{{ m.verdict }}</span></td>
            <td>
              <span v-for="k in m.kinds" :key="k" class="tag t-na">{{ k }}</span>
            </td>
            <td class="mono" style="font-size: 12px">{{ m.rule_ids.join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

  </template>

  <!-- ④ 本体图谱 ------------------------------------------------------------>
  <template v-else-if="tab === 'graph'">
    <div v-if="graph" class="panel">
      <h3>规则关系图谱（{{ graph.rule_edges.length }} 条边）</h3>
      <svg :viewBox="`0 0 ${W} ${H}`" class="g">
        <g v-for="(e, i) in graph.rule_edges" :key="'r' + i">
          <line :x1="rulePos(e.rule_id).x + 56" :y1="rulePos(e.rule_id).y"
                :x2="rulePos(e.other_rule).x - 56" :y2="rulePos(e.other_rule).y"
                :stroke="REL_COLOR[e.rel_type] || '#c9d3de'" stroke-width="1.4"
                :stroke-dasharray="e.needs_confirm ? '5 3' : undefined" opacity="0.8" />
        </g>
        <g v-for="id in ruleCols.left" :key="'l' + id">
          <rect :x="rulePos(id).x - 56" :y="rulePos(id).y - 11" width="112" height="22" rx="3"
                fill="#eef4fb" stroke="var(--brand)" stroke-width="1" />
          <text :x="rulePos(id).x" :y="rulePos(id).y + 4" class="nid">{{ id }}</text>
        </g>
        <g v-for="id in ruleCols.right" :key="'rt' + id">
          <rect :x="rulePos(id).x - 56" :y="rulePos(id).y - 11" width="112" height="22" rx="3"
                :fill="id.startsWith('TR-') ? '#f6f8fa' : '#fdecea'"
                :stroke="id.startsWith('TR-') ? '#c9d3de' : 'var(--hit)'" stroke-width="1" />
          <text :x="rulePos(id).x" :y="rulePos(id).y + 4" class="nid">
            {{ id.length > 14 ? id.slice(0, 13) + '…' : id }}</text>
        </g>
      </svg>
      <div class="note"><MarkText :text="graph.note" /></div>
    </div>
  </template>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>
</template>

<style scoped>
/* 数对不上时的提示。和本体工场那一页同一套样式——
   同一件事在两页长得不一样，客户会以为是两件事。 */
.stale {
  margin: 6px 0 8px;
  padding: 7px 10px;
  font-size: 12px;
  border: 1px solid var(--hit, #e0b4b4);
  border-left: 3px solid var(--hit, #c0392b);
  border-radius: 8px;
  line-height: 1.6;
}
button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.bar3 { display: flex; height: 10px; background: #eef1f5; border-radius: 5px; overflow: hidden; }
.bar3 > i { display: block; height: 100%; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.two h4 { margin: 6px 0 4px; font-size: 13px; }
.cf { border: 1px solid var(--line); border-radius: 4px; padding: 8px 10px; margin-top: 8px; }
.cf.待裁决 { border-color: var(--hit); background: #fffafa; }
.formcell { background: #fafbfc; }
.srcs { margin: 6px 0; }
.src { border-left: 2px solid var(--line); padding: 2px 0 2px 8px; margin-bottom: 5px; }
tr.warn td { background: #fffafa; }
.g { width: 100%; height: 420px; }
.g .nid { font-size: 11px; text-anchor: middle; fill: var(--ink); font-weight: 600; }
.g .nlb { font-size: 10px; text-anchor: middle; fill: var(--muted); }
.g .el { font-size: 9px; text-anchor: middle; fill: var(--muted); }
.sw { display: inline-block; width: 16px; height: 3px; margin-right: 4px; vertical-align: middle; }
</style>
