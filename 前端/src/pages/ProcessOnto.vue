<script setup lang="ts">
/**
 * 栏目四 · 流程本体。五段里用到三段：本体建设 / 本体展示 / 本体图谱。 *
 * 2026-10-21 隐藏了「存储」页签（按表名/行数列出本体落在哪些库表）。
 * 接口 `/api/onto/storage` 与数据都保留，只是不在这一页出现；
 * 恢复 = 把页签按钮、Tab 类型里的 'store'、pickTab 的那一支和模板块加回来。
 * （「本体关系」在流程里就是迁移边，所以并进图谱与展示，不单列一个空页签。）
 *
 * **和规则本体最大的差别，页面上必须说清楚**：
 * 规则本体的统一口径是智能体协同跑出来的；流程本体的节点与迁移是
 * **确定性解析器**从真实审批记录里数出来的，没有 Agent，也不调模型。
 * 不说清楚，看页面的人会以为这里也有一条智能体链在跑。
 *
 * 节点与迁移全部数自真实审批记录，不是设计出来的。第一版解析器漏了 24%
 * （330 条记成 255 条）而且不报错，就是靠「本体节点 vs 实测出现次数」
 * 这种对账发现的——所以这个对账是本体建设那一段的主角，不是附录。
 *
 * **漂移核对不猜映射**：审批记录里的节点名（开始/共享审核/部门负责人…）
 * 和本体里的节点名（提单/共享审核·初审/部门负责人审批…）不是同一套写法，
 * 两套名字之间的映射是**口径决策，不是技术判断**。所以只认「精确相等」
 * 和「本体名以实测名开头」，其余标「未匹配」或「多个候选」，由人来定。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AgentRuntime from '../components/AgentRuntime.vue'
import Async from '../components/Async.vue'
import { api } from '../api'
import type {
  ExMeta, ExProgress, OntoCandidate, ProcBuild, ProcGraph, StorageRow,
} from '../api'

type Tab = 'build' | 'show' | 'graph'
const tab = ref<Tab>('build')

const build = ref<ProcBuild | null>(null)
const graph = ref<ProcGraph | null>(null)
const err = ref('')

// 本体构建是**一条共用的链**（素材感知 → 字段与枚举候选 → 流程候选 → 候选对齐）：
// 它同时产出业务本体和流程本体的候选，两页各看自己那一片。
// 拆成两条链的代价是两套进度、两套基线，而它们读的是同一批素材。
const prog = ref<ExProgress | null>(null)
const meta = ref<ExMeta | null>(null)
const cands = ref<OntoCandidate[]>([])
const busy = ref('')
const msg = ref('')
const who = ref('')
const reason = ref('')
let timer: number | undefined

async function poll() {
  try {
    prog.value = await api.ontoBuildProgress()
    if (prog.value.status !== 'running') {
      stopPoll()
      await loadCands()
    }
  } catch {
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
async function loadCands() {
  const [a, b] = await Promise.all([
    api.ontoCandidates({ kind: '流程节点', limit: 200 }),
    api.ontoCandidates({ kind: '流程迁移', limit: 200 }),
  ])
  cands.value = [...a.items, ...b.items]
}
async function startJob() {
  busy.value = 'run'
  err.value = ''
  try {
    const r = await api.ontoBuildRun()
    msg.value = r.ok ? '已启动本体构建 ' + r.job_id : (r.reason || '已有作业在运行')
    if (r.ok) {
      await poll()
      startPoll()
    }
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = ''
  }
}
async function dispose(c: OntoCandidate, action: '驳回' | '暂缓') {
  busy.value = 'd'
  err.value = ''
  try {
    await api.ontoCandDispose(c.cand_id, { action, by: who.value, reason: reason.value })
    msg.value = c.cand_name + ' 已标「' + action + '」'
    await loadCands()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = ''
  }
}
const vTag = (v: string) =>
  v === '建议新增' ? 't-pend' : v === '层间不一致' ? 't-hit' : 't-na'

async function load(t: Tab) {
  err.value = ''
  try {
    if (t === 'build' && !build.value) build.value = await api.procBuild()
    if (t === 'graph' && !graph.value) graph.value = await api.procGraph()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  }
}
function pickTab(t: Tab) {
  tab.value = t
  load(t)
}

// 图谱几何：按后端算好的 rank 分列（**分层在后端算**，前端只摆位置——
// 前端估出来的层次会随数据变，两次打开图形不一样，人就没法把
// 「图变了」当成「本体变了」的信号）。
const W = 760
const LANE = 150
const laneOf = computed(() => {
  const g = graph.value
  const m: Record<string, { x: number; y: number }> = {}
  if (!g) return m
  const byRank: Record<number, string[]> = {}
  g.nodes.filter((n) => !n.off_path).forEach((n) => {
    byRank[n.rank as number] = byRank[n.rank as number] || []
    byRank[n.rank as number].push(n.node_id)
  })
  const ranks = Object.keys(byRank).map(Number).sort((a, b) => a - b)
  ranks.forEach((r, ri) => {
    byRank[r].forEach((id, i) => {
      m[id] = {
        x: 70 + (ri * (W - 160)) / Math.max(1, ranks.length - 1),
        y: 60 + i * 76 - ((byRank[r].length - 1) * 76) / 2,
      }
    })
  })
  g.nodes.filter((n) => n.off_path).forEach((n, i) => {
    m[n.node_id] = { x: 110 + i * 200, y: 300 }
  })
  return m
})

onMounted(async () => {
  load('build')
  meta.value = await api.ontoBuildMeta().catch(() => null)
  await poll()
  if (prog.value?.status === 'running') startPoll()
  await loadCands().catch(() => undefined)
})
onUnmounted(stopPoll)
</script>

<template>
  <h2>流程本体</h2>

  <div class="row">
    <button :class="{ on: tab === 'build' }" @click="pickTab('build')">建立</button>
    <button :class="{ on: tab === 'show' }" @click="pickTab('show')">展示</button>
    <button :class="{ on: tab === 'graph' }" @click="pickTab('graph')">图谱</button>
  </div>

  <!-- ① 本体建设 ------------------------------------------------------------>
  <template v-if="tab === 'build'">
    <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy"
                  start-label="开始本体构建" running-label="协同进行中…" @start="startJob" />

    <div v-if="cands.length" class="panel">
      <h3>流程候选（{{ cands.length }}）</h3>
      <div class="row">
        <input v-model="who" placeholder="处置人署名（必填）" style="width: 170px" />
        <input v-model="reason" placeholder="驳回 / 暂缓的理由（必填）" style="flex: 1" />
        <span class="muted" style="font-size: 12px">
          采纳（写进 本体_v0.1.yaml）在
          <router-link to="/asset/biz">业务本体 · 建立</router-link> 那一页做，
          那里有 YAML 补丁预览
        </span>
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 68px">类型</th><th style="width: 190px">候选</th>
            <th style="width: 76px">判定</th><th class="num" style="width: 46px">次数</th>
            <th>出处与依据</th><th style="width: 100px"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in cands" :key="c.cand_id">
            <td><span class="tag t-na">{{ c.kind }}</span></td>
            <td><b>{{ c.cand_name }}</b></td>
            <td><span class="tag" :class="vTag(c.verdict)">{{ c.verdict }}</span></td>
            <td class="num">{{ c.occurrences ?? '—' }}</td>
            <td style="font-size: 12px">
              <div class="muted">{{ c.evidence }}</div>
              <div><MarkText :text="c.basis" /></div>
            </td>
            <td>
              <span v-if="c.last_action" class="tag t-na">已{{ c.last_action }}</span>
              <template v-else>
                <button class="mini" :disabled="!!busy" @click="dispose(c, '暂缓')">暂缓</button>
                <button class="mini" :disabled="!!busy" @click="dispose(c, '驳回')">驳回</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="build" class="panel">
      <h3>建设来源</h3>
      <div class="muted" style="font-size: 12px">{{ build.built_by }}</div>
      <div class="stat">
        <div v-for="(v, k) in build.source" :key="k"><b>{{ v }}</b><span>{{ k }}</span></div>
      </div>
    </div>

    <div v-if="build" class="panel">
      <h3>漂移核对</h3>
      <div class="stat">
        <div><b style="color: var(--pass)">{{ build.drift_summary['对上的'] }}</b><span>对上的</span></div>
        <div>
          <b style="color: #b8860b">{{ build.drift_summary['多个候选'] }}</b>
          <span>多个候选（{{ build.drift_summary['多个候选出现次数'] }} 次）</span>
        </div>
        <div>
          <b style="color: var(--hit)">{{ build.drift_summary['未匹配'] }}</b>
          <span>未匹配（{{ build.drift_summary['未匹配出现次数'] }} 次）</span>
        </div>
        <div>
          <b style="color: var(--muted)">{{ build.drift_summary['本体里有而记录里没见过'] }}</b>
          <span>本体里有、记录里没见过</span>
        </div>
      </div>
      <table style="margin-top: 8px">
        <thead>
          <tr>
            <th style="width: 150px">审批记录里的节点名</th><th class="num" style="width: 70px">出现</th>
            <th class="num" style="width: 62px">涉及单据</th><th style="width: 80px">匹配</th>
            <th style="width: 60px">节点</th><th>本体里的名字</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in build.drift" :key="d.measured_node"
              :class="{ warn: d.match === '未匹配' || d.match === '多个候选' }">
            <td><b>{{ d.measured_node }}</b></td>
            <td class="num">{{ d.n }}</td>
            <td class="num">{{ d.bills }}</td>
            <td>
              <span class="tag" :class="d.match === '精确' ? 't-pass' : d.match === '前缀'
                ? 't-pend' : 't-hit'">{{ d.match }}</span>
            </td>
            <td class="mono">{{ d.node_id || '—' }}</td>
            <td>
              {{ d.onto_name || '' }}
              <span v-if="d.match === '多个候选'" class="muted" style="font-size: 12px">
                本体里有两个候选（初审 / 复审），<b>选哪个是口径决策</b>，系统不替你选
              </span>
              <span v-else-if="d.match === '未匹配'" class="muted" style="font-size: 12px">
                本体里找不到对应节点 —— 要么该补节点，要么该补一条名称映射
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="build.unseen_nodes.length" class="note">
        本体里有、而 {{ build.source['审批记录'] }} 条记录里<b>一次都没见过</b>的节点：
        <code v-for="n in build.unseen_nodes" :key="n.node_id" style="margin-right: 8px">
          {{ n.node_id }} {{ n.node_name }}</code>
        <br />这不一定是错——样本里没走到的分支（流程结束、复审）本来就不会出现；
        但它和「漏解析」长得一样，所以必须单独列出来让人看。
      </div>
      <div class="note"><MarkText :text="build.note" /></div>
    </div>
  </template>

  <!-- ② 本体展示 ------------------------------------------------------------>
  <template v-else-if="tab === 'show'">
    <Async :load="() => api.process()">
      <template #default="{ data }">
        <div class="panel">
          <h3>节点（{{ (data as any).nodes.length }} 个）</h3>
          <div class="chain">
            <template v-for="(n, i) in (data as any).nodes" :key="n.node_id">
              <span class="node" :class="{ bypass: n.is_bypass }">
                <code>{{ n.node_id }}</code> {{ n.node_name }}
              </span>
              <span v-if="i < (data as any).nodes.length - 1" class="arr">→</span>
            </template>
          </div>
        </div>

        <div class="panel">
          <h3>迁移（{{ (data as any).transitions.length }} 条）</h3>
          <table>
            <thead>
              <tr><th>从</th><th>到</th><th>条件</th><th>是否阻断</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="(t, i) in (data as any).transitions" :key="i">
                <td class="mono">{{ t.from_node }}</td>
                <td class="mono">{{ t.to_node }}</td>
                <td>{{ t.cond }}</td>
                <td>
                  <span class="tag" :class="t.blocking ? 't-block' : 't-pass'">
                    {{ t.blocking ? '阻断' : '不阻断' }}
                  </span>
                </td>
                <td class="muted" style="font-size: 12px; white-space: pre-wrap">
                  <MarkText :text="t.note || ''" /></td>
              </tr>
            </tbody>
          </table>
          <div class="note"><MarkText :text="(data as any).note" /></div>
        </div>

        <div class="panel">
          <h3>真实审批记录里的节点出现次数（实测）</h3>
          <table>
            <thead><tr><th>节点</th><th class="num">出现次数</th></tr></thead>
            <tbody>
              <tr v-for="m in (data as any).measured_from_real_approvals" :key="m.node">
                <td>{{ m.node }}</td>
                <td class="num">{{ m.n }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </Async>
  </template>

  <!-- ③ 本体图谱 ------------------------------------------------------------>
  <template v-else-if="tab === 'graph'">
    <div v-if="graph" class="panel">
      <h3>流程图谱（本体 {{ graph.version }}）</h3>
      <svg :viewBox="`0 0 ${W} 360`" class="g">
        <g v-for="(e, i) in graph.edges" :key="'e' + i">
          <line :x1="(laneOf[e.from_node]?.x || 0) + 58" :y1="laneOf[e.from_node]?.y"
                :x2="(laneOf[e.to_node]?.x || 0) - 58" :y2="laneOf[e.to_node]?.y"
                :stroke="e.blocking ? '#2f6fb5' : 'var(--hit)'" stroke-width="1.5"
                :stroke-dasharray="e.blocking ? undefined : '5 3'" opacity="0.85" />
        </g>
        <g v-for="n in graph.nodes" :key="n.node_id">
          <rect :x="(laneOf[n.node_id]?.x || 0) - 58" :y="(laneOf[n.node_id]?.y || 0) - 17"
                width="116" height="34" rx="4"
                :fill="n.off_path ? '#fafbfc' : n.measured ? '#eef4fb' : '#f6f8fa'"
                :stroke="n.off_path ? '#c9d3de' : 'var(--brand)'" stroke-width="1.2"
                :stroke-dasharray="n.off_path || n.is_bypass ? '4 3' : undefined" />
          <text :x="laneOf[n.node_id]?.x" :y="(laneOf[n.node_id]?.y || 0) - 3" class="nid">
            {{ n.node_id }}
            {{ n.node_name.length > 9 ? n.node_name.slice(0, 8) + '…' : n.node_name }}</text>
          <text :x="laneOf[n.node_id]?.x" :y="(laneOf[n.node_id]?.y || 0) + 11" class="nlb">
            {{ n.measured === null ? '实测 —' : '实测 ×' + n.measured }}</text>
        </g>
      </svg>
      <div class="note"><MarkText :text="graph.note" /></div>
    </div>
  </template>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>
</template>

<style scoped>
button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
button.mini { padding: 2px 7px; font-size: 12px; }
tr.warn td { background: #fffafa; }
.g { width: 100%; height: 360px; }
.g .nid { font-size: 11px; text-anchor: middle; fill: var(--ink); font-weight: 600; }
.g .nlb { font-size: 10px; text-anchor: middle; fill: var(--muted); }
</style>
