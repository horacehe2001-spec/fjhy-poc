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
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

const router = useRouter()

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
  // 库里的累计口径（后端从 llm_call_log 统计）。页面刷新后 lastLlm 为空，
  // 靠这个还能说明「模型到底调过没有」。
  llm_summary?: LlmSummary
  bills: BillRow[]
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
const lastStats = ref<Record<string, number> | null>(null)

// 本次调用大模型的统计。**和 lastStats 分开存**：混进同一组数字里，
// 会让人以为「通过/不通过」那几个数也是模型给的——实际结论全由规则引擎
// 确定性判定，模型只对 pending 出辅助说明，不改结论。
// 后端 /api/ai-review/batch 的 llm 段有两种形态：没调成（called=0 带 note）
// 和调过（带 ok/failed/token/时延）。两种都要显示：调用失败不影响审核结论，
// 但失败必须看得见。
type LlmStat = {
  called: number
  ok: number
  failed: number
  model?: string | null
  config_id?: string | null
  prompt_tokens?: number
  completion_tokens?: number
  latency_p50_ms?: number | null
  latency_max_ms?: number | null
  latency_note?: string
  note?: string
  errors?: string[]
}
const lastLlm = ref<LlmStat | null>(null)

// 本次有就显示本次，没有（刷新页面、换页回来）就退到库里的累计口径。
// **两种口径必须在页面上标出来是哪一种**：把累计数当本次数看，
// 会得出「这一轮调了 100 次模型」这种错结论。
const llmView = computed<{ scope: string; s: LlmStat | LlmSummary } | null>(() => {
  if (lastLlm.value && lastLlm.value.called) return { scope: '本次', s: lastLlm.value }
  const sum = status.value?.llm_summary
  if (sum && sum.called) return { scope: '累计', s: sum }
  return null
})

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
  lastStats.value = null
  lastLlm.value = null
  try {
    const res = await api.aiReviewBatch()
    if (!res.ok) {
      err.value = res.error || '审核失败'
      return
    }
    lastStats.value = res.stats as Record<string, number>
    lastLlm.value = (res.llm ?? null) as LlmStat | null
    await loadStatus()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function goWorkbench(conclusion?: string) {
  // 带 conclusion 参数跳转到工作台（工作台支持 query 过滤）
  router.push(conclusion ? `/review/bills?conclusion=${conclusion}` : '/review/bills')
}

onMounted(loadStatus)
</script>

<template>
  <h2>AI 智能审核</h2>

  <!-- 审核结果汇总。**数取自 /status（库里现存的结论），不是组件内存。**
       原先这一整块只在 lastStats 有值时渲染，而 lastStats 是组件内的 ref：
       刷新页面或切走再回来就为空，KPI 整块消失——看页面的人会以为审核结果丢了。
       实测连跑两轮 /batch，返回完全一致（29 单 · 不通过 9 / 待复核 14 / 通过 6，
       模型 25/25 全成功），后端一个数都没丢，丢的只是前端这份内存状态。 -->
  <!-- **这个面板一律渲染，不按「有没有审过」来判。**
       「智能审核」按钮就在这一排里，面板一旦不渲染，按钮跟着消失——
       而没审过的时候恰恰最需要这个按钮。所以数用 ?? 0 兜底，面板不藏。 -->
  <div class="panel" style="margin-top: 16px">
    <h3>审核结果汇总</h3>
    <div class="stat-row">
      <div class="stat-box">
        <div class="stat-n">{{ status?.total ?? 0 }}</div>
        <div class="stat-lbl muted">演示单</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ status?.reviewed ?? 0 }}</div>
        <div class="stat-lbl muted">已审</div>
      </div>
      <div class="stat-box t-pass-bg">
        <div class="stat-n">{{ status?.pass ?? 0 }}</div>
        <div class="stat-lbl">通过</div>
      </div>
      <div class="stat-box t-pend-bg">
        <div class="stat-n">{{ status?.review ?? 0 }}</div>
        <div class="stat-lbl">待复核</div>
      </div>
      <div class="stat-box t-hit-bg">
        <div class="stat-n">{{ status?.reject ?? 0 }}</div>
        <div class="stat-lbl">不通过</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ status?.llm_assisted ?? 0 }}</div>
        <div class="stat-lbl muted">模型辅助条数</div>
      </div>

      <!-- 触发按钮并进这一排的最右端：它原先自己占一个 panel 一整排，
           而它只是一个动作，不值一排。 -->
      <button :disabled="busy" @click="runReview" class="btn-primary stat-btn">
        {{ busy ? '审核中…' : '智能审核' }}
      </button>
    </div>

    <!-- 三条提示合成一行：原先「本次/库中现存」和「不合格单流入」各占一条，
         加上数字卡一共竖着堆三层，一屏里真正的信息密度很低。
         合并时**不删信息**，只把两句接成一行，用 · 分隔。 -->
    <div v-if="err" class="err">{{ err }}</div>
    <div class="note one-line">
      <template v-if="busy">正在生成演示数据并执行三层审核，请稍候——一轮约 5 分钟（含模型调用）</template>
      <template v-else-if="lastStats">
        本次运行：审核 {{ lastStats['total'] }} 张 ·
        不通过 {{ lastStats['reject'] }} · 待复核 {{ lastStats['review'] }} ·
        通过 {{ lastStats['pass'] }}<template
          v-if="lastStats['skipped']"> · 幂等跳过 {{ lastStats['skipped'] }}</template><template
          v-if="lastStats['failed']"> · 失败 {{ lastStats['failed'] }}</template>
      </template>
      <template v-else-if="status && status.reviewed">库中现存的审核结论，刷新后仍在；点「智能审核」重新生成演示数据并重审一轮</template>
      <template v-else>还没有审核结论。点右侧「智能审核」生成演示数据并跑一轮完整审核</template>
      <template v-if="!busy && status && status.reject + status.review > 0">
        · 不合格单已流入 <a href="#" @click.prevent="goWorkbench('reject')">审核岗工作台</a>，
        可按「不通过」「待复核」筛选
      </template>
    </div>
  </div>

  <!-- 大模型辅助：调了几次、成几次、花了多少 token、慢不慢。
       这一段原先只在接口返回里有，页面一个字都不显示，于是模型明明在调，
       看页面的人只看得见规则引擎的结论，会以为模型没接上。 -->
  <div v-if="llmView || (status && status.llm_summary)" class="panel">
    <h3>大模型辅助<span v-if="llmView" class="muted" style="font-size: 12px">
      （{{ llmView.scope }}口径）</span></h3>

    <div v-if="!llmView" class="note">
      还没有模型调用记录。<template v-if="status && status.llm_summary && !status.llm_summary.configured">
      「规则研判」用途还没有启用的配置，去「系统 → 大模型配置」里设一条默认。</template>
      <template v-else-if="status && status.llm_summary">
      当前「规则研判」模型 {{ status.llm_summary.model }}，点「智能审核」后会在待人工的结论上介入。</template>
    </div>

    <template v-else>
      <div class="stat-row">
        <div class="stat-box">
          <div class="stat-n">{{ llmView.s.called }}</div>
          <div class="stat-lbl muted">调用次数</div>
        </div>
        <div class="stat-box t-pass-bg">
          <div class="stat-n">{{ llmView.s.ok }}</div>
          <div class="stat-lbl">成功</div>
        </div>
        <div class="stat-box" :class="llmView.s.failed ? 't-hit-bg' : ''">
          <div class="stat-n">{{ llmView.s.failed }}</div>
          <div class="stat-lbl" :class="llmView.s.failed ? '' : 'muted'">失败</div>
        </div>
        <div class="stat-box">
          <div class="stat-n">{{ llmView.s.latency_p50_ms != null
            ? (llmView.s.latency_p50_ms / 1000).toFixed(1) + 's' : '—' }}</div>
          <div class="stat-lbl muted">时延 p50</div>
        </div>
        <div class="stat-box">
          <div class="stat-n">{{ llmView.s.latency_max_ms != null
            ? (llmView.s.latency_max_ms / 1000).toFixed(1) + 's' : '—' }}</div>
          <div class="stat-lbl muted">时延 max</div>
        </div>
        <div class="stat-box">
          <div class="stat-n">{{ (llmView.s.prompt_tokens ?? 0) + (llmView.s.completion_tokens ?? 0) }}</div>
          <div class="stat-lbl muted">token 合计</div>
        </div>
      </div>

      <div class="note" style="margin-top: 8px">
        模型 <span class="mono">{{ llmView.s.model || '—' }}</span>
        <template v-if="llmView.s.config_id">（配置 <span class="mono">{{ llmView.s.config_id }}</span>）</template>
        · 用途「规则研判」。模型只对规则引擎判「待人工」的结论出辅助说明，
        不改引擎的判定；审核报告里会标明哪条来自模型辅助。
        <template v-if="llmView.s.note">（{{ llmView.s.note }}）</template>
        <template v-else-if="llmView.s.latency_note">（{{ llmView.s.latency_note }}）</template>
      </div>

      <div v-if="lastLlm && lastLlm.errors && lastLlm.errors.length" class="llm-err">
        <div class="llm-err-t">本次失败原因（最多列 5 条）</div>
        <ul>
          <li v-for="(e, i) in lastLlm.errors" :key="i">{{ e }}</li>
        </ul>
      </div>
    </template>
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
.llm-err {
  margin-top: 10px;
  padding: 8px 12px;
  border: 1px solid var(--hit, #c0392b);
  border-radius: 6px;
}
.llm-err-t {
  font-size: 12px;
  color: var(--hit, #c0392b);
  margin-bottom: 4px;
}
.llm-err ul {
  margin: 0;
  padding-left: 18px;
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
</style>
