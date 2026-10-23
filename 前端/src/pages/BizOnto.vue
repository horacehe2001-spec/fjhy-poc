<script setup lang="ts">
/**
 * 栏目三 · 业务本体。四段：建立 / 展示 / 关系 / 图谱。 *
 * 2026-10-21 隐藏了「存储」页签（按表名/行数列出本体落在哪些库表）。
 * 接口 `/api/onto/storage` 与数据都保留，只是不在这一页出现；
 * 恢复 = 把页签按钮、Tab 类型里的 'store'、pickTab 的那一支和模板块加回来。
 *
 * 这一页是按一句要求做的：**所有东西都摆在明面上操作**。
 * 在它之前，业务本体这一步整个发生在系统之外：
 *   · 924 行的 本体_v0.1.yaml 是**人读客户材料手写的**（不是智能体跑出来的）
 *   · 装进库要在命令行跑 `python -m app.ontology.loader`
 *   · 页面上看不到源文件、没有入口，客户没法自己再跑一遍，也查不到证据链
 *
 * 所以「建立」这一段要同时回答四个问题，缺一个都算藏着：
 *   源头是什么、谁写的    → **L0 素材感知**的 S/F 输出（含 sha256、是否与库同步）
 *   素材有哪些            → 同一个 Agent（单据明细表 / 条款 / 审批记录）
 *   机器能自动发现什么    → 协同链 + 候选清单（每条带出处与出现次数）
 *   采纳会改什么          → **先给 YAML diff 预览**，确认才写，写完给行号与备份路径
 *
 * 源头与规则资产装载**原来是两个独立面板**，2026-10-19 按要求并进了 L0 素材感知
 * （协同全景上那一格因此叫「素材感知（源头、规则资产装载）」）。
 * 搬的是事实不是删证据：源文件 sha256、与库是否同步、装载会动什么、
 * 现在多少规则/发布，全在那个 Agent 的输出与小字里。
 * 装载本身 2026-10-20 做成了按钮（OntoLoadSteps），闸在后端装载器身上。
 *
 * **边界也要写在页面上，不含糊**：这条链能从素材里数出有硬证据的东西，
 * 但替代不了人读制度划实体、定概念语义。产出叫候选，采纳是人的动作。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AgentRuntime from '../components/AgentRuntime.vue'
import OntoLoadSteps from '../components/OntoLoadSteps.vue'
import Async from '../components/Async.vue'
import { api } from '../api'
import type {
  ExMeta, ExProgress, OntoBuildSummary, OntoCandAction, OntoCandidate, OntoGraph,
  OntoPatchPreview, OntoRelations, OntoSourceFile, StorageRow,
} from '../api'

type Tab = 'build' | 'show' | 'rel' | 'graph'
const tab = ref<Tab>('build')

const prog = ref<ExProgress | null>(null)
const meta = ref<ExMeta | null>(null)
const sum = ref<OntoBuildSummary | null>(null)
const src = ref<OntoSourceFile | null>(null)
const cands = ref<OntoCandidate[]>([])
const actions = ref<OntoCandAction[]>([])
const rel = ref<OntoRelations | null>(null)
const graph = ref<OntoGraph | null>(null)

const busy = ref('')
const err = ref('')
const msg = ref('')
const who = ref('')
const openCand = ref('')
const pv = ref<OntoPatchPreview | null>(null)
const target = ref('')
const reason = ref('')
const verdictFilter = ref('')
let timer: number | undefined

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
    prog.value = await api.ontoBuildProgress()
    if (prog.value.status !== 'running') {
      stopPoll()
      await reload()
    }
  } catch {
    // 轮询失败不清空快照：清空会让页面在网络抖一下时整个塌掉
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

const reload = () =>
  run('reload', async () => {
    const [s, c, a] = await Promise.all([
      api.ontoBuildSummary(),
      api.ontoCandidates({ limit: 300 }),
      api.ontoBuildActions(),
    ])
    sum.value = s
    src.value = s.source
    cands.value = c.items
    actions.value = a.items
  })

const startJob = () =>
  run('run', async () => {
    const r = await api.ontoBuildRun()
    if (!r.ok) {
      msg.value = r.reason || '已有作业在运行'
      return
    }
    msg.value = '已启动本体构建 ' + r.job_id
    await poll()
    startPoll()
  })

function pickTab(t: Tab) {
  tab.value = t
  if (t === 'build' && !sum.value) reload()
  if (t === 'rel' && !rel.value) run('rel', async () => { rel.value = await api.ontoRelations() })
  if (t === 'graph' && !graph.value) run('g', async () => { graph.value = await api.ontoGraph() })
}

const openPreview = (c: OntoCandidate) =>
  run('pv', async () => {
    openCand.value = openCand.value === c.cand_id ? '' : c.cand_id
    pv.value = null
    reason.value = ''
    target.value = c.yaml_target || ''
    if (openCand.value) {
      pv.value = await api.ontoCandPreview(c.cand_id, target.value || undefined)
    }
  })

const refreshPreview = (c: OntoCandidate) =>
  run('pv', async () => { pv.value = await api.ontoCandPreview(c.cand_id, target.value || undefined) })

const doAdopt = (c: OntoCandidate) =>
  run('adopt', async () => {
    const r = await api.ontoCandAdopt(c.cand_id, { by: who.value, yaml_target: target.value })
    msg.value = '已写入 本体_v0.1.yaml 第 ' + r.yaml_line + ' 行'
      + (r.new_id ? '（新编号 ' + r.new_id + '）' : '')
      + '；备份 ' + r.backup.split(/[\\/]/).pop()
      + '；重装 ' + (r.reloaded ? '成功' : '**失败：' + (r.reload_error || '') + '**')
    openCand.value = ''
    await reload()
  })

const doDispose = (c: OntoCandidate, action: '驳回' | '暂缓') =>
  run('dispose', async () => {
    await api.ontoCandDispose(c.cand_id, { action, by: who.value, reason: reason.value })
    msg.value = c.cand_name + ' 已标「' + action + '」'
    openCand.value = ''
    await reload()
  })

const shown = computed(() =>
  verdictFilter.value ? cands.value.filter((c) => c.verdict === verdictFilter.value)
    : cands.value)

const vTag = (v: string) =>
  v === '建议新增' ? 't-pend' : v === '层间不一致' ? 't-hit' : 't-na'

// 图谱几何：圆周等分，确定性——两次打开一模一样。
// 谁是孤点由后端给的 isolated 决定，前端不自己算。
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
const REL_COLOR: Record<string, string> = {
  包含: '#2f6fb5', 关联: '#7a9ec4', 流转: '#2e8b57', 归属: '#8e6fb5',
}

onMounted(async () => {
  meta.value = await api.ontoBuildMeta().catch(() => null)
  await poll()
  if (prog.value?.status === 'running') startPoll()
  await reload()
})
onUnmounted(stopPoll)
</script>

<template>
  <h2>业务本体</h2>

  <div class="row">
    <button :class="{ on: tab === 'build' }" @click="pickTab('build')">建立</button>
    <button :class="{ on: tab === 'show' }" @click="pickTab('show')">展示</button>
    <button :class="{ on: tab === 'rel' }" @click="pickTab('rel')">关系</button>
    <button :class="{ on: tab === 'graph' }" @click="pickTab('graph')">图谱</button>
    <span v-if="src" class="muted" style="margin-left: auto">
      本体 {{ src.version }}（{{ src.maturity }}）·
      {{ src.counts['实体'] }} 实体 / {{ src.counts['概念'] }} 概念 /
      {{ src.counts['枚举取值'] }} 枚举取值
    </span>
  </div>

  <!-- ① 建立 --------------------------------------------------------------->
  <template v-if="tab === 'build'">
    <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy"
                  start-label="开始本体构建" running-label="协同进行中…" @start="startJob" />

    <!-- 装载两步。原来只有命令行，2026-10-20 做成按钮：
         闸在后端装载器身上，页面只负责把结论显示出来、把③灰掉。 -->
    <OntoLoadSteps @loaded="reload()" />

    <div v-if="sum" class="panel">
      <h3>候选（{{ cands.length }}）</h3>
      <div class="sub"><MarkText :text="sum.note" /></div>
      <div class="row">
        <input v-model="who" placeholder="处置人署名（必填）" style="width: 170px" />
        <select v-model="verdictFilter">
          <option value="">全部判定</option>
          <option value="建议新增">建议新增（{{ sum.by_verdict['建议新增'] }}）</option>
          <option value="层间不一致">层间不一致（{{ sum.by_verdict['层间不一致'] }}）</option>
          <option value="已在本体">已在本体（{{ sum.by_verdict['已在本体'] }}）</option>
        </select>
        <span class="muted" style="font-size: 12px">
          采纳会<b>真的改源文件 {{ src?.path }}</b>（先备份、插完 safe_load 验、不过就回滚），
          然后自动重装
        </span>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 68px">类型</th><th style="width: 150px">候选</th>
            <th style="width: 76px">判定</th><th style="width: 46px">次数</th>
            <th>出处与依据</th><th style="width: 76px"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="c in shown" :key="c.cand_id">
            <tr :class="{ dim: c.verdict === '已在本体' }">
              <td><span class="tag t-na">{{ c.kind }}</span></td>
              <td><b>{{ c.cand_name }}</b></td>
              <td><span class="tag" :class="vTag(c.verdict)">{{ c.verdict }}</span></td>
              <td class="num">{{ c.occurrences ?? '—' }}</td>
              <td style="font-size: 12px">
                <div class="muted">{{ c.source_kind }} · {{ c.evidence }}</div>
                <div><MarkText :text="c.basis" /></div>
              </td>
              <td>
                <span v-if="c.last_action === '采纳'" class="tag t-pass">
                  已采纳 L{{ c.yaml_line }}</span>
                <span v-else-if="c.last_action === '驳回'" class="tag t-na">已驳回</span>
                <button v-else-if="c.verdict === '已在本体'" class="mini"
                        @click="openPreview(c)">处置</button>
                <button v-else class="mini" @click="openPreview(c)">查看补丁</button>
              </td>
            </tr>
            <tr v-if="openCand === c.cand_id">
              <td colspan="6" class="formcell">
                <div v-if="c.verdict === '已在本体'" class="note">
                  这条的判定是「已在本体」（对上 <code>{{ c.existing_ref }}</code>），
                  <b>不能采纳</b>。它进清单是为了让人看见
                  「枚举里有而样本与材料正文里都没检索到」——
                  该不该删要人判，而删枚举取值得改 YAML 对应那一行，这一步目前只能人工。
                </div>
                <template v-else>
                  <div class="row">
                    <span class="muted" style="font-size: 12px">插入目标：</span>
                    <input v-model="target" placeholder="实体号 E01 / 枚举号 EN16"
                           style="width: 160px" />
                    <button class="mini" :disabled="!!busy" @click="refreshPreview(c)">
                      重算补丁</button>
                    <span v-if="!c.yaml_target" class="tag t-hit">
                      系统判不出目标，必须人选</span>
                  </div>
                  <div v-if="pv" class="diffbox">
                    <div class="muted" style="font-size: 12px"><MarkText :text="pv.note" /></div>
                    <pre class="ctx">{{ pv.context_before }}</pre>
                    <pre class="add">{{ pv.patch }}</pre>
                    <pre class="ctx">{{ pv.context_after }}</pre>
                  </div>
                </template>
                <div class="row">
                  <input v-model="reason" placeholder="驳回 / 暂缓的理由（这两项必填）"
                         style="flex: 1" />
                  <button v-if="c.verdict !== '已在本体'" :disabled="!!busy || !pv"
                          @click="doAdopt(c)">采纳并写入 YAML</button>
                  <button :disabled="!!busy" @click="doDispose(c, '暂缓')">暂缓</button>
                  <button :disabled="!!busy" @click="doDispose(c, '驳回')">驳回</button>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div v-if="!shown.length" class="muted">没有候选。点「开始本体构建」跑一次。</div>
    </div>

    <div v-if="actions.length" class="panel">
      <h3>处置台账（{{ actions.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 56px">动作</th><th style="width: 68px">类型</th>
            <th style="width: 140px">候选</th><th style="width: 80px">署名</th>
            <th style="width: 56px">行号</th><th style="width: 60px">重装</th>
            <th>补丁 / 理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in actions" :key="a.action_id">
            <td><span class="tag" :class="a.action === '采纳' ? 't-pass'
              : a.action === '驳回' ? 't-hit' : 't-pend'">{{ a.action }}</span></td>
            <td class="muted" style="font-size: 12px">{{ a.cand_kind }}</td>
            <td style="font-size: 12px">{{ a.cand_key }}</td>
            <td>{{ a.acted_by }}</td>
            <td class="num">{{ a.yaml_line ?? '—' }}</td>
            <td>
              <span v-if="a.action !== '采纳'" class="muted">—</span>
              <span v-else class="tag" :class="a.reloaded ? 't-pass' : 't-hit'">
                {{ a.reloaded ? '成功' : '失败' }}</span>
            </td>
            <td>
              <pre v-if="a.yaml_patch" class="add small">{{ a.yaml_patch }}</pre>
              <span v-else class="muted" style="font-size: 12px"><MarkText :text="a.reason" /></span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ② 展示 --------------------------------------------------------------->
  <template v-else-if="tab === 'show'">
    <Async :load="() => api.ontology()">
      <template #default="{ data }">
        <div class="panel">
          <h3>本体现状（{{ (data as any).version }}）</h3>
          <table>
            <thead>
              <tr><th style="width: 70px">实体</th><th>名称</th>
                <th style="width: 80px">来源</th><th class="num" style="width: 60px">概念数</th></tr>
            </thead>
            <tbody>
              <tr v-for="e in (data as any).entities" :key="e.entity_id">
                <td class="mono">{{ e.entity_id }}</td>
                <td>{{ e.entity_name }}</td>
                <td class="muted" style="font-size: 12px">{{ e.source }}</td>
                <td class="num">{{ e.concepts }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="panel">
          <h3>概念可得性</h3>
          <table>
            <thead><tr><th>可得性</th><th class="num">概念数</th></tr></thead>
            <tbody>
              <tr v-for="a in (data as any).availability" :key="a.availability">
                <td>
                  <span class="tag" :class="/不可得|全空|缺列|待业务/.test(a.availability)
                    ? 't-hit' : /需|仅/.test(a.availability) ? 't-pend' : 't-pass'">
                    {{ a.availability }}</span>
                </td>
                <td class="num">{{ a.n }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="panel">
          <h3>枚举（{{ (data as any).enums.length }}）</h3>
          <table>
            <thead><tr><th style="width: 70px">枚举</th><th>名称</th>
              <th class="num" style="width: 60px">取值数</th></tr></thead>
            <tbody>
              <tr v-for="e in (data as any).enums" :key="e.enum_id">
                <td class="mono">{{ e.enum_id }}</td>
                <td>{{ e.enum_name }}</td>
                <td class="num">{{ e.values }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </Async>
  </template>

  <!-- ③ 关系 --------------------------------------------------------------->
  <template v-else-if="tab === 'rel'">
    <div v-if="rel" class="panel">
      <h3>实体 ↔ 实体（{{ rel.entity_relations.length }}）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 150px">从</th><th style="width: 56px">关系</th>
            <th style="width: 150px">到</th><th style="width: 56px">基数</th>
            <th style="width: 86px">来源</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(e, i) in rel.entity_relations" :key="i">
            <td><code>{{ e.from_entity }}</code> {{ e.from_name }}</td>
            <td><span class="tag t-na">{{ e.rel_type }}</span></td>
            <td><code>{{ e.to_entity }}</code> {{ e.to_name }}</td>
            <td class="mono">{{ e.cardinality || '—' }}</td>
            <td class="muted" style="font-size: 12px">{{ e.source || '—' }}</td>
            <td class="muted" style="font-size: 12px"><MarkText :text="(e.note || '').slice(0, 110)" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="rel" class="panel">
      <h3>规则 ↔ 概念（前 {{ rel.concept_refs.length }} 个被引用最多的）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 72px">概念</th><th style="width: 150px">名称</th>
            <th style="width: 72px">角色</th><th class="num" style="width: 46px">引用</th>
            <th>被哪些规则引用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in rel.concept_refs" :key="c.concept_id + c.role">
            <td class="mono">{{ c.concept_id }}</td>
            <td>{{ c.concept_name || '（本体里没有）' }}</td>
            <td><span class="tag" :class="c.role === 'threshold' ? 't-hit' : 't-na'">
              {{ c.role }}</span></td>
            <td class="num">{{ c.n }}</td>
            <td class="mono" style="font-size: 11px">{{ c.rules.join(' ') }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ④ 图谱 --------------------------------------------------------------->
  <template v-else-if="tab === 'graph'">
    <div v-if="graph" class="panel">
      <h3>实体图谱（本体 {{ graph.ontology_version }}）</h3>
      <svg :viewBox="`0 0 ${W} ${H}`" class="g">
        <g v-for="(e, i) in graph.entity_edges" :key="'e' + i">
          <line :x1="entPos[e.from_entity]?.x" :y1="entPos[e.from_entity]?.y"
                :x2="entPos[e.to_entity]?.x" :y2="entPos[e.to_entity]?.y"
                :stroke="REL_COLOR[e.rel_type] || '#c9d3de'" stroke-width="1.3" opacity="0.65" />
          <text :x="((entPos[e.from_entity]?.x || 0) + (entPos[e.to_entity]?.x || 0)) / 2"
                :y="((entPos[e.from_entity]?.y || 0) + (entPos[e.to_entity]?.y || 0)) / 2"
                class="el">{{ e.rel_type }}{{ e.cardinality ? ' ' + e.cardinality : '' }}</text>
        </g>
        <g v-for="en in graph.entities" :key="en.entity_id">
          <circle :cx="entPos[en.entity_id]?.x" :cy="entPos[en.entity_id]?.y"
                  :r="Math.min(26, 9 + (en.concepts || 0) * 1.1)"
                  :fill="en.isolated ? '#fdecea' : '#eef4fb'"
                  :stroke="en.isolated ? 'var(--hit)' : 'var(--brand)'" stroke-width="1.5" />
          <text :x="entPos[en.entity_id]?.x" :y="(entPos[en.entity_id]?.y || 0) + 3"
                class="nid">{{ en.entity_id }}</text>
          <text :x="entPos[en.entity_id]?.x" :y="(entPos[en.entity_id]?.y || 0) + 34"
                class="nlb">{{ en.entity_name }}（{{ en.concepts }}）</text>
        </g>
      </svg>
      <div class="row" style="gap: 14px; font-size: 12px">
        <span v-for="(c, k) in REL_COLOR" :key="k">
          <i class="sw" :style="{ background: c }" />{{ k }}
        </span>
        <span class="muted">
          孤点 {{ graph.entities.filter((e) => e.isolated).length }} 个 ·
          取不到字段的概念 {{ graph.entities.reduce((s, e) => s + (e.gap || 0), 0) }} 个
        </span>
      </div>
      <div class="note"><MarkText :text="graph.note" /></div>
    </div>
  </template>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>
</template>

<style scoped>
button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
button.mini { padding: 2px 7px; font-size: 12px; }
tr.dim td { opacity: .62; }
tr.srcrow td { background: #fffdf5; }
.formcell { background: #fafbfc; }
.diffbox { margin: 6px 0; border: 1px solid var(--line); border-radius: 4px; overflow: hidden; }
.diffbox pre { margin: 0; padding: 3px 8px; font-size: 11px; line-height: 1.5; white-space: pre-wrap; }
pre.ctx { background: #fafbfc; color: var(--muted); }
pre.add { background: #f1f8f2; border-left: 3px solid var(--pass); }
pre.add.small { font-size: 10px; max-height: 60px; overflow: auto; }
.g { width: 100%; height: 420px; }
.g .nid { font-size: 11px; text-anchor: middle; fill: var(--ink); font-weight: 600; }
.g .nlb { font-size: 10px; text-anchor: middle; fill: var(--muted); }
.g .el { font-size: 9px; text-anchor: middle; fill: var(--muted); }
.sw { display: inline-block; width: 16px; height: 3px; margin-right: 4px; vertical-align: middle; }
</style>
