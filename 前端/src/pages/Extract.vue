<script setup lang="ts">
/**
 * 栏目二 · 规则抽取 —— 智能体协同的运行时视图。
 *
 * 这一页展示的是**三个智能体在协同的过程**，不是一排按钮的工作流。
 * 画的是 SDAFI 自己的隐喻：三个 Agent 挂在一条数据池总线上，
 * **它们之间没有任何直连**——上游写进池子、发信号，下游从池子里读。
 *
 * **第四个 Agent（L2 抽取协同与冲突仲裁）不在这一栏。**
 * 多源归并、口径冲突裁决是「统一口径」，那是「规则本体」那一栏的事；
 * 抽取只负责抽出来。它的 YAML 还在，订阅 fjhy_draft_ready，
 * 正好接在这条链的产出之后，由本体那一栏驱动。
 *
 * 协同视图（进度条、协同全景）在
 * components/AgentRuntime.vue 里，和「统一口径」那条链共用一份——
 * 进度/失败/信号的四条硬规则写在那个组件的头注释里，
 * 后端对应的是 app/agent_job.py。**三处口径必须一致，所以只留一份实现。**
 *
 * 这一页自己的事：真计费那一步的提示（含 AI 草稿）、三个页签、单步调试。
 * 实测这条链耗时严重偏斜——切条款 16 秒，AI 草稿 396 秒占 96%，
 * 所以进度按实测耗时加权，不按「已完成个数/总数」。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AgentRuntime from '../components/AgentRuntime.vue'
import Async from '../components/Async.vue'
import { api } from '../api'
import type {
  ExClause, ExDetail, ExDetailStats, ExDraft, ExIncremental, ExMeta, ExProgress,
} from '../api'

// 「规则明细」排在第一个，而且是默认页签：**它才是这一栏的产物。**
// 以前默认落在「条款清单」上，于是这一栏看起来像个文本切片工具——
// 而条款是中间物，规则明细才是交付物。
// **页面上只有「规则明细」一块产物**，所以没有页签了。
// 收进「单步调试」的两样都是**底账，不是产物**：
//   条款底账   回答「这条规则出自哪一句」和「制度里还有多少条没变成规则」
//   AI 草稿台账 唯一的出网台账：哪条原文离开过本系统、发了多少字、哪个型号、
//              第几次抽、数值核对过没有。规则明细是整理后的东西，看不到这些。
// 「处置口径」没有收：它只有三行，但说的是「不允许自动发布 / 必须有确认人」,
// 这种口径藏起来等于页面上不再承诺它，所以挪到明细下面常显。

const prog = ref<ExProgress | null>(null)
const meta = ref<ExMeta | null>(null)
const clauses = ref<ExClause[]>([])
const drafts = ref<ExDraft[]>([])

// ── 规则明细 ────────────────────────────────────────────────────────────────
const details = ref<ExDetail[]>([])
const dstats = ref<ExDetailStats | null>(null)
const dtags = ref<{ tag: string; n: number }[]>([])
const fOrigin = ref('')
const fState = ref('')
const fMapped = ref('')
const fTag = ref('')
const fQ = ref('')
const who = ref('')
const openMap = ref('')
const tagDraft = ref<{ id: string; tag: string; why: string } | null>(null)

const busy = ref('')
const err = ref('')
const msg = ref('')
const debugOpen = ref(false)
const clausesOpen = ref(false)   // 条款底账，单步调试里的二级折叠
const draftsOpen = ref(false)    // AI 草稿台账，同上

/** 展开时才去取条款。**模板里不写语句**——Vue 的内联表达式不支持 if。 */
function toggleClauses() {
  clausesOpen.value = !clausesOpen.value
  if (clausesOpen.value && !clauses.value.length) loadClauses()
}
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
    prog.value = await api.exProgress()
    if (prog.value.status !== 'running') {
      stopPoll()
      // 跑完刷**产物**。条款是底账，只在展开着的时候才顺带刷一下——
      // 它一次取 1000 行，没人在看的时候刷它是白花一个请求。
      await loadDetails()
      if (clausesOpen.value) await loadClauses()
      // 增量范围跑完一定变了（抽过的文件指纹被钉下来，就不在增量里了）。
      // 不刷的话按钮上还写着「增量抽取（2 份）」，而那 2 份已经抽完了——
      // 再点一次会得到「0 份」，看着像上一次没生效。
      await loadInc().catch(() => {})
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

// ── 增量抽取 ────────────────────────────────────────────────────────────────
// 「增量」= **从没抽过的 + 抽过之后又被改过的**（按文件指纹比对），
// 不是「只取最近一次扫描的变化台账」——后者在「重扫一次没变、但有文件一直
// 没抽过」时会变成 0 份，而那种漏是静默的。2026-10-21 用户口径。
const inc = ref<ExIncremental | null>(null)
// 两段确认：最后一步是 AI 草稿，**真出网、真计费**。
// 第一下只是把要花的钱摆出来，第二下才真跑。
// 这里不做成 confirm() 弹窗：那个会被浏览器拦，而且拦了之后什么都不会发生。
const armed = ref(false)
const fullRerun = ref(false)

const loadInc = () =>
  run('inc', async () => { inc.value = await api.exIncremental() })

/** 按钮文案。**份数和「会不会花钱」必须写在按钮上**，不能只写在说明里。 */
const startLabel = computed(() => {
  if (fullRerun.value) {
    return armed.value
      ? '确认全量重跑（忽略增量判断，含 AI 草稿 · 出网计费）'
      : '全量重跑（改过切分策略之后用）'
  }
  const n = inc.value?.total
  if (n === 0) return '无增量可抽（没有新文件，也没有哪份被改过）'
  if (armed.value) {
    const k = inc.value?.will_call_model ?? 0
    return k > 0
      ? `确认：抽 ${n} 份，并对最多 ${k} 条候选调模型（出网计费）`
      : `确认：抽 ${n} 份（本批没有要调模型的候选）`
  }
  return n == null ? '增量抽取' : `增量抽取（${n} 份 · 含 AI 草稿，出网计费）`
})

const startJob = () =>
  run('run', async () => {
    // 增量为 0 且不是全量重跑 → 不发请求。发了也只是四步空跑，
    // 但那会在台账里留下一次没有意义的作业记录。
    if (!fullRerun.value && inc.value?.total === 0) {
      msg.value = '增量为 0 份：没有新文件，也没有哪份文件在上次抽取之后被改过。'
        + '要对库里积压的候选跑草稿，用下面「单步调试」里的「只跑 AI 草稿」；'
        + '改过切分策略要全量重切，勾上「全量重跑」。'
      return
    }
    if (!armed.value) {
      armed.value = true      // 第一下只是上膛，把要花的钱摆出来
      return
    }
    armed.value = false
    const r = await api.exRun({ only_changed: !fullRerun.value })
    if (!r.ok) {
      msg.value = r.reason || '已有作业在运行'
      return
    }
    msg.value = (fullRerun.value ? '已启动**全量**协同作业 ' : '已启动增量协同作业 ')
      + r.job_id
    await poll()
    startPoll()
  })

const loadClauses = () =>
  run('clauses', async () => {
    clauses.value = (await api.exClauses({ limit: 1000 })).items
  })
const loadDrafts = () =>
  run('drafts', async () => {
    drafts.value = (await api.exDrafts({ limit: 500 })).items
  })

const loadDetails = () =>
  run('details', async () => {
    const [d, s, t] = await Promise.all([
      api.exDetails({
        origin: fOrigin.value || undefined, state: fState.value || undefined,
        mapped: fMapped.value === '' ? undefined : fMapped.value === 'y',
        tag: fTag.value || undefined, q: fQ.value || undefined, limit: 1000,
      }),
      api.exDetailStats(),
      api.exDetailTags(),
    ])
    details.value = d.items
    dstats.value = s
    dtags.value = t.items
  })

const runDetail = () =>
  run('detailrun', async () => {
    const r = await api.exDetailRun()
    msg.value = '规则明细：确定性映射 ' + r.table.mapped + ' 条（其中场景拆条 '
      + r.scenario.mapped + ' 条）、列对不齐 ' + r.table.flagged
      + ' 条，另投影模型草稿 ' + r.draft.projected + ' 条'
    await loadDetails()
  })

/** 采纳/驳回。**署名不许空**——没有署名的审定在审计上等于没发生。 */
const reviewDetail = (d: ExDetail, state: string) =>
  run('rev', async () => {
    let reason: string | undefined
    if (state === '驳回') {
      reason = window.prompt('驳回原因（必填：不写原因，下次抽取还会抽出同一条）') || ''
      if (!reason.trim()) { msg.value = '驳回必须写原因，已取消'; return }
    }
    await api.exDetailReview(d.detail_id, state, who.value, reason)
    msg.value = d.rule_name + ' → ' + state
    await loadDetails()
  })

const addTag = () =>
  run('tag', async () => {
    const t = tagDraft.value
    if (!t) return
    await api.exDetailTagAdd(t.id, t.tag, who.value, t.why)
    tagDraft.value = null
    await loadDetails()
  })

/** 展开时才去取草稿台账。和条款底账同一个道理：没人看的时候不花这个请求。 */
function toggleDrafts() {
  draftsOpen.value = !draftsOpen.value
  if (draftsOpen.value && !drafts.value.length) loadDrafts()
}

const stepOcr = () =>
  run('ocr', async () => {
    const r = await api.dlOcr({ limit: 5 })
    msg.value = 'OCR：' + r.done + ' 成功 / ' + r.failed + ' 失败，' + r.elapsed_s + ' 秒'
  })
const stepSplit = (onlyChanged: boolean) =>
  run('split', async () => {
    const r = await api.exSplit({ only_changed: onlyChanged })
    msg.value = '切条款：' + r.files + ' 份 → ' + r.clauses + ' 条，候选 ' + r.candidates
    if (clausesOpen.value) await loadClauses()
  })
const stepAi = () =>
  run('ai', async () => {
    const r = await api.exAi({ limit: 8 })
    msg.value = 'AI 草稿：成稿 ' + r.drafted + ' / 试 ' + r.tried
      + '（草稿要进规则明细，再点一次「只跑规则明细」）'
    if (draftsOpen.value) await loadDrafts()
  })

// 进度条、协同全景都在 AgentRuntime 组件里，四个页面共用一份
// ——**不留两份会各自走偏的拷贝**。
// 这一页把 S→D→A→F→I 那列卡片关掉了（phase-cards=false）：
// 这一页的产物是「规则明细」，而协同全景已经画过同一批 Agent 的名字和状态。
// 代价是每个 Agent 的产出数字和备注跟着没了（它们只挂在那列卡片上）；
// 报错没丢——失败的那条后端抬到 job.error，顶上那条红线照显。
// 这一页自己的事只剩：启动、轮询、单步调试。
const jobStatus = computed(() => prog.value?.status || 'idle')

onMounted(async () => {
  meta.value = await api.exMeta().catch(() => null)
  await poll()
  await loadDetails()
  // 增量范围进页面就取：按钮上要写份数，而「点了才知道有没有东西可抽」
  // 等于把一个会花钱的动作当探测用。
  await loadInc().catch(() => {})
  if (prog.value?.status === 'running') startPoll()
})
onUnmounted(stopPoll)
</script>

<template>
  <h2>规则抽取</h2>

  <!-- ── 增量范围 ───────────────────────────────────────────────────────────
       点之前要能看见这一下会动哪几份、为什么、以及**会不会花钱**。
       最后一步是 AI 草稿（真出网、真计费），所以这块不是装饰：
       一个只写着「增量抽取」的按钮，点下去之前使用者不知道是动 0 份还是 14 份。 -->
  <div class="panel">
    <h3>增量范围</h3>
    <div v-if="!inc" class="muted">读取中…</div>
    <template v-else>
      <div class="stat">
        <div><b>{{ inc.total }}</b><span>待抽取文件</span></div>
        <div>
          <b>{{ inc.will_call_model }}{{ inc.candidates_unknown_files.length ? '+' : '' }}</b>
          <span>本次将调模型的候选条款</span>
        </div>
        <div><b>{{ inc.blocked_total }}</b><span>取不到正文（抽取覆盖不到）</span></div>
      </div>

      <div class="row" style="margin-top: 12px">
        <span v-for="w in inc.by_why" :key="w.why" class="tag"
              :class="w.why === '从未抽取' ? 't-pass' : 't-pend'">
          {{ w.why }} {{ w.n }} 份
        </span>
        <span v-if="!inc.total" class="tag t-na">没有新文件，也没有哪份被改过</span>
        <label style="margin-left: auto">
          <input v-model="fullRerun" type="checkbox" @change="armed = false" />
          全量重跑
        </label>
      </div>

      <!-- 增量为 0 份时也要把话说全：那是正常结果，不是功能坏了 -->
      <div :class="inc.will_call_model > 0 && !fullRerun ? 'err' : 'note'">
        <MarkText :text="inc.cost_note" />
      </div>
      <div v-if="fullRerun" class="err">
        <b>全量重跑</b>：忽略增量判断，对所有能取到正文的文件重切条款、重跑映射，
        并对全库候选调模型。**改过切分策略或正则之后必须这样跑一次**——
        否则文件没动、指纹没变，一份也跑不到，会得到「改了但看不出改了」的假象。
      </div>

      <table v-if="inc.files.length" style="margin-top: 8px">
        <thead>
          <tr>
            <th>文件</th><th style="width: 70px">来源</th>
            <th style="width: 150px">为什么在增量里</th>
            <th class="num" style="width: 80px">正文字数</th>
            <th style="width: 150px">变化时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in inc.files" :key="f.file_id">
            <td>
              {{ f.file_name }}
              <div class="muted mono" style="font-size: 11px">{{ f.rel_path }}</div>
            </td>
            <td>
              <span class="tag" :class="f.origin === '上传' ? 't-pass' : 't-na'">
                {{ f.origin }}
              </span>
            </td>
            <td>
              <span class="tag" :class="f.why === '从未抽取' ? 't-pass' : 't-pend'">
                {{ f.why }}
              </span>
            </td>
            <td class="num">{{ f.text_chars }}</td>
            <td class="mono" style="font-size: 11px">
              {{ f.changed_at ? String(f.changed_at).replace('T', ' ').slice(0, 19) : '—' }}
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="inc.candidates_unknown_files.length" class="note">
        另有 <b>{{ inc.candidates_unknown_files.length }}</b> 份文件还没切过条款，
        它们的候选数要切完才知道——所以上面那个数是<b>下限</b>，不是总数。
        <span class="muted">（{{ inc.candidates_unknown_files.slice(0, 3).join('、')
          }}{{ inc.candidates_unknown_files.length > 3 ? ' 等' : '' }}）</span>
      </div>
      <div class="note" style="margin-bottom: 0"><MarkText :text="inc.note" /></div>
    </template>
  </div>

  <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy" :phase-cards="false"
                :start-label="startLabel" running-label="协同进行中…" @start="startJob">
    <template #hint>
      <template v-if="armed">
        <b>再点一次才真跑。</b>第一下只是把范围和要花的钱摆出来。
      </template>
      <template v-else-if="fullRerun">全量重跑：忽略增量判断</template>
      <template v-else>
        只处理增量：从没抽过的 + 抽过之后又被改过的
      </template>
    </template>
  </AgentRuntime>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>

  <!-- 规则明细：一行三块 —— 规则 / 出处 / 分类（标签）。**这一页的产物就是它** -->
  <div class="panel">
    <h3>规则明细</h3>
    <div v-if="dstats" class="kv">
      <b>产出</b>
      <span>
        共 {{ dstats.total }} 条 · 确定性映射 {{ dstats.mapped }} ·
        列对不齐 <b :class="{ bad: dstats.flagged > 0 }">{{ dstats.flagged }}</b> ·
        已采纳 {{ dstats.adopted }} ·
        已进本体 {{ dstats.into_onto }}
      </span>
    </div>
    <div class="row">
      <select v-model="fOrigin" @change="loadDetails()">
        <option value="">全部来源</option>
        <option value="列映射">列映射（确定性）</option>
        <option value="场景拆条">场景拆条（确定性）</option>
        <option value="AI草稿">AI草稿（模型）</option>
      </select>
      <select v-model="fState" @change="loadDetails()">
        <option value="">全部状态</option>
        <option value="待审">待审</option>
        <option value="采纳">采纳</option>
        <option value="驳回">驳回</option>
      </select>
      <select v-model="fMapped" @change="loadDetails()">
        <option value="">列对齐不限</option>
        <option value="y">列已对齐</option>
        <option value="n">列对不齐</option>
      </select>
      <select v-model="fTag" @change="loadDetails()">
        <option value="">人工标签不限</option>
        <option v-for="t in dtags" :key="t.tag" :value="t.tag">
          {{ t.tag }}（{{ t.n }}）
        </option>
      </select>
      <input v-model="fQ" placeholder="搜规则名或判定逻辑" style="width: 170px"
             @keyup.enter="loadDetails()" />
      <button :disabled="!!busy" @click="loadDetails()">筛选</button>
      <button :disabled="!!busy" @click="runDetail">
        {{ busy === 'detailrun' ? '映射中…' : '重跑列映射（不计费）' }}
      </button>
      <input v-model="who" placeholder="审定署名" style="width: 110px" />
    </div>
    <table v-if="details.length">
      <thead>
        <tr>
          <th style="width: 40px" title="当前清单里的第几行。筛选、改排序都会变号——不是规则编号">序号</th>
          <th>规则</th>
          <th style="width: 250px">出处</th>
          <th style="width: 230px">分类（标签）</th>
          <th style="width: 120px">审定</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="(d, i) in details" :key="d.detail_id">
          <tr>
            <!--
              **显示序号，不是规则编号。** 一筛选就会变号，所以不能被引用——
              这条明细的身份是出处里的行号（「第5行」）和 detail_id。
              这个项目吃过「把看起来像编号的东西当成编号」的亏，所以
              表头上写清楚，样式也刻意做成淡灰、不像编号。
            -->
            <td class="num seqno">{{ i + 1 }}</td>
            <td>
              <b>{{ d.rule_name }}</b>
              <span class="tag" :class="d.origin === 'AI草稿' ? 't-pend' : 't-pass'">
                {{ d.origin }}</span>
              <div v-if="d.logic_text" style="font-size: 12px; margin-top: 2px">
                {{ d.logic_text }}
              </div>
              <div v-if="d.exception_text" class="muted" style="font-size: 11px">
                例外：{{ d.exception_text }}
              </div>
              <div v-if="d.numbers.length" class="muted" style="font-size: 11px">
                数值：<code v-for="n in d.numbers" :key="n"
                            style="margin-right: 5px">{{ n }}</code>
              </div>
              <div v-if="!d.mapped_ok" class="err" style="font-size: 11px">
                <b>列对不齐，没有按列映射</b>：{{ d.map_warning }}
              </div>
              <div v-else-if="d.map_warning" class="muted" style="font-size: 11px">
                {{ d.map_warning }}
              </div>
              <div v-if="d.note" class="muted" style="font-size: 11px"><MarkText :text="d.note" /></div>
              <div v-if="d.reject_reason" class="err" style="font-size: 11px">
                驳回原因：<MarkText :text="d.reject_reason" />
              </div>
            </td>
            <td style="font-size: 12px">
              {{ d.source.file_name }}
              <div><code class="mono">{{ d.source.clause_no }}</code>
                <span class="tag t-na">{{ d.source.source_kind }}</span>
                <span class="tag" :class="d.source.confidence === '原生文本'
                  ? 't-pass' : 't-pend'">{{ d.source.confidence }}</span>
              </div>
              <div class="muted" style="font-size: 11px">
                「{{ d.source.excerpt.slice(0, 90) }}」
              </div>
              <div v-if="d.source.model" class="muted" style="font-size: 11px">
                模型：{{ d.source.model }}
              </div>
              <button class="mini" @click="openMap = openMap === d.detail_id
                ? '' : d.detail_id">
                {{ openMap === d.detail_id ? '收起逐列留痕' : '看逐列留痕' }}
              </button>
            </td>
            <td>
              <span v-for="t in d.tags" :key="t.dim + t.value" class="tag"
                    :class="t.sure ? 't-pass' : 't-na'" :title="t.src">
                {{ t.dim }}：{{ t.value }}
              </span>
              <div v-if="d.missing_for_adopt.length" class="muted"
                   style="font-size: 11px">
                这几项要人填，系统不给默认值
              </div>
              <button class="mini" @click="tagDraft = tagDraft?.id === d.detail_id
                ? null : { id: d.detail_id, tag: '', why: '' }">
                {{ tagDraft?.id === d.detail_id ? '取消' : '加人工标签' }}
              </button>
            </td>
            <td>
              <span class="tag" :class="d.review_state === '采纳' ? 't-pass'
                : d.review_state === '驳回' ? 't-hit' : 't-na'">
                {{ d.review_state }}</span>
              <div v-if="d.reviewer" class="muted" style="font-size: 11px">
                {{ d.reviewer }}
              </div>
              <div v-if="d.adopted_rule_id" class="mono" style="font-size: 11px">
                {{ d.adopted_rule_id }}
              </div>
              <div v-if="d.review_state === '待审'" class="acts">
                <button class="mini" :disabled="!!busy || !who"
                        @click="reviewDetail(d, '采纳')">采纳</button>
                <button class="mini" :disabled="!!busy || !who"
                        @click="reviewDetail(d, '驳回')">驳回</button>
              </div>
            </td>
          </tr>
          <tr v-if="openMap === d.detail_id">
            <td colspan="5" class="detailcell">
              <table>
                <tbody>
                  <tr v-for="(v, k) in (d.col_map || {})" :key="k">
                    <td style="width: 190px"><b>{{ k }}</b></td>
                    <td style="font-size: 12px">{{ v }}</td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
          <tr v-if="tagDraft?.id === d.detail_id">
            <td colspan="5" class="detailcell">
              <div class="row">
                <input v-model="tagDraft.tag" placeholder="标签（如 待共享侧补数据）"
                       style="width: 190px" />
                <input v-model="tagDraft.why" placeholder="为什么打这个标签（必填）"
                       style="width: 330px" />
                <button class="mini" :disabled="!!busy || !who || !tagDraft.tag
                  || !tagDraft.why" @click="addTag">保存</button>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    <div v-else class="muted">
      还没有规则明细。点「重跑列映射（不计费）」，或者「开始抽取」跑一次完整协同。
    </div>
  </div>

  <Async :load="() => api.exDetailStorage()">
    <template #default="{ data }">
      <div class="panel">
        <h3>列映射 → 人核列 → 采纳 → <b>到「规则本体」才写 rule 表</b></h3>
        <table>
          <thead>
            <tr>
              <th style="width: 160px">表</th><th style="width: 210px">存的是什么</th>
              <th style="width: 60px">条数</th>
              <th style="width: 160px">谁写的</th><th style="width: 130px">重算时</th>
              <th>为什么这么存</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="x in (data as any).items" :key="x.table_name">
              <td class="mono" style="font-size: 12px">{{ x.table_name }}</td>
              <td style="font-size: 12px">{{ x.what }}</td>
              <td class="num">{{ x.rows }}</td>
              <td class="muted" style="font-size: 12px">{{ x.written_by }}</td>
              <td style="font-size: 12px">{{ x.on_recalc }}</td>
              <td class="muted" style="font-size: 12px"><MarkText :text="x.why" /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </Async>



  <Async :load="() => api.extractPolicy()">
      <template #default="{ data }">
        <div class="panel">
          <h3>处置口径</h3>
          <div class="kv">
            <b>两档</b><span>{{ (data as any).two_tier }}</span>
            <b>允许自动发布</b>
            <span><span class="tag t-hit">
              {{ (data as any).no_auto_publish ? '不允许' : '允许' }}</span></span>
            <b>必须有确认人</b>
            <span><span class="tag t-pass">
              {{ (data as any).requires_reviewer ? '是' : '否' }}</span></span>
          </div>
        </div>
      </template>
  </Async>

  <div class="panel">
    <h3 style="cursor: pointer" @click="debugOpen = !debugOpen">
      {{ debugOpen ? '▾' : '▸' }} 单步调试
    </h3>
    <template v-if="debugOpen">
      <div class="row">
        <button :disabled="!!busy" @click="stepOcr">
          {{ busy === 'ocr' ? 'OCR 中…' : '只跑 OCR' }}
        </button>
        <button :disabled="!!busy" @click="stepSplit(false)">
          {{ busy === 'split' ? '切分中…' : '只切条款（全部 · 不计费）' }}
        </button>
        <button :disabled="!!busy" @click="stepSplit(true)">
          只切条款（仅增量 · 不计费）
        </button>
        <button :disabled="!!busy" @click="runDetail">
          {{ busy === 'detailrun' ? '映射中…' : '只跑规则明细（全部 · 不计费）' }}
        </button>
        <button :disabled="!!busy" @click="stepAi">
          {{ busy === 'ai' ? '模型处理中…' : '只跑 AI 草稿（出网计费）' }}
        </button>
      </div>
      <div class="note">
        这一排是**单步**，和上面的「增量抽取」口径不同，用途也不同：
        <b>「只切条款（全部）」是改过切分策略之后的全量逃生门</b>——
        文件没动、指纹没变时增量一份也跑不到，必须用它重跑一遍。
        <b>「只跑 AI 草稿」不受增量范围限制</b>，它对库里积压的候选挑一批买，
        所以增量为 0 份时想继续跑草稿，用的是这个按钮。
      </div>

      <!-- AI 草稿台账。**唯一的出网台账**，所以不能删只能收。
           规则明细看的是整理后的产物；这里看的是模型原样返回的八要素，
           外加「哪一条原文离开过本系统、发了多少字、有没有被截断、
           哪个型号、第几次抽、数值核对过没有」——审计要的是这些。 -->
      <h3 style="cursor: pointer; margin-top: 14px" @click="toggleDrafts">
        {{ draftsOpen ? '▾' : '▸' }} AI 草稿台账（{{ drafts.length || '未加载' }}）
      </h3>
      <template v-if="draftsOpen">
      <div v-for="d in drafts" :key="d.draft_id" class="panel" style="margin-top: 8px">
        <div class="row" style="margin-bottom: 4px">
          <b>{{ d.file_name }} · {{ d.clause_no }}</b>
          <span class="tag" :class="d.review_state === '采纳' ? 't-pass'
            : d.review_state === '驳回' ? 't-hit' : 't-na'">{{ d.review_state }}</span>
          <span v-if="d.rulable === false" class="tag t-na">模型判定：不可规则化</span>
          <span v-if="d.number_check.ok === false" class="tag t-block">数值存疑</span>
          <span class="muted" style="font-size: 11px">
            {{ d.model }} · {{ d.latency_ms }}ms · 发送 {{ d.sent_chars }} 字
          </span>
        </div>
        <div v-if="d.number_check.ok === false" class="err">
          <b>数值存疑</b>（草稿里的这些数字在条款原文中找不到）：
          <span v-for="s in d.number_check.suspect" :key="s.field + s.value">
            <code>{{ s.field }} = {{ s.value }}</code>
          </span>
        </div>
        <table>
          <tbody>
            <tr v-for="(v, k) in d.draft" :key="k">
              <td style="width: 110px"><b>{{ k }}</b></td>
              <td>{{ v === null ? '（模型未给）' : v }}</td>
            </tr>
          </tbody>
        </table>
      </div>
        <div v-if="!drafts.length" class="muted">还没有草稿</div>
      </template>

      <!-- 条款底账。**不是产物，是证据链的落点。**
           报告里那句「依据：XX第十四条『……』」就是从这儿来的；
           「制度里还有多少条带阈值/禁止却没被任何规则引用」这个数（本次 81 条）
           也只能由它算。所以表不能不要，但不往客户眼前摆。 -->
      <h3 style="cursor: pointer; margin-top: 14px" @click="toggleClauses">
        {{ clausesOpen ? '▾' : '▸' }} 条款底账（{{ clauses.length || '未加载' }}）
      </h3>
      <template v-if="clausesOpen">
      <table v-if="clauses.length">
        <thead>
          <tr><th style="width: 180px">文件</th><th style="width: 84px">条款</th>
            <th>正文</th><th style="width: 140px">特征</th><th style="width: 110px">规则覆盖</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in clauses" :key="c.clause_id">
            <td>
              {{ c.file_name }}
              <div>
                <span class="tag" :class="c.doc_kind === '制度' ? 't-pass' : 't-na'">
                  {{ c.doc_kind }}</span>
                <span v-if="c.text_source === 'OCR'" class="tag t-pend">OCR</span>
                <span v-if="c.from_table_page" class="tag t-block">表格页</span>
                <span v-if="c.stale" class="tag t-hit">已过期</span>
              </div>
            </td>
            <td><span class="mono">{{ c.clause_no }}</span>
              <div v-if="c.title" class="muted" style="font-size: 11px">{{ c.title }}</div>
            </td>
            <td>
              {{ c.snippet }}
              <div v-if="c.numbers.length" class="muted" style="font-size: 11px">
                数值：<code v-for="n in c.numbers" :key="n" style="margin-right: 6px">{{ n }}</code>
              </div>
              <div v-if="c.trust_warning" class="err" style="font-size: 11px">
                {{ c.trust_warning }}
              </div>
            </td>
            <td><span v-for="f in c.features" :key="f" class="tag t-na">{{ f }}</span></td>
            <td>
              <code v-for="r in c.covered_by" :key="r" class="mono"
                    style="display: block; font-size: 11px">{{ r }}</code>
              <span v-if="!c.covered_by.length && c.is_candidate" class="tag t-pend">候选</span>
            </td>
          </tr>
        </tbody>
      </table>
      </template>
    </template>
  </div>
</template>

<style scoped>
button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.detailcell { background: #fafbfc; }
/* 序号刻意做淡：它是「第几行」，不是编号，不该抢 detail_id 和行号的位置 */
.seqno { color: var(--muted); font-size: 11px; vertical-align: top; padding-top: 9px; }
td.acts { white-space: nowrap; }
.acts button { margin-right: 4px; }
b.bad { color: var(--hit); }

</style>
