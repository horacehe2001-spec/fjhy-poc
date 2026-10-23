<script setup lang="ts">
/**
 * 智能体协同的运行时视图（规则抽取、统一口径两条链共用）。
 *
 * 抽成组件的理由和后端 app/agent_job.py 一样：**两条链的进度语义必须一致**。
 * 留两份拷贝的下场是可预见的——改了一处忘了另一处，
 * 然后两个页面上「跑到一半失败」长得不一样，而没人说得清哪个才是对的。
 *
 * 画的是 SDAFI 自己的隐喻：Agent 挂在一条数据池总线上，
 * **它们之间没有任何直连**——上游写进池子、发信号，下游从池子里读。
 *
 * 三条硬规则（与后端一致，两边都不许破）：
 *
 * 1. **进度条单调不回退。** 回退会让人以为任务重来了。
 * 2. **页面上的数字一律来自接口，前端不做任何估算。**
 *    确定性 Agent 显示「规则引擎 · 不消耗」而不是 0；
 *    模型费用显示「单价待核对」而不是 ¥0.00 —— 0 是个结论，「不知道」不是 0。
 * 3. **某个 Agent 失败就红在原地，后面的标「未执行」，不跳过不粉饰。**
 *
 * 进度权重按**实测耗时**折算（后端算好传过来），不按「已完成个数/总数」：
 * 抽取链实测严重偏斜——切条款 16 秒、AI 草稿 396 秒占 96%。
 * 按个数算，进度条会在某一格卡住六分钟，比没有进度条更像死机。
 */
import { computed } from 'vue'
import type { AgentSnap, ExMeta, ExProgress } from '../api'

const props = defineProps<{
  prog: ExProgress | null
  meta: ExMeta | null
  /** 按钮文案：未运行时 / 运行中 */
  startLabel: string
  runningLabel: string
  disabled?: boolean
  /**
   * 是否画「S→D→A→F→I」那列卡片。默认画。
   * 规则抽取那一页关掉了：那一页要留给「规则明细」，
   * 而协同全景已经把同一批 Agent 的名字和状态画了一遍。
   * **关掉会一起带走每个 Agent 的产出数字、费用和备注**（它们只在这列卡片上），
   * 报错不受影响——失败的那条后端会抬到 job.error，顶上那条红线照显。
   */
  phaseCards?: boolean
}>()
defineEmits<{ start: [] }>()

const PHASES = [['S', '读'], ['D', '判'], ['A', '写'], ['F', '量'], ['I', '调']] as const
const STATUS_CN: Record<string, string> = {
  waiting: '待运行', running: '运行中', done: '已完成', failed: '失败', skipped: '未执行',
}

const agents = computed<AgentSnap[]>(() => props.prog?.agents || props.meta?.agents || [])
const running = computed(() => agents.value.find((a) => a.status === 'running'))
const jobStatus = computed(() => props.prog?.status || 'idle')
/** 阶段内进度：当前 Agent 五个阶段走完几个。刚进 Sense 时没有分母，走不确定态。 */
const phaseFrac = computed(() => {
  const a = running.value
  return a ? Object.keys(a.timings).length / PHASES.length : 0
})

function nodeX(i: number, n: number) {
  return ((i + 0.5) / n) * 100
}
function fmt(s?: number | null) {
  if (s === null || s === undefined) return '—'
  return s >= 60 ? Math.floor(s / 60) + '分' + Math.round(s % 60) + '秒' : s + '秒'
}
</script>

<template>
  <div class="panel">
    <div class="row" style="margin-bottom: 10px">
      <button :disabled="jobStatus === 'running' || disabled" @click="$emit('start')">
        {{ jobStatus === 'running' ? runningLabel : startLabel }}
      </button>
      <span class="muted" style="font-size: 12px"><slot name="hint" /></span>
    </div>

    <div class="bar"><i :class="jobStatus" :style="{ width: (prog?.pct || 0) + '%' }" /></div>
    <div v-if="jobStatus === 'running'" class="bar2">
      <i :class="{ indet: phaseFrac === 0 }"
         :style="phaseFrac ? { width: Math.round(phaseFrac * 100) + '%' } : undefined" />
    </div>
    <div class="row" style="margin: 6px 0 0; justify-content: space-between">
      <span>
        <b style="font-size: 15px">{{ prog?.pct || 0 }}%</b>
        <span style="margin-left: 10px">
          {{ jobStatus === 'running' ? (running ? running.agent_name + ' 运行中' : '调度中')
            : jobStatus === 'done' ? '协同完成'
            : jobStatus === 'failed' ? '协同失败' : '未开始' }}
        </span>
        <span class="muted" style="margin-left: 10px">
          {{ prog?.totals.agents_done || 0 }}/{{ prog?.totals.agents || agents.length }} 个智能体
          · 实际用时 {{ fmt(prog?.elapsed) }}
        </span>
      </span>
      <span v-if="jobStatus === 'running' && running" class="muted">
        本阶段 {{ running.phase }} {{ fmt(running.phase_elapsed) }}
      </span>
    </div>
    <div v-if="prog && prog.status === 'running' && !prog.measured_baseline"
         class="muted" style="font-size: 12px; margin-top: 4px">
      首次运行没有耗时基线，总进度暂按等权估算；跑完一次后用实测耗时重算权重。
    </div>
    <div v-if="prog?.error" class="err" style="margin-top: 8px">{{ prog.error }}</div>
  </div>

  <div class="panel">
    <h3>协同全景</h3>
    <div class="pano">
      <div v-for="(a, i) in agents" :key="a.id" class="node" :class="a.status"
           :style="{ left: nodeX(i, agents.length) + '%' }">
        <span class="tag" :class="a.layer === 'L0' ? 't-na' : 't-pass'">{{ a.layer }}</span>
        <div class="nm">{{ a.agent_name }}</div>
        <div class="st">{{ STATUS_CN[a.status] }}</div>
        <div class="stem" />
        <div class="keys"><code v-for="k in a.pool_keys" :key="k">{{ k }}</code></div>
      </div>
      <div class="bus" />
      <div v-for="(s, i) in (prog?.signals || [])" :key="i" class="sig"
           :style="{ left: nodeX(agents.findIndex((x) => x.id === s.from), agents.length) + '%' }">
        {{ s.channel.replace('fjhy_', '') }}
      </div>
    </div>
    <div class="muted" style="font-size: 12px">
      智能体之间<b>零直接调用</b>：上游写进数据池、发信号，下游从池子里读。
      {{ meta?.bus ? '总线：' + meta.bus : '' }}
    </div>
  </div>

  <div v-if="phaseCards !== false" class="panel">
    <h3>S → D → A → F → I</h3>
    <div v-for="a in agents" :key="a.id" class="acard" :class="a.status">
      <div class="row" style="margin: 0 0 6px">
        <span class="tag" :class="a.layer === 'L0' ? 't-na' : 't-pass'">{{ a.layer }}</span>
        <b>{{ a.agent_name }}</b>
        <span class="tag" :class="a.status === 'done' ? 't-pass' : a.status === 'running'
          ? 't-pend' : a.status === 'failed' ? 't-hit' : 't-na'">{{ STATUS_CN[a.status] }}</span>
        <span class="muted" style="font-size: 11px">{{ a.desc }}</span>
        <span style="margin-left: auto" class="muted">{{ fmt(a.elapsed) }}</span>
      </div>
      <div class="phases">
        <div v-for="[code, cn] in PHASES" :key="code" class="ph"
             :class="{ done: a.timings[code] !== undefined, cur: a.phase === code }">
          <b>{{ code }}</b><span>{{ cn }}</span>
          <i>{{ a.timings[code] !== undefined ? a.timings[code] + 's' : '' }}</i>
        </div>
      </div>
      <div v-if="Object.keys(a.outputs).length" class="row" style="margin: 6px 0 0; gap: 14px">
        <span v-for="(v, k) in a.outputs" :key="k">
          <span class="muted">{{ k }}</span> <b>{{ v }}</b>
        </span>
        <span v-if="a.cost" class="muted" style="margin-left: auto">{{ a.cost }}</span>
      </div>
      <div v-if="a.note" class="muted" style="font-size: 12px; margin-top: 4px"><MarkText :text="a.note" /></div>
      <div v-if="a.error" class="err" style="margin-top: 4px">{{ a.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.bar { height: 10px; background: #eef1f5; border-radius: 5px; overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--brand); transition: width .4s; }
.bar > i.done { background: var(--pass); }
.bar > i.failed { background: var(--hit); }
.bar2 { height: 4px; background: #f3f5f8; border-radius: 2px; margin-top: 3px; overflow: hidden; }
.bar2 > i { display: block; height: 100%; background: #9bb7d4; transition: width .4s; }
.bar2 > i.indet { width: 32%; animation: slide 1.1s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -32%; } 100% { margin-left: 100%; } }

.pano { position: relative; height: 164px; margin: 4px 0 6px; }
.pano .bus {
  position: absolute; left: 0; right: 0; top: 100px; height: 3px;
  background: linear-gradient(90deg, #cfdcea, #9bb7d4, #cfdcea);
}
.pano .node {
  position: absolute; top: 0; transform: translateX(-50%); width: 184px; height: 164px;
  text-align: center;
}
.pano .node .nm { font-weight: 600; font-size: 13px; margin-top: 3px; line-height: 1.25; }
.pano .node .st { font-size: 11px; color: var(--muted); }
/* stem 与 keys 钉死在节点内的固定位置：节点名可能折成两行
   （「素材感知（源头、规则资产装载）」），顺排会把它们压下去而总线不动，
   三条链共用这个组件，一条链改名不该让另外两条的图错位。 */
.pano .node .stem {
  position: absolute; top: 74px; left: 50%; width: 2px; height: 22px;
  margin-left: -1px; background: #cfdcea;
}
.pano .node .keys { position: absolute; top: 108px; left: 0; right: 0; }
.pano .node .keys code { display: block; font-size: 10px; color: var(--muted); line-height: 1.5; }
.pano .node.running .nm { color: var(--brand); }
.pano .node.running .stem { background: var(--brand); animation: pulse 1s ease-in-out infinite; }
.pano .node.done .stem { background: var(--pass); }
.pano .node.failed .nm { color: var(--hit); }
.pano .node.failed .stem { background: var(--hit); }
.pano .node.skipped { opacity: .45; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
.pano .sig {
  position: absolute; top: 108px; transform: translateX(-50%);
  font-size: 10px; color: var(--brand); background: #fff; padding: 0 4px;
}

.acard { border: 1px solid var(--line); border-radius: 4px; padding: 9px 11px; margin-top: 9px; }
.acard.running { border-color: var(--brand); }
.acard.failed { border-color: var(--hit); }
.acard.skipped { opacity: .55; }
.phases { display: flex; gap: 6px; }
.phases .ph {
  flex: 1; border: 1px solid var(--line); border-radius: 3px; padding: 4px 6px;
  font-size: 11px; color: var(--muted); background: #fafbfc;
}
.phases .ph b { color: var(--ink); margin-right: 4px; }
.phases .ph i { float: right; font-style: normal; }
.phases .ph.done { background: #f1f8f2; border-color: #cfe6d3; }
.phases .ph.cur { background: #eef4fb; border-color: var(--brand); }
</style>
