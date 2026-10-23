<script setup lang="ts">
/**
 * 系统 · 智能体工作台。五段：总览 / 契约 / 协同与信号 / 运行 / 存储。
 *
 * 2026-10-19 按要求在系统一级菜单下新增。
 *
 * 做它的理由:全站 12 个 Agent 散在三处——契约在 config/agents/*.yaml、
 * 运行时状态在 agent_state、运行痕迹在 agent_signal_log 与 cycle_log，
 * 而编排分散在四条链上，每条链的入口挂在它自己那一栏的页面里。
 * **结果是没有一个地方能回答「现在有几个智能体、各自什么状态」。**
 *
 * **这一页只读。** 不改参数、不投信号、不停 Agent：
 * 改参数要走 improve 的 bounds 校验与 improve_log，不能从页面绕过去；
 * 手工投信号会产生一条「没有上游」的协同记录，审计时说不清是谁触发的。
 * 能启动的只有那三条链，而且启动的是**同一个编排**（app/agent_job.py），
 * 不是这一页新造的能力。
 *
 * 页面上三条口径必须写出来，不写就是在说谎：
 *   · 契约 12 个，agent_state 只有 2 行——那不是缺数据，是两种编排方式的差别
 *   · 心跳全没跳过——**空不等于坏，等于没开**（没有常驻调度器在跑）
 *   · 三条手工链的信号 2026-10-19 之前一条都没落库（实测的 bug，已修）
 */
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type {
  AgentChain, AgentCycleRow, AgentDetail, AgentOverview, AgentSignalRow, StorageRow,
} from '../api'

type Tab = 'all' | 'contract' | 'bus' | 'run' | 'store'
const tab = ref<Tab>('all')

const ov = ref<AgentOverview | null>(null)
const det = ref<AgentDetail | null>(null)
const pick = ref('')
const checks = ref<{ items: { check: string; ok: boolean; detail: string }[]
                     total: number; failed: number; note: string } | null>(null)
const showAllChecks = ref(false)
const showYaml = ref(false)
const sig = ref<Awaited<ReturnType<typeof api.agentSignals>> | null>(null)
const cyc = ref<Awaited<ReturnType<typeof api.agentCycles>> | null>(null)
const chains = ref<AgentChain[]>([])
const chainNote = ref('')
const store = ref<{ items: StorageRow[]; note: string } | null>(null)

const busy = ref('')
const err = ref('')
const msg = ref('')
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

const loadOv = () => run('ov', async () => { ov.value = await api.agentOverview() })
const loadChains = () =>
  run('ch', async () => {
    const r = await api.agentChains()
    chains.value = r.items
    chainNote.value = r.note
  })

const openAgent = (id: string) =>
  run('d', async () => {
    pick.value = id
    showYaml.value = false
    det.value = await api.agentDetail(id)
    tab.value = 'contract'
  })

function pickTab(t: Tab) {
  tab.value = t
  if (t === 'all' && !ov.value) loadOv()
  if (t === 'contract' && !det.value && ov.value?.items.length)
    openAgent(ov.value.items[0].agent_id)
  if (t === 'bus' && !sig.value) {
    run('s', async () => { sig.value = await api.agentSignals(50) })
    run('c', async () => { cyc.value = await api.agentCycles(50) })
  }
  if (t === 'run' && !chains.value.length) loadChains()
  if (t === 'store' && !store.value)
    run('st', async () => { store.value = await api.agentStorage() })
}

const loadChecks = () =>
  run('k', async () => { checks.value = await api.agentChecks() })

/** 轮询:启动一条链之后盯着它，跑完自动停。 */
function poll(chainId: string) {
  stop()
  timer = window.setInterval(async () => {
    try {
      const p = await api.agentChainProgress(chainId)
      const row = chains.value.find((c) => c.chain_id === chainId)
      if (row) {
        row.status = p.status
        row.pct = p.pct
        row.job_id = p.job_id
        row.elapsed = p.elapsed
        row.signals_in_job = (p.signals || []).length
        row.error = p.error
      }
      if (p.status !== 'running') {
        stop()
        await loadChains()
      }
    } catch {
      // 轮询失败不清空表格:清空会让页面在网络抖一下时整个塌掉
      stop()
    }
  }, 1000)
}
function stop() {
  if (timer !== undefined) {
    window.clearInterval(timer)
    timer = undefined
  }
}

const startChain = (c: AgentChain) =>
  run('go', async () => {
    const r = await api.agentChainRun(c.chain_id)
    msg.value = r.ok ? `已启动「${c.chain_name}」 ${r.job_id}`
      : (r.reason || '已有作业在运行')
    if (r.ok) poll(c.chain_id)
  })

const LAYER_WHY: Record<string, string> = {
  L0: '感知:只看见，不判断。感知层一旦下结论，「谁发现的」与「谁判的」就混在一起',
  L1: '研判:在自己的领域里出结论',
  L2: '协同:汇总与仲裁，不自己产内容',
}
const ST_CN: Record<string, string> = {
  idle: '未开始', running: '运行中', done: '已完成', failed: '失败',
}

const triple = computed(() => ov.value?.items.filter((x) => x.no_judgement) || [])

onMounted(loadOv)
</script>

<template>
  <h2>智能体工作台</h2>
  <div class="sub">
    全站 {{ ov?.totals.agents ?? '—' }} 个智能体:契约在
    <code>config/agents/*.yaml</code>、状态在 <code>agent_state</code>、
    痕迹在 <code>agent_signal_log</code> 与 <code>cycle_log</code>。
    <b>这一页只读</b>——不改参数、不投信号、不停 Agent。
  </div>

  <div class="row">
    <button :class="{ on: tab === 'all' }" @click="pickTab('all')">总览</button>
    <button :class="{ on: tab === 'contract' }" @click="pickTab('contract')">契约</button>
    <button :class="{ on: tab === 'bus' }" @click="pickTab('bus')">协同与信号</button>
    <button :class="{ on: tab === 'run' }" @click="pickTab('run')">运行</button>
    <button :class="{ on: tab === 'store' }" @click="pickTab('store')">存储</button>
    <span v-if="ov" class="muted" style="margin-left: auto">
      L0 {{ ov.totals.by_layer.L0 }} · L1 {{ ov.totals.by_layer.L1 }} ·
      L2 {{ ov.totals.by_layer.L2 }} ·
      契约校验 {{ ov.totals.checks }} 项
      <span class="tag" :class="ov.totals.failed ? 't-hit' : 't-pass'">
        失败 {{ ov.totals.failed }}</span>
    </span>
  </div>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="ok-note">{{ msg }}</div>

  <!-- ① 总览 ---------------------------------------------------------------->
  <template v-if="tab === 'all'">
    <!-- 原来这里是「实话先说」面板（契约数 vs agent_state 行数、心跳没跳过、
         有运行痕迹的个数三条）。2026-10-21 按要求从页面去掉。
         **数据本身没删**：/api/agents 的 facts 仍然返回这三条，
         tests/test_agent_hub.py::test_实话必须带原因 还钉着「每条都要带 why」。
         要用的时候把这个面板加回来就行，不必再去后端找口径。 -->

    <div v-if="ov" class="panel">
      <h3>{{ ov.totals.agents }} 个智能体</h3>
      <div class="sub">
        层级不是大小:<b>L0 只看见不判断</b>、L1 研判、L2 协同与仲裁。
        点一行看它的完整契约。
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 34px">层</th><th style="width: 160px">智能体</th>
            <th style="width: 150px">所属链</th>
            <th style="width: 120px">触发</th>
            <th class="num" style="width: 40px">读</th>
            <th class="num" style="width: 40px">写</th>
            <th class="num" style="width: 44px">可调参</th>
            <th style="width: 70px">状态行</th>
            <th style="width: 80px">上次心跳</th>
            <th class="num" style="width: 46px">信号</th>
            <th class="num" style="width: 46px">周期</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in ov.items" :key="a.agent_id" class="clickable"
              :class="{ on: pick === a.agent_id }" @click="openAgent(a.agent_id)">
            <td>
              <span class="tag" :class="a.layer === 'L0' ? 't-na'
                : a.layer === 'L1' ? 't-pend' : 't-pass'">{{ a.layer }}</span>
            </td>
            <td>
              <b>{{ a.agent_name }}</b>
              <div class="muted mono" style="font-size: 10px">{{ a.agent_id }}</div>
            </td>
            <td style="font-size: 12px">{{ a.chain_name || '—' }}</td>
            <td style="font-size: 11px">
              {{ a.triggers.join('+') }}
              <span v-if="a.heartbeat_seconds" class="muted">{{ a.heartbeat_seconds }}s</span>
            </td>
            <td class="num">{{ a.reads.length }}</td>
            <td class="num">{{ a.writes.length }}</td>
            <td class="num">{{ a.params }}</td>
            <td>
              <span v-if="a.has_state" class="tag t-pass">有</span>
              <span v-else class="tag t-na">无</span>
            </td>
            <td class="muted" style="font-size: 11px">
              {{ a.last_beat_at ? a.last_beat_at.toString().slice(5, 16) : '没跳过' }}
            </td>
            <td class="num" :class="{ muted: !a.signals }">{{ a.signals || '—' }}</td>
            <td class="num" :class="{ muted: !a.cycles }">{{ a.cycles || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div class="note">
        <div v-for="(why, k) in LAYER_WHY" :key="k">
          <b>{{ k }}</b> —— <MarkText :text="why" />
        </div>
      </div>
    </div>

    <div v-if="triple.length" class="panel">
      <h3>声明了「不下结论」的 {{ triple.length }} 个</h3>
      <div class="sub">
        <code>decide.no_judgement: true</code> 是契约里的一条自我约束,不是省事。
        感知层开始下结论,出错时就分不清是**看漏了**还是**判错了**。
      </div>
      <div class="row" style="flex-wrap: wrap">
        <code v-for="a in triple" :key="a.agent_id" class="chip"
              @click="openAgent(a.agent_id)">{{ a.agent_name }}</code>
      </div>
    </div>
  </template>

  <!-- ② 契约 ---------------------------------------------------------------->
  <template v-if="tab === 'contract'">
    <div class="panel">
      <div class="row" style="flex-wrap: wrap; margin: 0">
        <button v-for="a in ov?.items || []" :key="a.agent_id" class="mini"
                :class="{ on: pick === a.agent_id }" @click="openAgent(a.agent_id)">
          <span class="muted">{{ a.layer }}</span> {{ a.agent_name }}
        </button>
      </div>
    </div>

    <template v-if="det">
      <div class="panel">
        <h3>
          {{ det.agent_name }}
          <span class="tag" :class="det.layer === 'L0' ? 't-na'
            : det.layer === 'L1' ? 't-pend' : 't-pass'">{{ det.layer }}</span>
          <span class="muted mono" style="font-size: 12px">
            {{ det.agent_id }} v{{ det.agent_version }}
          </span>
        </h3>
        <div class="sub">{{ det.desc }}</div>
        <div class="kv">
          <b>所属链</b>
          <span>{{ det.chain_name || '—' }}
            <span class="muted" style="font-size: 12px">
              （由哪个编排跑它;它订阅谁的信号见下面的触发器——两件事会不一样）
            </span>
          </span>
          <b>执行策略</b><span>{{ det.execution_strategy }}</span>
          <b>运行时参数</b>
          <span>
            <code>{{ JSON.stringify(det.state.params) }}</code>
            <span class="muted" style="font-size: 12px; margin-left: 6px">
              来自 {{ det.state.params_from }}</span>
          </span>
          <b>上次心跳</b>
          <span>
            {{ det.state.last_beat_at || '没跳过' }}
            <span v-if="!det.state.last_beat_at" class="muted" style="font-size: 12px">
              —— POC 里没有常驻调度器在跑,**空不等于坏,等于没开**
            </span>
          </span>
        </div>
      </div>

      <div class="panel">
        <h3>触发器</h3>
        <table>
          <thead>
            <tr>
              <th style="width: 80px">类型</th><th style="width: 54px">启用</th>
              <th style="width: 200px">通道 / 间隔</th><th>为什么这么设</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(t, k) in det.triggers" :key="k">
              <td class="mono">{{ k }}</td>
              <td>
                <span class="tag" :class="t.enabled ? 't-pass' : 't-na'">
                  {{ t.enabled ? '开' : '关' }}</span>
              </td>
              <td class="mono" style="font-size: 11px">
                {{ (t.channels as string[] | undefined)?.join('、')
                  || (t.interval_seconds ? t.interval_seconds + ' 秒' : '—') }}
              </td>
              <td style="font-size: 12px"><MarkText :text="t.why as (string | undefined)" /></td>
            </tr>
          </tbody>
        </table>
        <div class="note">
          **心跳不是可选项**（契约校验强制）:LISTEN/NOTIFY 是尽力而为的,
          连接断开期间的通知收不到**且不报错**。只靠消息触发会在重连窗口静默丢任务。
        </div>
      </div>

      <div class="panel">
        <h3>S → D → A → F → I</h3>
        <div class="sdafi">
          <div class="blk">
            <b>S 读</b>
            <div class="muted" style="font-size: 12px">
              只允许读这些表（执行点 app/runtime/pipeline.py）
            </div>
            <div><code v-for="t in det.sdafi.S.tables" :key="t" class="chip">{{ t }}</code></div>
            <pre v-if="det.sdafi.S.select" class="pre">{{ det.sdafi.S.select }}</pre>
          </div>
          <div class="blk">
            <b>D 判</b>
            <div v-if="det.sdafi.D.no_judgement" class="tag t-na">声明不下结论</div>
            <div class="muted" style="font-size: 12px">{{ det.sdafi.D.kind }}</div>
            <div>
              <code v-for="v in det.sdafi.D.conclusion_values" :key="v" class="chip">{{ v }}</code>
            </div>
            <pre v-if="det.sdafi.D.pending_why" class="pre">判不出时:{{ det.sdafi.D.pending_counts_as }}
{{ det.sdafi.D.pending_why }}</pre>
            <pre v-if="det.sdafi.D.no_judgement_why" class="pre">{{ det.sdafi.D.no_judgement_why }}</pre>
          </div>
          <div class="blk">
            <b>A 写</b>
            <div class="muted" style="font-size: 12px">
              <b>唯一允许写库的阶段</b>,且只能写这些表
            </div>
            <div><code v-for="t in det.sdafi.A.writes" :key="t" class="chip">{{ t }}</code></div>
            <div v-if="det.sdafi.A.signal.channel" style="font-size: 12px; margin-top: 4px">
              投信号 → <code>{{ det.sdafi.A.signal.channel }}</code>
            </div>
            <pre v-if="det.sdafi.A.idempotency" class="pre">幂等键 {{ det.sdafi.A.idempotency }}
{{ det.sdafi.A.idempotency_why }}</pre>
          </div>
          <div class="blk">
            <b>F 量</b>
            <table v-if="det.sdafi.F.metrics.length">
              <tbody>
                <tr v-for="m in det.sdafi.F.metrics" :key="m.metric_name">
                  <td class="mono" style="width: 150px; font-size: 11px">
                    {{ m.metric_name }}
                  </td>
                  <td style="font-size: 12px">{{ m.desc }}</td>
                  <td class="mono muted" style="width: 130px; font-size: 11px">
                    {{ m.source }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="blk">
            <b>I 调</b>
            <table v-if="det.sdafi.I.params.length">
              <thead>
                <tr>
                  <th style="width: 150px">参数</th><th style="width: 60px">默认</th>
                  <th style="width: 100px">允许区间</th><th>为什么是这个区间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in det.sdafi.I.params" :key="p.param_name">
                  <td class="mono" style="font-size: 11px">{{ p.param_name }}</td>
                  <td class="num">{{ p.default }}</td>
                  <td class="mono" style="font-size: 11px">[{{ p.lo }}, {{ p.hi }}]</td>
                  <td style="font-size: 12px"><MarkText :text="p.why" /></td>
                </tr>
              </tbody>
            </table>
            <div style="margin-top: 6px">
              <b style="color: var(--hit)">禁止自调（{{ det.sdafi.I.forbidden.length }}）</b>
              <div v-for="f in det.sdafi.I.forbidden" :key="f" style="font-size: 12px">
                · {{ f }}
              </div>
            </div>
          </div>
        </div>
        <div class="note"><MarkText :text="det.note" /></div>
      </div>

      <div class="panel">
        <div class="row" style="margin: 0">
          <h3 style="margin: 0">
            契约原文 <code class="muted">{{ det.contract_file.file }}</code>
          </h3>
          <span class="muted" style="font-size: 12px">
            {{ det.contract_file.lines }} 行,其中 {{ det.contract_file.comment_lines }}
            行是注释——<b>注释就是口径本身</b>
          </span>
          <button class="mini" style="margin-left: auto" @click="showYaml = !showYaml">
            {{ showYaml ? '收起' : '看原文' }}
          </button>
        </div>
        <pre v-if="showYaml" class="yaml">{{ det.contract_file.text }}</pre>
      </div>
    </template>

    <div class="panel">
      <div class="row" style="margin: 0">
        <h3 style="margin: 0">契约校验</h3>
        <button class="mini" :disabled="!!busy" @click="loadChecks">现跑一遍</button>
        <span v-if="checks" class="muted" style="font-size: 12px">
          {{ checks.total }} 项,失败
          <b :style="{ color: checks.failed ? 'var(--hit)' : 'var(--pass)' }">
            {{ checks.failed }}</b>
        </span>
        <button v-if="checks" class="mini" @click="showAllChecks = !showAllChecks">
          {{ showAllChecks ? '只看失败' : '看全部' }}
        </button>
      </div>
      <div v-if="checks">
        <div class="note"><MarkText :text="checks.note" /></div>
        <table v-if="showAllChecks || checks.failed">
          <thead><tr><th style="width: 54px">结果</th><th style="width: 330px">检查项</th>
            <th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(k, i) in checks.items.filter((x) => showAllChecks || !x.ok)"
                :key="i">
              <td><span class="tag" :class="k.ok ? 't-pass' : 't-hit'">
                {{ k.ok ? '过' : '失败' }}</span></td>
              <td style="font-size: 12px">{{ k.check }}</td>
              <td class="muted" style="font-size: 11px"><MarkText :text="k.detail" /></td>
            </tr>
          </tbody>
        </table>
        <div v-else class="note" style="background: #f1f8f2">
          {{ checks.total }} 项全过。<b>和启动时跑的是同一套</b>——另写一份校验就会出现
          「启动能过、页面报错」这种谁也说不清的状态。
        </div>
      </div>
    </div>
  </template>

  <!-- ③ 协同与信号 ---------------------------------------------------------->
  <template v-if="tab === 'bus'">
    <div v-if="sig" class="panel">
      <h3>数据池</h3>
      <div class="sub">
        <b>Agent 之间零直接调用</b>:上游写进数据池、发信号,下游从池子里读。
        <code>agent_signal</code> 是在途的信号,<code>agent_signal_log</code> 是不可改的台账。
      </div>
      <div class="kv">
        <template v-for="f in sig.facts" :key="f.fact">
          <b>{{ f.fact }}</b><span style="font-size: 12px"><MarkText :text="f.why" /></span>
        </template>
      </div>
      <table v-if="sig.pool.length">
        <thead><tr><th style="width: 220px">通道</th><th style="width: 90px">状态</th>
          <th class="num" style="width: 60px">条数</th><th>最近一条</th></tr></thead>
        <tbody>
          <tr v-for="(p, i) in sig.pool" :key="i">
            <td class="mono" style="font-size: 12px">{{ p.channel }}</td>
            <td>
              <span class="tag" :class="p.status === 'pending' ? 't-pend' : 't-pass'">
                {{ p.status }}</span>
            </td>
            <td class="num">{{ p.n }}</td>
            <td class="muted mono" style="font-size: 11px">
              {{ p.last?.toString().slice(0, 19) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="sig" class="panel">
      <h3>谁投过信号</h3>
      <table>
        <thead><tr><th style="width: 260px">智能体</th><th class="num" style="width: 60px">条数</th>
          <th style="width: 150px">第一条</th><th>最近一条</th></tr></thead>
        <tbody>
          <tr v-for="a in sig.by_agent" :key="a.agent_id">
            <td class="mono" style="font-size: 12px">{{ a.agent_id }}</td>
            <td class="num">{{ a.n }}</td>
            <td class="muted mono" style="font-size: 11px">
              {{ a.first?.toString().slice(0, 19) }}
            </td>
            <td class="muted mono" style="font-size: 11px">
              {{ a.last?.toString().slice(0, 19) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="sig" class="panel">
      <h3>信号台账（最近 {{ sig.items.length }} 条）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 130px">时间</th><th style="width: 200px">智能体</th>
            <th style="width: 100px">动作</th><th style="width: 180px">目标</th>
            <th style="width: 150px">写了哪些表</th><th>幂等键</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in sig.items" :key="s.id">
            <td class="mono" style="font-size: 11px">
              {{ s.emitted_at.toString().slice(5, 19) }}
            </td>
            <td class="mono" style="font-size: 11px">{{ s.agent_id }}</td>
            <td style="font-size: 12px">{{ s.action }}</td>
            <td class="mono" style="font-size: 11px">{{ s.target }}</td>
            <td class="mono muted" style="font-size: 10px">
              {{ s.written_tables ? JSON.stringify(s.written_tables) : '—' }}
            </td>
            <td class="mono muted" style="font-size: 10px">{{ s.idempotency_key }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="cyc" class="panel">
      <h3>周期（{{ cyc.by_agent.reduce((n, x) => n + x.n, 0) }} 次）</h3>
      <div class="sub"><MarkText :text="cyc.note" /></div>
      <table v-if="cyc.by_agent.length">
        <thead><tr><th style="width: 260px">智能体</th><th class="num" style="width: 60px">周期</th>
          <th class="num" style="width: 60px">非 ok</th><th>最近</th></tr></thead>
        <tbody>
          <tr v-for="a in cyc.by_agent" :key="a.agent_id">
            <td class="mono" style="font-size: 12px">{{ a.agent_id }}</td>
            <td class="num">{{ a.n }}</td>
            <td class="num" :class="{ warn: a.bad }">{{ a.bad }}</td>
            <td class="muted mono" style="font-size: 11px">
              {{ a.last?.toString().slice(0, 19) }}
            </td>
          </tr>
        </tbody>
      </table>
      <table v-if="cyc.items.length">
        <thead>
          <tr>
            <th style="width: 120px">开始</th><th style="width: 100px">触发</th>
            <th style="width: 110px">主体</th><th style="width: 70px">结论</th>
            <th style="width: 56px">状态</th><th style="width: 100px">规则集</th>
            <th>各阶段毫秒</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in cyc.items" :key="c.cycle_id">
            <td class="mono" style="font-size: 11px">
              {{ c.started_at.toString().slice(5, 19) }}
            </td>
            <td style="font-size: 11px">{{ c.trigger_kind }}</td>
            <td class="mono" style="font-size: 11px">{{ c.subject_id }}</td>
            <td style="font-size: 12px">{{ c.decision || '—' }}</td>
            <td>
              <span class="tag" :class="c.status === 'ok' ? 't-pass' : 't-hit'">
                {{ c.status }}</span>
            </td>
            <td class="mono muted" style="font-size: 11px">{{ c.rule_set_version }}</td>
            <td class="mono muted" style="font-size: 10px">
              {{ c.phase_timings_ms ? JSON.stringify(c.phase_timings_ms) : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <!-- ④ 运行 ---------------------------------------------------------------->
  <template v-if="tab === 'run'">
    <div class="panel">
      <h3>四条链</h3>
      <div class="sub">{{ chainNote }}</div>
    </div>
    <div v-for="c in chains" :key="c.chain_id" class="panel">
      <div class="row" style="margin: 0 0 6px">
        <h3 style="margin: 0">{{ c.chain_name }}</h3>
        <span class="tag" :class="c.kind === '常驻编排' ? 't-pass' : 't-na'">{{ c.kind }}</span>
        <span v-if="c.status" class="tag" :class="c.status === 'running' ? 't-pend'
          : c.status === 'failed' ? 't-hit' : 't-na'">
          {{ ST_CN[c.status] || c.status }}
        </span>
        <span v-if="c.pct !== undefined && c.status === 'running'" class="muted">
          {{ c.pct }}%
        </span>
        <a class="mini dl" :href="c.page">去那一栏 →</a>
        <button v-if="c.kind !== '常驻编排'" class="mini" style="margin-left: auto"
                :disabled="!!busy || c.status === 'running'" @click="startChain(c)">
          {{ c.status === 'running' ? '运行中…' : '在这里启动' }}
        </button>
        <span v-else class="muted" style="margin-left: auto; font-size: 12px">
          常驻,本页不启停
        </span>
      </div>
      <div class="note" style="margin-top: 0"><MarkText :text="c.note" /></div>
      <div class="row" style="flex-wrap: wrap; margin: 6px 0 0">
        <span v-for="(a, i) in c.agents" :key="a.agent_id" class="step">
          <span v-if="i" class="arrow">→</span>
          <code class="chip" @click="openAgent(a.agent_id)">
            <span class="muted">{{ a.layer }}</span> {{ a.agent_name }}
          </code>
        </span>
      </div>
      <div class="muted" style="font-size: 12px; margin-top: 6px">
        <span v-if="c.signals !== undefined">台账 {{ c.signals }} 条信号</span>
        <span v-if="c.signals_in_job !== undefined">
          （本次作业 {{ c.signals_in_job }} 条）</span>
        <span v-if="c.cycles !== undefined"> · 周期 {{ c.cycles }} 次</span>
        <span v-if="c.elapsed"> · 上次用时 {{ c.elapsed }} 秒</span>
        <span v-if="c.job_id"> · 作业 {{ c.job_id }}</span>
      </div>
      <div v-if="c.error" class="err" style="margin-top: 6px">{{ c.error }}</div>
      <div v-if="c.log_errors && c.log_errors.length" class="err" style="margin-top: 6px">
        <b>信号落库失败 {{ c.log_errors.length }} 条</b>（链照常跑完,但台账缺了行）:
        <div v-for="(e, i) in c.log_errors" :key="i" style="font-size: 12px">
          {{ e.from }} → {{ e.channel }}:{{ e.error }}
        </div>
      </div>
    </div>
  </template>

  <!-- ⑤ 存储 ---------------------------------------------------------------->
  <template v-if="tab === 'store'">
    <div v-if="store" class="panel">
      <h3>存储</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 180px">表 / 文件</th><th class="num" style="width: 54px">行数</th>
            <th style="width: 220px">存的是什么</th><th style="width: 150px">谁写的</th>
            <th style="width: 130px">重算时</th><th>为什么这么存</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in store.items" :key="t.table_name">
            <td class="mono" style="font-size: 12px">{{ t.table_name }}</td>
            <td class="num">{{ t.rows ?? '—' }}</td>
            <td style="font-size: 12px">{{ t.what }}</td>
            <td class="muted" style="font-size: 12px">{{ t.written_by }}</td>
            <td style="font-size: 12px">{{ t.on_recalc }}</td>
            <td class="muted" style="font-size: 12px"><MarkText :text="t.why" /></td>
          </tr>
        </tbody>
      </table>
      <div class="note"><MarkText :text="store.note" /></div>
    </div>
  </template>
</template>

<style scoped>
button.on { background: var(--brand); color: #fff; }
button.mini { padding: 2px 7px; font-size: 12px; }
a.mini.dl {
  display: inline-block; padding: 2px 7px; font-size: 12px;
  border: 1px solid var(--line); border-radius: 4px; text-decoration: none;
}
tr.clickable { cursor: pointer; }
tr.clickable:hover td { background: #f7fafd; }
tr.on td { background: #fffdf5; }
td.warn { color: var(--hit); }
code.chip {
  display: inline-block; padding: 1px 6px; margin: 2px 4px 2px 0; font-size: 11px;
  background: #f3f6fa; border: 1px solid var(--line); border-radius: 3px; cursor: pointer;
}
.sdafi .blk {
  border-left: 3px solid var(--line); padding: 2px 0 8px 9px; margin-bottom: 10px;
}
.sdafi .blk > b { font-size: 14px; }
.pre {
  margin: 4px 0 0; padding: 6px 8px; background: #fafbfc; border: 1px solid var(--line);
  border-radius: 4px; font-size: 11px; line-height: 1.55; white-space: pre-wrap;
}
.yaml {
  max-height: 440px; overflow: auto; background: #fafbfc; border: 1px solid var(--line);
  border-radius: 4px; padding: 8px; font-size: 11px; line-height: 1.5; white-space: pre;
}
.step { display: inline-flex; align-items: center; }
.arrow { color: var(--muted); margin: 0 4px; }
.ok-note {
  background: #f1f8f2; border: 1px solid var(--pass); border-radius: var(--radius);
  padding: 7px 9px; margin-bottom: 10px; font-size: 13px;
}
</style>
