<script setup lang="ts">
/**
 * AI 智能审核页。三层架构：本体快照 → 规则引擎 → LLM 辅助。
 *
 * 点击「智能审核」按钮：
 *   1. 生成演示数据（幂等，每次刷新）
 *   2. S 阶段：用本体概念定义捞每张单的事实快照
 *   3. D 阶段：规则引擎确定性判断，产出 hit / pass / pending
 *   4. D-ext：对 pending findings，LLM 给辅助判断（需要配置大模型）
 *   5. A 阶段：结论写入 review_run + review_finding
 *
 * 不合格单（结论 reject / review）自动出现在审核岗工作台的过滤视图里，
 * 不需要额外推送——工作台按 conclusion 筛选，审核完即可见。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'

type HitRow = {
  run_id: string
  rule_id: string
  rule_name: string | null
  control_type: string
  message: string
  needs_human: boolean
}

type BillRow = {
  bill_no: string
  amount: number | null
  summary: string | null
  run_id: string | null
  conclusion: 'pass' | 'review' | 'reject' | null
  hit_count: number | null
  pending_count: number | null
  rule_executed: number | null
  finished_at: string | null
  hits: HitRow[]
}

type StatusData = {
  total: number
  reviewed: number
  reject: number
  review: number
  pass: number
  llm_assisted: number
  // 库里的累计口径（后端从 llm_call_log 统计）。页面已不再展示它
  // （「大模型辅助」面板 2026-10-24 按要求去掉），但字段留着：
  // 后端照旧返回，删掉类型只会让下次要用的人以为接口没给。
  llm_summary?: LlmSummary
  /** 三个比率。**分母一律是「已审」**，口径由后端 review_rates 定，前端不另算。 */
  rates?: ReviewRates
  /** 时效。口径由后端 review_timing 定，含「这段时间里有没有模型」。 */
  timing?: ReviewTiming
  bills: BillRow[]
}

type ReviewRates = {
  denominator: number
  /** 还没有审核结论时三个都是 null，**不是 0**——算不出来和「0%」是两回事 */
  pass_rate: number | null
  review_rate: number | null
  reject_rate: number | null
  note: string
}

type ReviewTiming = {
  bills: number
  /** finished_at 早于 started_at 的异常行，**排除了多少条要报出来** */
  excluded_nonpositive: number
  p50_ms: number | null
  p90_ms: number | null
  min_ms: number | null
  max_ms: number | null
  llm_involved_findings: number
  /** 这批结论里模型有没有介入。量级差两个数量级，卡片上必须标出来 */
  includes_llm: boolean
  scope: string
}

type LlmSummary = {
  configured: boolean
  model: string | null
  config_id: string | null
  called: number
  ok: number
  failed: number
  prompt_tokens: number
  completion_tokens: number
  latency_p50_ms: number | null
  latency_max_ms: number | null
  last_called_at: string | null
  note?: string
  latency_note?: string
}

const status = ref<StatusData | null>(null)
const busy = ref(false)
const err = ref('')

// ── 跑一轮的进度与中断 ───────────────────────────────────────────────────────
// 之前这里什么都没有：按钮变成「审核中…」，然后就没有然后了。
// 实测一次点击挂了 25 分钟还没返回（模型补判串行 + 失败要等满 timeout），
// 而裸 fetch 不带超时，所以页面会一直停在那三个字上——**看起来和卡死一模一样**。
//
// 现在三样东西一起给：**已经等了多久**、**跑完还剩多少**、**能不能不等了**。
/** 本轮已等秒数，每秒自己走，让人看得出它在动而不是挂了 */
const elapsed = ref(0)
let tick: ReturnType<typeof setInterval> | null = null
/** 本轮的中断器。取消只是「不等了」，不回滚已经落库的补判 */
let aborter: AbortController | null = null
/** 上一轮的补判进度文案（续跑提示就靠它） */
const lastProgress = ref('')
/** 上一轮结束后还剩多少条没补判。> 0 时按钮上要显示「接着跑」 */
const remaining = ref(0)
/** 本轮是被用户取消的。和「失败」分开——取消不是错误 */
const canceled = ref(false)

function stopTick() {
  if (tick) { clearInterval(tick); tick = null }
}

function cancelRun() {
  // **只中断等待，不中断服务端。** 这是同步接口，客户端断开之后
  // 服务端那一条仍会跑完并落库——说成「已停止审核」就是假话。
  canceled.value = true
  aborter?.abort()
}

function hhmmss(s: number) {
  const m = Math.floor(s / 60)
  return m ? `${m} 分 ${s % 60} 秒` : `${s} 秒`
}

// 「大模型辅助」面板 2026-10-24 按要求整块去掉了，连带删掉的还有
// lastLlm（本次调用统计）和 llmView（本次/累计两种口径的取舍）。
//
// **页面上仍然看得出模型有没有介入**：上面汇总那一排留着「模型辅助条数」
// （status.llm_assisted），每份审核报告里也仍然标明哪一条来自模型辅助。
// 去掉的是调用次数/时延/token 这组运维口径的数字，不是模型的痕迹。
//
// 接口侧没动：/api/ai-review/batch 照旧返回 llm 段、/status 照旧返回
// llm_summary，要查调用情况直接看接口或 llm_call_log。

/**
 * 比率显示。**算不出来显示「—」，不显示 0%。**
 * 没审过的时候写 0%，等于说「一张都没通过」——那是个结论，而实际情况是没结论。
 */
function rate(v: number | null | undefined) {
  return v == null ? '—' : (v * 100).toFixed(1) + '%'
}

/** 耗时显示。毫秒级和秒级都要能读——模型一介入会从几十毫秒跳到十几秒。 */
function ms(v: number | null | undefined) {
  if (v == null) return '—'
  return v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms'
}

const CONCL: Record<string, { label: string; cls: string }> = {
  pass:   { label: '通过',   cls: 't-pass' },
  review: { label: '待复核', cls: 't-pend' },
  reject: { label: '不通过', cls: 't-hit' },
}

async function loadStatus() {
  try {
    status.value = await api.aiReviewStatus() as StatusData
  } catch {
    // 初始加载失败不弹错误；可能是还没生成过演示数据
  }
}

async function runReview() {
  if (busy.value) return
  busy.value = true
  err.value = ''
  canceled.value = false
  lastProgress.value = ''
  elapsed.value = 0
  stopTick()
  tick = setInterval(() => { elapsed.value += 1 }, 1000)
  aborter = new AbortController()
  try {
    const res = await api.aiReviewBatch(aborter.signal)
    if (!res.ok) {
      err.value = res.error || '审核失败'
      return
    }
    // 补判可能没跑完（单轮有时间/条数预算）。**跑完和没跑完必须说清楚**，
    // 否则使用者不知道还该不该再点一次。
    remaining.value = res.llm?.remaining ?? 0
    lastProgress.value = res.llm?.progress_note || ''
    await loadStatus()
  } catch (e) {
    if (canceled.value) {
      // 取消不是错误，别往 err 里塞。但要说清楚服务端没停——
      // 否则使用者以为自己把它掐了，实际那一条还在跑、还在计费。
      lastProgress.value = '已停止等待。**服务端当前这一条仍会跑完并落库**，'
        + '刷新页面就能看到它的结果；补判可续跑，再点一次接着往下跑。'
      await loadStatus()
    } else {
      err.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    stopTick()
    aborter = null
    busy.value = false
  }
}

onUnmounted(stopTick)

onMounted(loadStatus)
</script>

<template>
  <h2>AI 智能审核</h2>

  <!-- 审核结果汇总。**数一律取自 /status（库里现存的结论），不取组件内存。**
       曾经栽过：这一整块只在「本次运行」的内存状态有值时才渲染，于是刷新页面
       或切走再回来 KPI 整块消失——看页面的人会以为审核结果丢了。
       实测连跑两轮 /batch 返回完全一致，后端一个数都没丢，丢的只是那份内存状态。 -->
  <!-- **这个面板一律渲染，不按「有没有审过」来判。**
       「智能审核」按钮就在这一排里，面板一旦不渲染，按钮跟着消失——
       而没审过的时候恰恰最需要这个按钮。所以数用 ?? 0 兜底，面板不藏。 -->
  <div class="panel" style="margin-top: 16px">
    <h3>审核结果汇总</h3>
    <!-- KPI 改成**比率 + 时效**（客户关注的两项），原始条数降成副标——
         条数一个都没丢，只是不再占主位：「通过 6」要和「已审 29」一起看
         才有意义，而占主位的数会被单独记住。

         三个比率的分母一律是**已审**，口径由后端 review_rates 算，前端不另算一份。
         「待人工」单列一张卡，**不并进通过也不并进不通过**——它是判不了。 -->
    <div class="stat-row">
      <div class="stat-box">
        <div class="stat-n">{{ status?.reviewed ?? 0 }}</div>
        <div class="stat-lbl muted">已审</div>
        <div class="stat-sub">共 {{ status?.total ?? 0 }} 张</div>
      </div>
      <div class="stat-box t-pass-bg">
        <div class="stat-n">{{ rate(status?.rates?.pass_rate) }}</div>
        <div class="stat-lbl">通过率</div>
        <div class="stat-sub">通过 {{ status?.pass ?? 0 }}</div>
      </div>
      <div class="stat-box t-pend-bg">
        <div class="stat-n">{{ rate(status?.rates?.review_rate) }}</div>
        <div class="stat-lbl">待人工率</div>
        <div class="stat-sub">待复核 {{ status?.review ?? 0 }}</div>
      </div>
      <div class="stat-box t-hit-bg">
        <div class="stat-n">{{ rate(status?.rates?.reject_rate) }}</div>
        <div class="stat-lbl">不通过率</div>
        <div class="stat-sub">不通过 {{ status?.reject ?? 0 }}</div>
      </div>
      <!-- 时效。**副标必须写「不含模型」**：实测不含模型整条链路 p50 75ms，
           而模型研判单次 p50 约 12 秒——模型一介入这个数翻一百多倍，
           不标的话看过旧数的人会以为系统变慢了。 -->
      <div class="stat-box">
        <div class="stat-n">{{ ms(status?.timing?.p50_ms) }}</div>
        <div class="stat-lbl">单均耗时</div>
        <div class="stat-sub">
          p90 {{ ms(status?.timing?.p90_ms) }}<template
            v-if="status?.timing && !status.timing.includes_llm"> · 不含模型</template>
        </div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ms(status?.timing?.max_ms) }}</div>
        <div class="stat-lbl muted">最慢一张</div>
        <div class="stat-sub">最快 {{ ms(status?.timing?.min_ms) }}</div>
      </div>

      <!-- 触发按钮并进这一排的最右端：它原先自己占一个 panel 一整排，
           而它只是一个动作，不值一排。 -->
      <button :disabled="busy" @click="runReview" class="btn-primary stat-btn">
        <!-- 按钮上带计时：**「审核中…」不动的那三个字和卡死长得一样。**
             上一轮没跑完时改口叫「接着跑」——还剩多少就写多少，
             不写的话使用者不知道该再点一次。 -->
        <template v-if="busy">审核中 {{ elapsed }}s</template>
        <template v-else-if="remaining">接着跑（还剩 {{ remaining }} 条）</template>
        <template v-else>智能审核</template>
      </button>
    </div>

    <!-- 跑的过程中：等了多久 + 可以不等了。
         取消**只停等待，不停服务端**，文案上必须说清楚，不能让人以为掐掉了。 -->
    <div v-if="busy" class="run-bar">
      <span class="spin" />
      <span>已等 <b>{{ hhmmss(elapsed) }}</b></span>
      <span class="muted" style="font-size: 12px">
        规则引擎毫秒级；慢的是「待人工」那部分的模型补判，单轮有时间上限
      </span>
      <button class="mini" @click="cancelRun">不等了</button>
    </div>

    <!-- 说明解释的整段文字 2026-10-24 按要求去掉。
         **错误提示留着**：那不是解释，是「这一轮没跑成」，藏了就只剩一排旧数字。 -->
    <div v-if="err" class="err">{{ err }}</div>
    <!-- 续跑提示。**跑完和没跑完必须看得出区别。** -->
    <div v-if="!busy && lastProgress" class="note">
      <MarkText :text="lastProgress" />
    </div>
  </div>

  <!-- 演示单列表 -->
  <div v-if="status && status.bills.length" class="panel">
    <h3>演示单审核状态</h3>
    <table>
      <thead>
        <tr>
          <th style="width: 220px">单据编号</th>
          <th>摘要</th>
          <th class="num" style="width: 90px">金额</th>
          <th style="width: 72px">结论</th>
          <th class="num" style="width: 52px">执行</th>
          <th class="num" style="width: 52px">命中</th>
          <th class="num" style="width: 62px">待人工</th>
          <th style="width: 60px"></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="b in status.bills" :key="b.bill_no">
            <td>
              <a :href="`#/review/bills/${encodeURIComponent(b.bill_no)}`"
                 style="font-size: 12px; font-family: monospace">
                {{ b.bill_no }}
              </a>
            </td>
            <td style="font-size: 12px">{{ b.summary }}</td>
            <td class="num">{{ b.amount != null ? b.amount.toLocaleString() : '—' }}</td>
            <td>
              <template v-if="b.conclusion && CONCL[b.conclusion]">
                <span class="tag" :class="CONCL[b.conclusion].cls">
                  {{ CONCL[b.conclusion].label }}
                </span>
              </template>
              <span v-else class="tag t-na">未审核</span>
            </td>
            <td class="num">{{ b.rule_executed ?? '—' }}</td>
            <td class="num">
              <span v-if="(b.hit_count ?? 0) > 0" class="tag t-hit">{{ b.hit_count }}</span>
              <span v-else>{{ b.hit_count ?? '—' }}</span>
            </td>
            <td class="num">
              <span v-if="(b.pending_count ?? 0) > 0" class="tag t-pend">{{ b.pending_count }}</span>
              <span v-else>{{ b.pending_count ?? '—' }}</span>
            </td>
            <td>
              <a v-if="b.run_id"
                 :href="`#/review/report/${encodeURIComponent(b.run_id)}`"
                 style="font-size: 12px">报告</a>
            </td>
          </tr>
      </tbody>
    </table>
  </div>

  <div v-if="!status || !status.bills.length" class="note" style="margin-top: 16px">
    点击「智能审核」按钮，系统会生成演示数据并执行完整的三层 AI 审核。
  </div>
</template>

<style scoped>
.arch-row {
  display: flex;
  align-items: stretch;
  gap: 0;
  margin: 12px 0 0;
}
.arch-card {
  flex: 1;
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 12px 14px;
  border: 1px solid var(--line, #e3e8ef);
  border-radius: 8px;
}
.arch-arrow {
  display: flex;
  align-items: center;
  padding: 0 8px;
  color: var(--muted);
  font-size: 18px;
}
.arch-no {
  font-size: 18px;
  color: var(--brand, #1668dc);
  min-width: 24px;
}
.stat-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
/* 卡片压紧：6 张并排时原尺寸（28px 数字 + 18px 内边距）在 1100px 宽度下
   会把提示行挤到第二屏。改小后 6 张仍是一排，数字依然是全页最大的字。 */
.stat-box {
  flex: 1 1 0;
  min-width: 78px;
  max-width: 140px;
  padding: 7px 10px;
  border: 1px solid var(--line, #e3e8ef);
  border-radius: 8px;
  text-align: center;
}
.stat-n {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.15;
}
.stat-lbl {
  font-size: 11px;
  margin-top: 1px;
  white-space: nowrap;
}
/* 副标：原始条数和口径标注。**比主标小、比主标淡**——
   它是「这个比率是拿什么算的」，不该和比率本身抢注意力。 */
.stat-sub {
  font-size: 10px;
  color: var(--muted);
  margin-top: 2px;
  line-height: 1.4;
  /* **不设 nowrap**：卡片最宽 140px，而「p90 90ms · 不含模型」这类副标
     放不下时该换行，不该溢出到隔壁卡片上去。 */
}
/* 提示行：一行放不下时正常换行，不裁切文字 */
.one-line {
  margin-top: 8px;
  line-height: 1.7;
}
.t-pass-bg { border-color: var(--pass, #27ae60); }
.t-pass-bg .stat-n { color: var(--pass, #27ae60); }
.t-pend-bg { border-color: var(--brand, #1668dc); }
.t-pend-bg .stat-n { color: var(--brand, #1668dc); }
.t-hit-bg  { border-color: var(--hit, #c0392b); }
.t-hit-bg  .stat-n  { color: var(--hit, #c0392b); }
.mono {
  font-family: monospace;
  font-size: 12px;
}
/* 按钮并进数字卡那一排：不参与 flex 均分（卡片是 flex:1），
   靠 margin-left:auto 顶到最右端，高度跟着卡片拉满。 */
.stat-btn {
  flex: 0 0 auto;
  margin-left: auto;
  align-self: stretch;
  padding: 0 22px;
  font-size: 15px;
}
.btn-primary {
  padding: 6px 20px;
  background: var(--brand, #1668dc);
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 跑的过程中那一条。**转圈是为了证明它在动**——
   一个不动的「审核中…」和卡死在页面上没有区别。 */
.run-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding: 8px 12px;
  border: 1px solid var(--line, #e3e8ef);
  border-left: 3px solid var(--brand, #1668dc);
  border-radius: 6px;
  font-size: 13px;
  flex-wrap: wrap;
}
.spin {
  width: 12px;
  height: 12px;
  border: 2px solid var(--line, #e3e8ef);
  border-top-color: var(--brand, #1668dc);
  border-radius: 50%;
  display: inline-block;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
/* .mini 在 AgentHub.vue 里是 scoped 的，别的页面拿不到，所以这里自己写一份。
   （BillDetail.vue 的「判类型」按钮也用了 .mini，同样没生效——另记） */
button.mini {
  padding: 2px 9px;
  font-size: 12px;
  cursor: pointer;
}
/* 跟随系统「减少动态效果」设置：转圈换成静止的高亮边，不闪 */
@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
}


</style>
