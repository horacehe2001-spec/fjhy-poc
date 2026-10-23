<script setup lang="ts">
/**
 * 栏目：本体工场。**客户自己把三元本体从零搭起来的那一页。**
 *
 * 在它之前，一次完整搭建被切在三个页面上：业务本体和流程本体共用一条链却
 * 各放一个「建设」页签，规则本体是另一条链，而两个装载器根本不在页面上
 * （只能走命令行）。客户看不到「源文件 → 本体 → 规则 → 可执行」是一条线，
 * 也没有任何东西告诉他先后顺序——而顺序点反了**不报错**。
 *
 * 这一页只做两件事：把那条线画出来，把顺序钉住。
 *
 * **四条纪律，页面上不许破：**
 *
 * 1. **八步不是八个智能体。** 每张卡片标「谁干的」：②③ 是装载器、⑧ 是人，
 *    只有 ①④⑤⑥⑦ 是 Agent。不标就会变成「AI 全自动搭本体」的幻觉，
 *    而这是这个产品最不该给的错觉。
 *
 * 2. **一键只跑到 ⑦。** ⑧ 采纳改的是客户的源文件，永远由人点。
 *    页面上用一条分隔线把它隔开，不是放在同一排按钮里。
 *
 * 3. **顺序和「谁干的」都照后端给的画**（/workshop/steps），前端不自己排。
 *    顺序本身是口径（③ 必须在 ② 之后），前端排一遍就是第二份口径，
 *    而两份迟早不一样。
 *
 * 4. **没跑过也要显示库里的事实。** 第一次打开时作业快照是空的，但库里有数。
 *    只拿快照画页面，等于告诉客户「什么都没有」，而他上次明明搭过。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AgentRuntime from '../components/AgentRuntime.vue'
import OntoLoadSteps from '../components/OntoLoadSteps.vue'
import { api } from '../api'
import type { ExMeta, ExProgress, WsState, WsStep, WsSteps } from '../api'

const busy = ref('')
const err = ref('')
const msg = ref('')
const steps = ref<WsSteps | null>(null)
const st = ref<WsState | null>(null)
const prog = ref<ExProgress | null>(null)
const meta = ref<ExMeta | null>(null)
let timer: number | undefined

/** 执行单元 id → 这一次作业里它的状态。⑦ 那种一步三单元的，取最差的那个。 */
const unitStatus = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const a of prog.value?.agents || meta.value?.agents || []) m[a.id] = a.status
  return m
})

const RANK: Record<string, number> = {
  failed: 0, skipped: 1, running: 2, waiting: 3, done: 4,
}
function stepStatus(s: WsStep) {
  if (!s.covers.length) return 'human'
  const got = s.covers.map((i) => unitStatus.value[i]).filter(Boolean)
  if (!got.length) return 'waiting'
  // **取最差的那个。** 三个单元里有一个失败，这一步就是失败——
  // 取最好的会让一步里的失败被另外两步的成功盖住。
  return got.reduce((a, b) => (RANK[a] <= RANK[b] ? a : b))
}
const STATUS_CN: Record<string, string> = {
  waiting: '待运行', running: '运行中', done: '已完成',
  failed: '失败', skipped: '未执行', human: '等人点',
}
function statusTag(s: WsStep) {
  const k = stepStatus(s)
  return k === 'done' ? 't-pass' : k === 'running' ? 't-pend'
    : k === 'failed' ? 't-hit' : 't-na'
}
/** 谁干的 —— 颜色和图上一致：智能体中性、装载器次色、人用强调色。 */
function actorTag(s: WsStep) {
  return s.actor === '人' ? 't-block' : s.actor === '装载器' ? 't-pend' : 't-na'
}
/** 这一步跑出来的数字。没跑过就空着，不编。 */
function outputs(s: WsStep) {
  const out: [string, unknown][] = []
  for (const a of prog.value?.agents || []) {
    if (s.covers.includes(a.id)) {
      for (const [k, v] of Object.entries(a.outputs || {})) out.push([k, v])
    }
  }
  return out
}
/** 「本体」那一排指标。**version 要摘掉。**
 *
 * 它是版本号，不是指标——和「实体 17」「概念 121」并排显示时，
 * 客户看到的是「版本 0.1」这么一个业务数字，而上面那句话已经说过
 * 「下面报的是当前版本 0.1」，等于同一件事说两遍、还换了个身份。
 *
 * **不能让后端不给**：上面那句话的版本号就取自它，而且
 * test_没跑过也看得到库里的事实 断言了 st.onto.version 必须在。
 * 所以摘在显示这一层。
 */
const ontoMetrics = computed<[string, unknown][]>(() =>
  Object.entries(st.value?.onto || {}).filter(([k]) => k !== 'version'))

/** 库里的数对不对得上——只报对不上的那些。对得上时数字本身就是证据。 */
const staleWarns = computed(() => (st.value?.freshness || []).filter((c) => !c.ok))

function noteOf(s: WsStep) {
  return (prog.value?.agents || [])
    .filter((a) => s.covers.includes(a.id) && (a.note || a.error))
    .map((a) => a.error || a.note)
}

async function load() {
  const [a, b, c] = await Promise.all([api.wsSteps(), api.wsState(), api.wsAgents()])
  steps.value = a
  st.value = b
  meta.value = c
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

async function poll() {
  try {
    prog.value = await api.wsProgress()
    if (prog.value.status !== 'running') {
      stopPoll()
      st.value = await api.wsState()
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

const startJob = () =>
  run('run', async () => {
    const r = await api.wsRun()
    if (!r.ok) {
      err.value = r.reason || '启动失败'
      return
    }
    msg.value = '开始搭建 ①→⑦：' + r.job_id
    startPoll()
  })

onMounted(async () => {
  await run('load', load)
  await poll()
  if (prog.value?.status === 'running') startPoll()
})
onUnmounted(stopPoll)
</script>

<template>
  <h2>本体工场</h2>

  <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy" :phase-cards="false"
                start-label="一键搭建 ①→⑦" running-label="搭建进行中…" @start="startJob" />

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>

  <div v-if="steps" class="panel">
    <h3>八步</h3>
    <div class="sub"><MarkText :text="steps.note" /></div>
    <template v-for="s in steps.steps" :key="s.no">
      <!-- ⑦ 和 ⑧ 之间那条线：一键到此为止，下面是人的动作 -->
      <div v-if="!s.one_click" class="cut">
        以上一键跑完 ｜ 以下永远由人点
      </div>
      <div class="step" :class="stepStatus(s)">
        <div class="row" style="margin: 0 0 4px">
          <b class="no">{{ s.no }}</b>
          <b>{{ s.step }}</b>
          <span class="tag" :class="actorTag(s)">{{ s.actor }}</span>
          <span class="tag" :class="statusTag(s)">{{ STATUS_CN[stepStatus(s)] }}</span>
          <span class="muted" style="font-size: 11px"><MarkText :text="s.why" /></span>
        </div>
        <div v-if="s.units.length" class="muted" style="font-size: 11px">
          执行单元：<code v-for="u in s.units" :key="u.id">{{ u.agent_name }}</code>
        </div>
        <div v-if="outputs(s).length" class="row" style="margin: 6px 0 0; gap: 14px">
          <span v-for="[k, v] in outputs(s)" :key="k">
            <span class="muted">{{ k }}</span> <b>{{ v }}</b>
          </span>
        </div>
        <div v-for="(n, i) in noteOf(s)" :key="i" class="muted"
             style="font-size: 12px; margin-top: 4px">{{ n }}</div>
      </div>
    </template>
  </div>

  <!-- ②③ 也能单独点。一键是方便，不是唯一入口——
       排错时要能只跑其中一步，而且闸在后端，两条路共用同一道。 -->
  <OntoLoadSteps @loaded="load()" />

  <div v-if="st" class="panel">
    <h3>现在库里有什么</h3>
    <div class="kv">
      <b>源文件</b>
      <span>
        {{ st.source.path }} · {{ st.source.lines }} 行 · {{ st.source.written_by }} ·
        <span class="tag" :class="st.source.in_sync ? 't-pass' : 't-hit'">
          {{ st.source.in_sync ? '与库同步' : '与库不同步' }}</span>
      </span>
      <b>本体</b>
      <span>
        <span v-for="[k, v] in ontoMetrics" :key="k" style="margin-right: 12px">
          <span class="muted">{{ k }}</span> <b>{{ v }}</b>
        </span>
      </span>
      <b>规则资产</b>
      <span>
        <span v-for="(v, k) in st.assets" :key="k" style="margin-right: 12px">
          <span class="muted">{{ k }}</span> <b>{{ v }}</b>
        </span>
      </span>
      <b>候选</b>
      <span>
        <span v-for="(v, k) in st.candidates" :key="k" style="margin-right: 12px">
          <span class="muted">{{ k }}</span> <b>{{ v }}</b>
        </span>
      </span>
      <b>统一口径</b>
      <span>
        <span v-for="(v, k) in st.unify" :key="k" style="margin-right: 12px">
          <span class="muted">{{ k }}</span> <b>{{ v }}</b>
        </span>
      </span>
    </div>
    <!-- 数字对不上的时候必须说是「没算」而不是让人把 0 读成「没覆盖」：
         真的 0 条覆盖要去查规则，还没算只要补跑第⑦步，处置完全相反。 -->
    <div v-for="c in staleWarns" :key="c.check" class="stale">
      <span class="tag t-hit">{{ c.check }}</span>
      <span><MarkText :text="c.detail" /></span>
    </div>
  </div>
</template>

<style scoped>
.step {
  border: 1px solid var(--line, #e3e8ef);
  border-left: 3px solid var(--line, #e3e8ef);
  border-radius: 8px;
  padding: 8px 10px;
  margin-top: 8px;
}
.step.done { border-left-color: var(--pass); }
.step.running { border-left-color: var(--brand); }
.step.failed { border-left-color: var(--hit); }
.step.human { border-left-color: var(--block, #9b59b6); }
.no {
  display: inline-block;
  width: 20px;
  text-align: center;
  color: var(--muted);
}
.cut {
  margin: 14px 0 2px;
  font-size: 12px;
  color: var(--muted);
  border-top: 1px dashed var(--line, #cfdcea);
  padding-top: 6px;
}
.stale {
  margin-top: 8px;
  padding: 7px 10px;
  font-size: 12px;
  border: 1px solid var(--hit, #e0b4b4);
  border-left: 3px solid var(--hit, #c0392b);
  border-radius: 8px;
  line-height: 1.6;
}
.back {
  margin-top: 8px;
  font-size: 12px;
  color: var(--muted);
}
</style>
