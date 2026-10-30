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
/** 署名框的引用。没署名时下面每行的提示点一下就跳到它，省得自己找。 */
const whoInput = ref<HTMLInputElement | null>(null)
function focusWho() {
  whoInput.value?.focus()
  // 跳过去还要看得见：表格可能已经滚出去很远了
  whoInput.value?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}
const openMap = ref('')
const tagDraft = ref<{ id: string; tag: string; why: string } | null>(null)

const busy = ref('')
const err = ref('')
const msg = ref('')
const debugOpen = ref(false)
const clausesOpen = ref(false)   // 条款底账，单步调试里的二级折叠

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
/** 只看可成规则。默认不勾——**不可成的那些也要看得见**：
 *  模型说「这条做不成规则」本身就是结论，藏起来等于把它当成没抽过。 */
const onlyRulable = ref(false)

/** 草稿 → 规则清单的一行。
 *
 * 模型返回的是中文键（规则名称/审核维度/…），这里拍平成列，
 * **拍平在这一处做**：模板里到处写 d.draft['规则名称'] 的话，
 * 哪天模型换个键名要改十几处，而漏改的地方只会显示空白，不报错。
 */
const ruleRows = computed(() => drafts.value.map((d) => {
  const g = (k: string) => {
    const v = (d.draft as Record<string, unknown>)?.[k]
    return v == null || v === '' ? null : String(v)
  }
  return {
    draft_id: d.draft_id,
    rulable: d.rulable !== false,
    name: g('规则名称'),
    dim: g('审核维度'),
    cls: g('规则类型'),
    ctrl: g('处置动作'),
    risk: g('风险等级'),
    logic: g('判定逻辑'),
    gap: g('缺口'),
    scope: g('适用范围'),
    except: g('例外情形'),
    file_name: d.file_name,
    clause_no: d.clause_no,
    review_state: d.review_state,
    truncated: d.truncated,
    model: d.model,
    sent_chars: d.sent_chars,
    suspect: d.number_check?.ok === false ? (d.number_check.suspect || []) : [],
  }
}))
const ruleRowsShown = computed(() =>
  onlyRulable.value ? ruleRows.value.filter(r => r.rulable) : ruleRows.value)
const suspectRows = computed(() => ruleRows.value.filter(r => r.suspect.length))
const truncRows = computed(() => ruleRows.value.filter(r => r.truncated))

/** 上膛的是哪一种。null = 没上膛。
 *
 * **按模式分开记，不能用一个布尔。** 两个按钮共用一个 armed 的话，
 * 点「全量」上膛之后再点「新增」会直接跑成全量——而那一下是真花钱的。
 */
const armedMode = ref<null | 'full' | 'inc'>(null)

/** 清单里三种状态的配色。够不着要显眼：它是「这份制度没进来」。 */
const STATE_CLS: Record<string, string> = {
  待抽取: 't-pend', 已抽取: 't-pass', 够不着: 't-hit',
}

const loadInc = () =>
  run('inc', async () => { inc.value = await api.exIncremental() })

const startJob = (full: boolean) =>
  run('run', async () => {
    const mode = full ? 'full' : 'inc'
    // 新增为 0 → 不发请求。发了也只是四步空跑，
    // 但那会在台账里留下一次没有意义的作业记录。
    if (!full && inc.value?.total === 0) {
      msg.value = '没有新增：没有新文件，也没有哪份文件在上次抽取之后被改过。'
        + '改过切分策略要重切，用「全量抽取」。'
      return
    }
    // 两段确认。最后一步是 AI 草稿，**真出网、真计费**，
    // 第一下只把要花的钱摆出来。不用 confirm()：那个会被浏览器拦，
    // 而拦了之后什么都不会发生。
    if (armedMode.value !== mode) {
      armedMode.value = mode
      const k = inc.value?.will_call_model ?? 0
      const money = k > 0 ? `，并对最多 ${k} 条候选调模型（**出网计费**）` : ''
      msg.value = full
        ? `再点一次「全量抽取」才真跑：会对所有能取到正文的文件重切条款${money}。`
        : `再点一次「新增抽取」才真跑：处理 ${inc.value?.total} 份${money}。`
      return
    }
    armedMode.value = null
    const r = await api.exRun({ only_changed: !full })
    if (!r.ok) {
      msg.value = r.reason || '已有作业在运行'
      return
    }
    msg.value = (full ? '已启动**全量**协同作业 ' : '已启动新增协同作业 ') + r.job_id
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
    await loadDrafts()      // 规则清单常驻在页面上，跑完直接刷新它
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
  // 规则清单进页面就取。它原来挂在「AI 草稿台账」那个折叠块里、点开才加载，
  // 于是这一页最该被看见的东西（抽出来的规则到底有哪些）默认是看不见的。
  await loadDrafts().catch(() => {})
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
      <!-- **一张清单，不是一排数字。**
           「在库 12 / 待抽取 0 / 够不着 5」解释不了差额去哪了，
           而使用者想知道的本来就是「哪几份没进来、为什么」。清单不用解释。 -->
      <div class="row" style="margin-bottom: 10px">
        <button :disabled="!!busy" @click="startJob(true)">
          {{ busy ? '运行中…' : '全量抽取' }}
        </button>
        <button :disabled="!!busy || !inc.total" @click="startJob(false)">
          新增抽取{{ inc.total ? `（${inc.total} 份）` : '（无新增）' }}
        </button>
        <span class="muted">
          全量 = 所有能取到正文的文件重切一遍；新增 = 只处理没抽过的和改过的。
          <b v-if="inc.will_call_model">最后一步会对最多 {{ inc.will_call_model }} 条候选调模型（计费）。</b>
        </span>
      </div>

      <table>
        <thead>
          <tr>
            <th>文件</th>
            <th style="width: 76px">状态</th>
            <th style="width: 76px">正文来源</th>
            <th class="num" style="width: 64px">字数</th>
            <th class="num" style="width: 56px">条款</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in inc.inventory" :key="f.file_id">
            <td>
              {{ f.file_name }}
              <div class="muted mono" style="font-size: 11px">{{ f.rel_path }}</div>
            </td>
            <td>
              <span class="tag" :class="STATE_CLS[f.state] || 't-na'">{{ f.state }}</span>
            </td>
            <td>
              <span class="tag" :class="f.text_source === '读图' ? 't-demo'
                                        : f.text_source ? 't-pass' : 't-hit'">
                {{ f.text_source || '取不到' }}
              </span>
            </td>
            <td class="num">{{ f.text_source === '读图' ? '—' : f.text_chars }}</td>
            <td class="num">{{ f.clauses }}</td>
            <td class="muted" style="font-size: 12px">{{ f.why }}</td>
          </tr>
        </tbody>
      </table>
      <div class="note" style="margin-bottom: 0">
        共 <b>{{ inc.inventory.length }}</b> 份：待抽取 {{ inc.total }} ·
        已抽取 {{ inc.extracted }} ·
        <b :style="inc.blocked_total ? 'color: var(--hit)' : ''">够不着 {{ inc.blocked_total }}</b>。
        「读图」来源的字数列不显示原生字数——那一档的原生文字层本来就是残缺的，
        显示出来会让人以为正文只有那么点。
      </div>
    </template>
  </div>

  <!-- 启动按钮画在上面那块清单里（「全量抽取」「新增抽取」），
       这里只留进度与协同全景，所以 hide-start。 -->
  <AgentRuntime :prog="prog" :meta="meta" :disabled="!!busy" :phase-cards="false"
                hide-start start-label="" running-label="协同进行中…" @start="() => {}">
    <template #hint>
      <template v-if="armedMode">
        <b>再点一次那个按钮才真跑。</b>第一下只是把范围和要花的钱摆出来。
      </template>
      <template v-else>
        全量 = 所有能取到正文的文件重切一遍；新增 = 从没抽过的 + 抽过之后又被改过的
      </template>
    </template>
  </AgentRuntime>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>

  <!-- ── 规则清单 ─────────────────────────────────────────────────────────
       **一行一条规则**，默认展开、不折叠。
       原来这一页上草稿只在「AI 草稿台账」里，默认收起、点开才加载，
       而且是一条一个面板堆下去——97 条根本没法看，于是页面上
       「抽出来的规则到底有哪些」这个最该看见的东西反而看不见。
       台账那一块保留（它是唯一的出网留痕，审计要），但收到下面去。 -->
  <div class="panel">
    <div class="row">
      <h3 style="margin: 0">规则清单</h3>
      <span class="muted">模型从条款抽出来的规则草稿。<b>草稿不是规则</b>——
        要进规则本体必须在下面「规则明细」里人工采纳，系统不自动落。</span>
      <button style="margin-left: auto" :disabled="!!busy" @click="loadDrafts">
        {{ busy === 'drafts' ? '读取中…' : '刷新' }}
      </button>
    </div>

    <div v-if="!drafts.length" class="muted">
      还没有草稿。先用上面的「全量抽取」或「新增抽取」跑一轮。
    </div>
    <template v-else>
      <div class="row" style="margin-bottom: 6px">
        <span class="tag t-pass">可成规则 {{ ruleRows.filter(r => r.rulable).length }}</span>
        <span class="tag t-na">不可成 {{ ruleRows.filter(r => !r.rulable).length }}</span>
        <span v-if="suspectRows.length" class="tag t-hit">
          数值存疑 {{ suspectRows.length }}
        </span>
        <span v-if="truncRows.length" class="tag t-pend">
          原文被截断 {{ truncRows.length }}
        </span>
        <label style="margin-left: auto">
          <input v-model="onlyRulable" type="checkbox" /> 只看可成规则
        </label>
      </div>

      <table>
        <thead>
          <tr>
            <th style="width: 30px" class="num">#</th>
            <th>规则名称</th>
            <th style="width: 120px">审核维度</th>
            <th style="width: 52px">类型</th>
            <th style="width: 52px">处置</th>
            <th style="width: 44px">风险</th>
            <th>判定逻辑</th>
            <th style="width: 190px">缺口（现在还判不了什么）</th>
            <th style="width: 150px">出处</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in ruleRowsShown" :key="r.draft_id">
            <td class="num muted">{{ i + 1 }}</td>
            <td>
              <b v-if="r.rulable">{{ r.name || '（模型未给名称）' }}</b>
              <span v-else class="muted">{{ r.name || '（判定不可规则化）' }}</span>
              <div class="row" style="gap: 4px; margin: 2px 0 0">
                <span v-if="!r.rulable" class="tag t-na">不可成规则</span>
                <span v-if="r.suspect.length" class="tag t-hit"
                      :title="r.suspect.map(s => s.field + '=' + s.value).join('；')">
                  数值存疑
                </span>
                <span v-if="r.truncated" class="tag t-pend">原文被截断</span>
                <span v-if="r.review_state !== '待审'" class="tag t-pass">
                  {{ r.review_state }}
                </span>
              </div>
            </td>
            <td style="font-size: 12px">{{ r.dim || '—' }}</td>
            <td><span class="tag" :class="r.cls === '硬' ? 't-pass'
                                          : r.cls === '软' ? 't-na' : 't-demo'">
              {{ r.cls || '—' }}</span></td>
            <td><span class="tag" :class="r.ctrl === '禁止' ? 't-hit' : 't-pend'">
              {{ r.ctrl || '—' }}</span></td>
            <td style="font-size: 12px">{{ r.risk || '—' }}</td>
            <td style="font-size: 12px">{{ r.logic || '—' }}</td>
            <!-- 缺口这一栏不许省：它说的是「这条制度现在还判不了什么」。
                 缺口没补，规则落下去也是空转，而判定逻辑看起来却很完整。 -->
            <td style="font-size: 12px" class="muted">{{ r.gap || '—' }}</td>
            <td style="font-size: 11px">
              {{ r.file_name }}
              <div class="mono muted">{{ r.clause_no }}</div>
              <!-- 出网留痕：这一条原文离开过本系统，发了多少字、哪个型号。
                   草稿台账那一块去掉之后，这是页面上唯一还说得出这件事的地方。 -->
              <div class="muted" style="font-size: 10px">
                {{ r.model || '—' }} · 发送 {{ r.sent_chars }} 字
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="note" style="margin-bottom: 0">
        共 <b>{{ ruleRowsShown.length }}</b> 条{{ onlyRulable ? '（已过滤掉不可成规则的）' : '' }}。
        <b v-if="suspectRows.length">「数值存疑」那 {{ suspectRows.length }} 条是
        草稿里出现了条款原文里找不到的阈值——采纳前必须回查原文。</b>
        <b v-if="truncRows.length">「原文被截断」那 {{ truncRows.length }} 条，
        模型没看完整条原文，判定逻辑可能不全。</b>
      </div>
    </template>
  </div>

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
      <!-- 署名空着，下面每一行的「采纳 / 驳回」就全是禁用态。
           **这件事原来页面上一个字都没说**：按钮灰着、署名框在屏幕另一头，
           看起来就是「功能坏了」。所以空的时候这里要显眼，
           并且下面的行里给一条能点回来的提示。 -->
      <input ref="whoInput" v-model="who"
             :class="{ 'need-fill': !who }"
             :placeholder="who ? '审定署名' : '审定署名（必填）'"
             title="审定要留名：没有署名的审定在审计上等于没发生"
             style="width: 130px" />
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
                <!-- 没署名时**不给灰按钮，给一条能点的提示**。
                     灰按钮不解释自己为什么灰，看起来和坏了一样；
                     点这条直接跳到上面的署名框，不用自己找。 -->
                <a v-if="!who" href="#" class="need-who" @click.prevent="focusWho">
                  填「审定署名」后可采纳/驳回 ↑
                </a>
                <template v-else>
                  <button class="mini" :disabled="!!busy"
                          @click="reviewDetail(d, '采纳')">采纳</button>
                  <button class="mini" :disabled="!!busy"
                          @click="reviewDetail(d, '驳回')">驳回</button>
                </template>
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
                <!-- 打标签同样要署名，同样给原因，别让人对着灰按钮猜 -->
                <a v-if="!who" href="#" class="need-who" @click.prevent="focusWho">
                  还要填「审定署名」↑
                </a>
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

      <!-- AI 草稿台账（逐条面板）已按用户要求去掉，只保留上面那张「规则清单」。
           **数据一行没删**：doclib_draft 照旧逐条留痕（型号、发送字数、
           是否截断、数值核对、第几次抽），接口 /api/extract/drafts 也还在，
           去掉的只是这一处渲染。出网留痕里最该被看见的三样
           （数值存疑 / 原文被截断 / 型号与发送字数）已经搬进清单的行里，
           不是连同台账一起消失了。 -->

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
/* 必填但还没填。**用警示色不用错误色**——它不是「填错了」，是「还没填」。 */
.need-fill {
  border-color: var(--pend, #d48806);
  background: #fffbe6;
}
/* 没署名时顶替灰按钮的那条提示。灰按钮不解释自己为什么灰，
   看起来和功能坏了一样——这条既说清原因，又能点回去填。 */
.need-who {
  font-size: 11px;
  color: var(--pend, #d48806);
  white-space: nowrap;
}
button.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.detailcell { background: #fafbfc; }
/* 序号刻意做淡：它是「第几行」，不是编号，不该抢 detail_id 和行号的位置 */
.seqno { color: var(--muted); font-size: 11px; vertical-align: top; padding-top: 9px; }
td.acts { white-space: nowrap; }
.acts button { margin-right: 4px; }
b.bad { color: var(--hit); }

</style>
