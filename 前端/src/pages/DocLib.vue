<script setup lang="ts">
/**
 * 栏目一 · 制度文件库。规则的唯一上游。
 *
 * 五个页签对应五件不同的事，**刻意不合并**：
 *   文件来源   上传制度文件；或纳入服务器上已有的目录；扫描
 *   文件清单   库里有哪些文件、每份能不能识别、识别不了的原因、删托管文件
 *   上传台账   谁什么时候传了什么、**哪些被拒了以及为什么**、托管区占了多少盘
 *   变化台账   历次扫描的增删改历史（第二次扫描的价值全在这里）
 *   读图       OCR 丢结构的那几页，用多模态模型再读一遍并互相印证
 *
 * 2026-10-21 文件进库改成**以客户端上传为主**（迁移 0034，用户决策「双模式并存」）：
 *   托管目录   客户端上传 → 服务器 var/doclib/{领域id}/{实例id}/，可写可删
 *   引用目录   服务器上已有的目录 → 原地只读扫描，一个字节都不写
 * 原来只有「浏览服务器本机目录」这一条路，而浏览器和服务不在同一台机器上时
 * 那里根本选不到使用者自己的文件——客户给制度文件的常态恰恰是「我这边有一堆 PDF」。
 * 所以上传是默认入口，FolderPicker 退为折叠起来的补充手段。
 *
 * 2026-10-21 删掉了第五个页签「已入库制度文件」（原 policy_doc 链路）。
 * 它读的是 /api/asset/docs，和本页其余四个页签的「只读扫描目录」口径不同，
 * 两套口径摆在同一页上分不清哪份文件算库里的。接口与数据都还在，只是不再出现在这一页。
 * 另外它当时挂的是 `v-else` 兜底分支，而「读图」是独立的 `v-if`——
 * 于是切到读图页签时这张表会跟着渲染一遍。删掉同时修掉了这个串页。
 *
 * 两条口径必须在界面上写死，否则一定被读错：
 *
 * 1. **「只读」是这一栏的地基。** 本系统绝不向被扫描的目录写入任何东西，
 *    「移出文件库」只删登记。客户的资料目录是证据，不是工作区。
 *
 * 2. **「需OCR」不等于「不能识别」。** 前者是 PDF 没有文字层、本机装了
 *    tesseract、下一步办得到；后者（「未解析」）是格式本身取不到、要换路子。
 *    合成一个状态，没人知道该去跑 OCR 还是该去转格式。
 *    而把任何一种记成「正文为空」最坏——下游会以为制度里没写过那件事。
 *
 * 3. **本栏的数字按当前实例过滤，而抽取/本体/发布那三栏是全库数字。**
 *    这条边界必须显示出来（stats.scope_note）：让人以为全站都过滤过，
 *    等于埋一个将来说不清的坑。见迁移 0034 的「过滤边界」。
 */
import { computed, onMounted, ref } from 'vue'
import Async from '../components/Async.vue'
import FolderPicker from '../components/FolderPicker.vue'
import Uploader from '../components/Uploader.vue'
import { api } from '../api'
import type {
  DlChange, DlFile, DlRoot, DlScanResult, DlStats, DlStorage, DlUploadItem,
  DlUploadResult, VisionCross, VisionPage, VisionRead,
} from '../api'

type Tab = 'roots' | 'files' | 'uploads' | 'changes' | 'vision'
const tab = ref<Tab>('roots')

const roots = ref<DlRoot[]>([])
const stats = ref<DlStats | null>(null)
const files = ref<DlFile[]>([])
const changes = ref<DlChange[]>([])
const changeScan = ref<Record<string, unknown> | null>(null)
const lastScan = ref<DlScanResult | null>(null)
const opened = ref<(DlFile & { preview: string; truncated: boolean }) | null>(null)

// ── 读图 ────────────────────────────────────────────────────────────────
const vpages = ref<VisionPage[]>([])
const vreads = ref<VisionRead[]>([])
const vcross = ref<VisionCross | null>(null)
const vopen = ref('')
const vsum = ref<{ totals: Record<string, number>; note: string } | null>(null)
const vwho = ref('')
const vmsg = ref('')

// ── 上传与托管区 ────────────────────────────────────────────────────────
const storage = ref<DlStorage | null>(null)
const ledger = ref<(DlUploadItem & { batch_id: string; uploaded_at: string
                                     uploaded_by: string | null
                                     parse_state: string | null
                                     text_chars: number | null
                                     missing: boolean | null })[]>([])
const ledgerBy = ref<{ result: string; n: number }[]>([])
const onlyRejected = ref(false)
const scopeNote = ref('')

const picking = ref(false)
const busy = ref('')
const err = ref('')
const msg = ref('')
const pendingRemove = ref('')
const pendingDel = ref('')

const fq = ref('')
const fext = ref('')
const fstate = ref('')
const fkind = ref('')
const forigin = ref('')

/** 解析状态 → 标签样式。六态各有各的颜色，合并就等于合并了处置动作。 */
const STATE_CLS: Record<string, string> = {
  已解析: 't-pass',
  需OCR: 't-pend',
  未解析: 't-na',
  空文件: 't-na',
  解析失败: 't-hit',
  未扫描: 't-na',
}
/** 文件类别。单据和范本不是制度——混在一张清单里看不出来，
 *  下游「制度里还有多少条没变成规则」的数字就会把它们一起算进去。 */
const KIND_CLS: Record<string, string> = {
  制度: 't-pass', 范本: 't-na', 单据: 't-demo', 其他: 't-na',
}
const CHANGE_CLS: Record<string, string> = {
  新增: 't-pass', 修改: 't-pend', 消失: 't-hit',
}
/** 根的类型。**托管可写、引用只读**，这是只读纪律的边界，必须一眼可辨。 */
const KIND_ROOT_CLS: Record<string, string> = { 托管: 't-pass', 引用: 't-na' }
/** 上传结果七态。「同名另存」与「同内容已存在」是相反的两件事，颜色也要分开。 */
const RESULT_CLS: Record<string, string> = {
  已入库: 't-pass', 同名另存: 't-demo', 同内容已存在: 't-na',
  扩展名不支持: 't-hit', 超过大小上限: 't-hit',
  文件名不合法: 't-hit', 写盘失败: 't-hit',
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

const loadRoots = () =>
  run('roots', async () => {
    const [r, s, st] = await Promise.all([api.dlRoots(), api.dlStats(), api.dlStorage()])
    roots.value = r.items
    stats.value = s
    storage.value = st
    scopeNote.value = s.scope_note || ''
  })


const loadFiles = () =>
  run('files', async () => {
    files.value = (await api.dlFiles({
      // 按「能否识别」筛，不按 parse_state：后者是磁盘事实，OCR 跑完也还是「需OCR」，
      // 让人对着一个写着「需OCR」的下拉去找已经 OCR 过的文件，是界面在骗人。
      q: fq.value, ext: fext.value, recognize: fstate.value, kind: fkind.value,
      origin: forigin.value, limit: 1000,
    })).items
  })

const loadUploads = () =>
  run('uploads', async () => {
    const [r, st] = await Promise.all([
      api.dlUploads({ limit: 500, only_rejected: onlyRejected.value || undefined }),
      api.dlStorage(),
    ])
    ledger.value = r.items
    ledgerBy.value = r.by_result
    storage.value = st
  })

/** 上传完刷新三处：根（文件数变了）、清单、台账。
 *  只刷一处会让另外两处显示旧数字，而使用者不知道哪个是真的。 */
const afterUpload = (r: DlUploadResult) =>
  run('after-upload', async () => {
    msg.value = `上传完成：进库 ${r.ok_count} 份`
      + (r.rejected.length ? `，未入库 ${r.rejected.length} 份（见「上传台账」）` : '')
    await loadRoots()
    await loadFiles()
    if (tab.value === 'uploads') await loadUploads()
  })

/** 删一份托管文件。**引用目录的删不了**，后端回 400；
 *  已产出条款/规则明细的回 409，理由里带着挂了多少条。 */
const delFile = (f: DlFile) =>
  run(f.file_id + ':del', async () => {
    const d = await api.dlDeleteFile(f.file_id, 'ui')
    pendingDel.value = ''
    msg.value = `已删除「${d.file_name}」。`
      + (d.removed_from_disk ? '磁盘文件已删，' : '磁盘上本来就没有这个文件，')
      + '登记行按「消失」保留——删登记会连带级联清空 OCR、读图、条款、规则明细。'
    await loadRoots()
    await loadFiles()
  })

const loadChanges = () =>
  run('changes', async () => {
    const r = await api.dlChanges({ limit: 1000 })
    changes.value = r.items
    changeScan.value = r.scan || null
  })

const addRoots = (paths: string[]) =>
  run('add', async () => {
    const r = await api.dlAddRoots(paths)
    picking.value = false
    msg.value = `加入 ${r.ok_count} 个文件夹`
      + (r.failed.length ? `，${r.failed.length} 个未加入：${r.failed[0].reason}` : '')
    await loadRoots()
    if (r.ok_count) await scan(false)
  })

const toggleRoot = (r: DlRoot) =>
  run(r.root_id + ':toggle', async () => {
    await api.dlSetRoot(r.root_id, { enabled: !r.enabled })
    await loadRoots()
  })

const removeRoot = (r: DlRoot) =>
  run(r.root_id + ':del', async () => {
    const d = await api.dlRemoveRoot(r.root_id)
    pendingRemove.value = ''
    msg.value = `已移出文件库（删登记 ${d.files_unregistered} 条，磁盘文件未做任何改动）`
    await loadRoots()
    await loadFiles()
  })

const scan = (force: boolean) =>
  run(force ? 'rescan' : 'scan', async () => {
    const r = await api.dlScan({ force })
    lastScan.value = r
    msg.value = `扫描完成：新增 ${r.added}、修改 ${r.updated}、未变 ${r.unchanged}、消失 ${r.missing}`
    await loadRoots()
    await loadFiles()
    await loadChanges()
  })

const open = (f: DlFile) =>
  run(f.file_id + ':open', async () => {
    opened.value = await api.dlFile(f.file_id)
  })

function pickTab(t: Tab) {
  tab.value = t
  if (t === 'files' && !files.value.length) loadFiles()
  if (t === 'uploads' && !ledger.value.length) loadUploads()
  if (t === 'changes' && !changes.value.length) loadChanges()
  if (t === 'vision' && !vpages.value.length) loadVision()
}

const loadVision = () =>
  run('v', async () => {
    const [p, s2] = await Promise.all([api.visionPages(60), api.visionSummary()])
    vpages.value = p.items
    vsum.value = { totals: s2.totals as unknown as Record<string, number>, note: s2.note }
  })

const openPage = (p: VisionPage) =>
  run('vp', async () => {
    const key = p.file_id + ':' + p.page_no
    vopen.value = vopen.value === key ? '' : key
    vreads.value = []
    vcross.value = null
    if (!vopen.value) return
    const d = await api.visionPage(p.file_id, p.page_no)
    vreads.value = d.reads
    vcross.value = d.cross_check
  })

/** **这一下会出网、会计费。** 按钮文案里必须写清楚。 */
const readPage = (p: VisionPage) =>
  run('read', async () => {
    const r = await api.visionRead({
      file_id: p.file_id, page_no: p.page_no,
      kind: p.has_table ? 'table' : 'text',
    })
    vmsg.value = r.ok
      ? `${r.model_name} 读完：${r.rows ?? 0} 行、看不清 ${r.unreadable ?? 0} 格、`
        + `${r.latency_ms}ms、token ${r.tokens.prompt_tokens}/${r.tokens.completion_tokens}`
      : `**失败** ${r.err_kind}：${(r.message || '').slice(0, 120)}`
    await loadVision()
    if (vopen.value === p.file_id + ':' + p.page_no) {
      const d = await api.visionPage(p.file_id, p.page_no)
      vreads.value = d.reads
      vcross.value = d.cross_check
    }
  })

const unreadable = computed(() =>
  (stats.value?.by_state || []).filter((s) => s.state !== '已解析')
    .reduce((a, b) => a + b.n, 0))
const needOcr = computed(() =>
  (stats.value?.by_state || []).find((s) => s.state === '需OCR')?.n || 0)

const byOrigin = computed(() => {
  const m: Record<string, number> = { 上传: 0, 扫描: 0 }
  for (const o of stats.value?.by_origin || []) m[o.origin] = o.n
  return m
})
/** 托管根与引用根分开列：**两者的权限与可靠性都不一样**。
 *  托管的在我们自己手里；引用的明天可能被客户改了或移走了（只能标消失）。 */
const managedRoots = computed(() => roots.value.filter((r) => r.kind === '托管'))
const refRoots = computed(() => roots.value.filter((r) => r.kind === '引用'))

function kb(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}
function t(s: string | null | undefined) {
  return s ? String(s).replace('T', ' ').slice(0, 19) : '—'
}

onMounted(loadRoots)
</script>

<template>
  <h2>制度文件库</h2>

  <div v-if="stats" class="panel">
    <div class="stat">
      <div><b>{{ stats.files }}</b><span>在库文件</span></div>
      <div><b>{{ byOrigin['上传'] }}</b><span>客户端上传</span></div>
      <div><b>{{ byOrigin['扫描'] }}</b><span>引用目录扫描</span></div>
      <div><b>{{ stats.readable }}</b><span>已取到正文</span></div>
      <div><b>{{ needOcr }}</b><span>需 OCR</span></div>
      <div><b>{{ unreadable - needOcr }}</b><span>取不到正文</span></div>
      <div><b>{{ kb(stats.bytes) }}</b><span>总体积</span></div>
      <div><b>{{ stats.missing }}</b><span>已消失</span></div>
    </div>
    <!-- 过滤边界必须写在页面上。只写在后端的 note 里，看页面的人会以为
         全站都按实例过滤过——那是迁移 0034 刻意留下的边界，不是漏做。 -->
    <div v-if="stats.instance_label" class="note" style="margin-bottom: 0">
      以上数字是 <b>{{ stats.domain_label }} / {{ stats.instance_label }}</b> 的。
      <MarkText :text="scopeNote" />
    </div>
  </div>

  <div class="row">
    <button :class="{ on: tab === 'roots' }" @click="pickTab('roots')">文件来源</button>
    <button :class="{ on: tab === 'files' }" @click="pickTab('files')">文件清单</button>
    <button :class="{ on: tab === 'uploads' }" @click="pickTab('uploads')">上传台账</button>
    <button :class="{ on: tab === 'changes' }" @click="pickTab('changes')">变化台账</button>
    <button :class="{ on: tab === 'vision' }" @click="pickTab('vision')">读图</button>
    <span v-if="busy" class="muted">{{ busy }} 处理中…</span>
  </div>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="note">{{ msg }}</div>

  <!-- ── 文件来源 ───────────────────────────────────────────────────────── -->
  <template v-if="tab === 'roots'">
    <!-- 上传放在最前：它是默认入口。浏览器和服务不在同一台机器上时，
         下面那个「纳入服务器已有目录」根本选不到使用者自己的文件。 -->
    <div class="panel">
      <h3>上传制度文件</h3>
      <Uploader :storage="storage" @done="afterUpload" />
    </div>

    <div v-if="managedRoots.length" class="panel">
      <h3>托管目录（上传落在这里，可写可删）</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 56px">启用</th><th style="width: 180px">名称</th>
            <th>服务器路径</th><th class="num" style="width: 70px">文件数</th>
            <th class="num" style="width: 80px">占用</th>
            <th style="width: 150px">上次扫描</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in managedRoots" :key="r.root_id">
            <td><input type="checkbox" :checked="r.enabled" @change="toggleRoot(r)" /></td>
            <td>
              {{ r.label }}
              <span class="tag" :class="KIND_ROOT_CLS[r.kind]" style="margin-left: 5px">
                {{ r.kind }}
              </span>
            </td>
            <td>
              <code class="mono">{{ r.abs_path }}</code>
              <div v-if="!r.exists" class="tag t-hit" style="margin-top: 3px">
                目录已不存在（下一次上传会自动重建）
              </div>
            </td>
            <td class="num">{{ r.files }}</td>
            <td class="num">
              {{ (storage?.dirs || []).find(d => d.root_id === r.root_id)?.size_text || '—' }}
            </td>
            <td class="mono" style="font-size: 11px">{{ t(r.last_scan_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div class="note" style="margin-bottom: 0">
        托管目录是<b>本系统自己的工作区</b>，所以允许写入与删除；
        而下面的引用目录是<b>客户的资料目录</b>，本系统对它只读，一个字节都不写。
        托管目录由系统按当前实例自动创建，<b>不提供整体移出</b>——
        整体移出会把条款、OCR、读图结果、规则明细一起级联删掉，
        而磁盘上的文件会留下来变成没人认领的孤儿。要清空请在「文件清单」里逐份删。
      </div>
    </div>

    <details class="panel" :open="picking || refRoots.length > 0">
      <summary>
        纳入服务器上已有的目录（引用，原地只读扫描）· 已纳入 {{ refRoots.length }} 个
      </summary>

      <div class="note">
        <b>这不是上传。</b>它要求路径在<b>服务器</b>上存在，适合「资料已经在服务器上了」
        的情况。如果浏览器和服务不在同一台机器上，这里选不到你本机的文件，请用上面的上传。
        纳入只登记路径，<b>不复制、不移动、不修改磁盘上的任何文件</b>。
      </div>

      <FolderPicker v-if="picking" @add="addRoots" @close="picking = false" />

      <table v-if="refRoots.length">
        <thead>
          <tr>
            <th style="width: 56px">启用</th><th style="width: 160px">名称</th>
            <th>路径</th><th class="num" style="width: 70px">文件数</th>
            <th style="width: 150px">上次扫描</th><th style="width: 130px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in refRoots" :key="r.root_id">
            <td><input type="checkbox" :checked="r.enabled" @change="toggleRoot(r)" /></td>
            <td>
              {{ r.label }}
              <span class="tag" :class="KIND_ROOT_CLS[r.kind]" style="margin-left: 5px">
                {{ r.kind }}
              </span>
            </td>
            <td>
              <code class="mono">{{ r.abs_path }}</code>
              <div v-if="!r.exists" class="tag t-hit" style="margin-top: 3px">
                文件夹已不存在
              </div>
              <div v-if="r.note" class="muted" style="font-size: 11px"><MarkText :text="r.note" /></div>
            </td>
            <td class="num">{{ r.files }}</td>
            <td class="mono" style="font-size: 11px">{{ t(r.last_scan_at) || '未扫描' }}</td>
            <td>
              <button v-if="pendingRemove !== r.root_id" :disabled="!!busy"
                      @click="pendingRemove = r.root_id">移出</button>
              <template v-else>
                <button :disabled="!!busy" @click="removeRoot(r)">确认移出</button>
                <button :disabled="!!busy" @click="pendingRemove = ''">取消</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted">尚未纳入任何服务器目录</div>

      <div class="row" style="margin-top: 12px">
        <button @click="picking = !picking">
          {{ picking ? '收起选择器' : '＋ 浏览服务器目录' }}
        </button>
      </div>
    </details>

    <div class="panel">
      <h3>扫描</h3>
      <div class="note">
        上传之后会<b>自动扫描一次</b>，不用手点。这里的按钮给两种情况：
        引用目录里的文件被客户改过了；或者改过解析器要全量重跑（强制重扫）。
        不指定范围时<b>只扫当前实例的根</b>。
      </div>
      <div class="row" style="margin-bottom: 0">
        <button :disabled="!!busy || !roots.length" @click="scan(false)">
          {{ busy === 'scan' ? '扫描中…' : '扫描' }}
        </button>
        <button :disabled="!!busy || !roots.length" @click="scan(true)">
          {{ busy === 'rescan' ? '重扫中…' : '强制重扫' }}
        </button>
      </div>

      <div v-if="lastScan" class="note" style="margin-top: 10px">
        <b>本次扫描 {{ t(lastScan.scanned_at) }}（{{ lastScan.mode }}）：</b>
        新增 <b>{{ lastScan.added }}</b>、修改 <b>{{ lastScan.updated }}</b>、
        未变 {{ lastScan.unchanged }}、消失 <b>{{ lastScan.missing }}</b>
        <div v-for="w in lastScan.warnings" :key="w" class="err" style="margin-top: 4px">{{ w }}</div>
        <table v-if="lastScan.changes.length" style="margin-top: 8px">
          <thead><tr><th style="width: 70px">变化</th><th>文件</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in lastScan.changes.slice(0, 50)" :key="c.file_id + c.change">
              <td><span class="tag" :class="CHANGE_CLS[c.change]">{{ c.change }}</span></td>
              <td>
                {{ c.file_name }}
                <div class="muted mono" style="font-size: 11px">{{ c.rel_path }}</div>
              </td>
              <td class="muted"><MarkText :text="c.detail" /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </template>

  <!-- ── 文件清单 ───────────────────────────────────────────────────────── -->
  <template v-else-if="tab === 'files'">
    <div class="panel">
      <h3>文件清单（{{ files.length }} 份）</h3>
      <div class="row">
        <input v-model="fq" placeholder="按文件名筛选" style="flex: 1; min-width: 160px"
               @keyup.enter="loadFiles" />
        <select v-model="fext" @change="loadFiles">
          <option value="">全部类型</option>
          <option v-for="e in stats?.by_ext || []" :key="e.ext" :value="e.ext">
            {{ e.ext }}（{{ e.n }}）
          </option>
        </select>
        <select v-model="fstate" @change="loadFiles">
          <option value="">全部识别状态</option>
          <option v-for="s in stats?.by_recognize || []" :key="s.label" :value="s.label">
            {{ s.label }}（{{ s.n }}）
          </option>
        </select>
        <select v-model="fkind" @change="loadFiles">
          <option value="">全部类别</option>
          <option v-for="k in stats?.by_kind || []" :key="k.kind" :value="k.kind">
            {{ k.kind }}（{{ k.n }}）
          </option>
        </select>
        <select v-model="forigin" @change="loadFiles">
          <option value="">全部来源</option>
          <option v-for="o in stats?.by_origin || []" :key="o.origin" :value="o.origin">
            {{ o.origin }}（{{ o.n }}）
          </option>
        </select>
        <button @click="loadFiles">筛选</button>
      </div>

      <table v-if="files.length">
        <thead>
          <tr>
            <th style="width: 40px">#</th><th>文件</th>
            <th style="width: 58px">类别</th>
            <th style="width: 72px">来源</th>
            <th style="width: 160px">文号 / 标题</th>
            <th style="width: 50px">格式</th><th class="num" style="width: 72px">大小</th>
            <th style="width: 130px">能否识别</th><th>备注</th>
            <th style="width: 120px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(f, i) in files" :key="f.file_id">
            <td class="num">{{ i + 1 }}</td>
            <td>
              <a href="#" @click.prevent="open(f)">{{ f.file_name }}</a>
              <div class="muted mono" style="font-size: 11px">{{ f.rel_path }}</div>
              <!-- 同名的另一个版本：同名不同内容时上传不覆盖，另存为 xxx(2).pdf。
                   不标出来，使用者会以为自己传的新版本把旧的替换掉了。 -->
              <div v-if="f.name_copies > 1" class="tag t-demo" style="margin-top: 3px">
                同名另有 {{ f.name_copies - 1 }} 个版本
              </div>
            </td>
            <td>
              <span class="tag" :class="KIND_CLS[f.doc_kind || '其他'] || 't-na'">
                {{ f.doc_kind || '其他' }}
              </span>
            </td>
            <td>
              <span class="tag" :class="f.origin === '上传' ? 't-pass' : 't-na'">
                {{ f.origin }}
              </span>
              <div v-if="f.uploaded_by" class="muted" style="font-size: 11px">
                {{ f.uploaded_by }}
              </div>
            </td>
            <td>
              <div v-if="f.doc_number" class="mono" style="font-size: 11px">
                {{ f.doc_number }}
              </div>
              <div v-if="f.doc_title" style="font-size: 11px">《{{ f.doc_title }}》</div>
              <span v-if="!f.doc_number && !f.doc_title" class="muted">—</span>
            </td>
            <td class="mono">{{ f.ext }}</td>
            <td class="num">{{ kb(f.file_size) }}</td>
            <td>
              <span class="tag" :class="'t-' + (f.recognize?.cls || 'na')">
                {{ f.recognize?.label || f.parse_state }}
              </span>
              <div class="muted" style="font-size: 11px; margin-top: 2px">
                {{ f.recognize?.how }}
              </div>
            </td>
            <td class="muted" style="font-size: 12px"><MarkText :text="f.recognize?.note || f.parse_reason || ''" /></td>
            <td>
              <!-- 删除只对托管文件开放。引用目录里的文件是客户的资料，
                   本系统对它只读——按钮直接不给，而不是点了再报错。 -->
              <template v-if="f.deletable">
                <button v-if="pendingDel !== f.file_id" :disabled="!!busy"
                        @click="pendingDel = f.file_id">删除</button>
                <template v-else>
                  <button :disabled="!!busy" @click="delFile(f)">确认删除</button>
                  <button :disabled="!!busy" @click="pendingDel = ''">取消</button>
                </template>
              </template>
              <span v-else class="muted" style="font-size: 11px"
                    title="引用目录是客户的资料目录，本系统对它只读">
                引用 · 只读
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted">没有符合条件的文件</div>

      <div class="note" style="margin-bottom: 0">
        <b>删除</b>只对托管目录里的文件开放，而且<b>已产出条款、规则明细或已签字读图的会被拒</b>——
        删掉原文之后那些条款会指向一份不存在的文件，而页面上它们看着仍然有出处，
        这种断链基本查不出来。删除成功时<b>磁盘文件删掉、登记行按「消失」保留</b>：
        删登记行会连带级联清空 OCR、读图、条款、规则明细，
        而「这份资料曾经在库里」是溯源的一部分，只能作废不能蒸发。
      </div>
    </div>

    <div v-if="opened" class="panel">
      <h3>{{ opened.file_name }}</h3>
      <div class="kv">
        <b>路径</b><span class="mono">{{ opened.abs_path }}</span>
        <b>解析状态</b>
        <span>
          <span class="tag" :class="STATE_CLS[opened.parse_state] || 't-na'">
            {{ opened.parse_state }}</span>
          <span class="muted" style="margin-left: 8px"><MarkText :text="opened.parse_reason" /></span>
        </span>
        <b>正文字数</b><span>{{ opened.text_chars }}{{ opened.truncated ? '（预览已截断）' : '' }}</span>
      </div>
    </div>
  </template>

  <!-- ── 上传台账 ───────────────────────────────────────────────────────── -->
  <!-- 这一页存在的理由只有一个：**客户传了 20 份而库里只多 17 份时，
       差的 3 份去哪了必须答得上来。** 所以被拒的文件也在台账里留行，
       而「成功 17 份」那种总数是答不了这个问题的。 -->
  <template v-else-if="tab === 'uploads'">
    <div v-if="storage" class="panel">
      <h3>托管区占用</h3>
      <div class="stat">
        <div><b>{{ storage.total_files }}</b><span>托管文件</span></div>
        <div><b>{{ kb(storage.total_bytes) }}</b><span>占用磁盘</span></div>
        <div><b>{{ storage.limits.max_upload_bytes_text }}</b><span>单份上限</span></div>
        <div><b>{{ storage.limits.max_upload_files }}</b><span>单次份数上限</span></div>
      </div>
      <div class="kv" style="margin-top: 12px">
        <b>托管区根目录</b><span class="mono">{{ storage.root_dir }}</span>
        <b>允许的扩展名</b><span class="mono">{{ storage.limits.allowed_ext.join(' ') }}</span>
      </div>
      <table v-if="storage.dirs.length" style="margin-top: 12px">
        <thead>
          <tr>
            <th style="width: 200px">归属</th><th>目录</th>
            <th class="num" style="width: 70px">文件</th>
            <th class="num" style="width: 80px">占用</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in storage.dirs" :key="d.root_id">
            <td>{{ d.domain_label }} / {{ d.instance_label }}</td>
            <td>
              <code class="mono">{{ d.abs_path }}</code>
              <div v-if="!d.exists" class="tag t-hit" style="margin-top: 3px">目录已不存在</div>
            </td>
            <td class="num">{{ d.files }}</td>
            <td class="num">{{ d.size_text }}</td>
          </tr>
        </tbody>
      </table>
      <!-- 暂存区本该是空的。非空说明有一次上传崩在中途，必须报出来，
           不能悄悄留着占磁盘——那些文件没有任何登记指向它们。 -->
      <div v-if="storage.staging_stale.length" class="err">
        <b>暂存区有 {{ storage.staging_stale.length }} 个残留文件</b>（{{ storage.staging_dir }}）：
        每份上传要么移入托管目录、要么当场删掉，残留说明有一次上传崩在中途。
        这些文件没有任何登记指向它们，可以直接删。
      </div>
      <div v-else class="note" style="margin-bottom: 0"><MarkText :text="storage.note" /></div>
    </div>

    <div class="panel">
      <h3>上传台账（{{ ledger.length }} 行）</h3>
      <div class="row">
        <label>
          <input v-model="onlyRejected" type="checkbox" @change="loadUploads" />
          只看未入库的
        </label>
        <button :disabled="!!busy" @click="loadUploads">刷新</button>
        <span v-for="b in ledgerBy" :key="b.result" class="tag"
              :class="RESULT_CLS[b.result] || 't-na'">
          {{ b.result }} {{ b.n }}
        </span>
      </div>
      <table v-if="ledger.length">
        <thead>
          <tr>
            <th style="width: 150px">时间</th><th>文件</th>
            <th style="width: 100px">结果</th>
            <th class="num" style="width: 72px">大小</th>
            <th style="width: 80px">上传人</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(u, i) in ledger" :key="u.batch_id + i">
            <td class="mono" style="font-size: 11px">{{ t(u.uploaded_at) }}</td>
            <td>
              <div class="mono">{{ u.rel_path || u.file_name }}</div>
              <div v-if="u.stored_name && u.stored_name !== u.file_name"
                   class="muted" style="font-size: 11px">
                落盘名 → <b>{{ u.stored_name }}</b>
              </div>
            </td>
            <td>
              <span class="tag" :class="RESULT_CLS[u.result] || 't-na'">{{ u.result }}</span>
              <div v-if="u.missing" class="muted" style="font-size: 11px">已删除</div>
            </td>
            <td class="num">{{ u.file_size == null ? '—' : kb(u.file_size) }}</td>
            <td>{{ u.uploaded_by || '—' }}</td>
            <td class="muted" style="font-size: 12px; max-width: 380px">
              <MarkText :text="u.reason || '—'" />
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted">还没有上传过文件</div>
      <div class="note" style="margin-bottom: 0">
        <b>「同名另存」和「同内容已存在」是相反的两件事。</b>
        前者=文件名一样但内容不同，已按新版本落盘（制度换了一版，旧的没被覆盖）；
        后者=字节完全相同，什么都没落盘（重复点了上传）。
        合成一个「重复」，使用者就不知道自己刚传的新版本到底进没进库。
      </div>
    </div>
  </template>

  <!-- ── 变化台账 ───────────────────────────────────────────────────────── -->
  <template v-else-if="tab === 'changes'">
    <div class="panel">
      <h3>变化台账</h3>
      <div v-if="changeScan" class="note">
        最近一次扫描 <code class="mono">{{ changeScan.scan_id }}</code>
        ｜{{ t(changeScan.started_at as string) }}｜{{ changeScan.mode }}
        ｜新增 {{ changeScan.added }}·修改 {{ changeScan.updated }}·消失 {{ changeScan.missing }}
      </div>
      <table v-if="changes.length">
        <thead>
          <tr><th style="width: 70px">变化</th><th>文件</th><th>说明</th>
            <th style="width: 150px">记录时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in changes" :key="c.id">
            <td><span class="tag" :class="CHANGE_CLS[c.change]">{{ c.change }}</span></td>
            <td>
              {{ c.file_name }}
              <div class="muted mono" style="font-size: 11px">{{ c.rel_path }}</div>
            </td>
            <td class="muted"><MarkText :text="c.detail" /></td>
            <td class="mono" style="font-size: 11px">{{ t(c.noted_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="muted">最近一次扫描没有变化</div>
    </div>
  </template>

  <!-- 读图 -------------------------------------------------------------->
  <template v-if="tab === 'vision'">
    <div class="panel">
      <h3>多模态读图</h3>
      <div v-if="vsum" class="kv">
        <b>已调用</b>
        <span>
          {{ vsum.totals.calls }} 次（失败 {{ vsum.totals.failed }}）·
          {{ vsum.totals.models }} 个型号 ·
          入 {{ vsum.totals.pt }} token / 出 {{ vsum.totals.ct }} token
        </span>
        <b>口径</b><span style="font-size: 12px"><MarkText :text="vsum.note" /></span>
      </div>
      <div class="row">
        <input v-model="vwho" placeholder="签字人署名" style="width: 150px" />
      </div>
      <div v-if="vmsg" class="ok-note">{{ vmsg }}</div>
      <table>
        <thead>
          <tr>
            <th>文件</th><th style="width: 46px">页</th>
            <th style="width: 54px">OCR 字</th><th style="width: 60px">OCR 置信</th>
            <th style="width: 54px">表格</th><th style="width: 58px">已读</th>
            <th style="width: 170px"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="p in vpages" :key="p.file_id + ':' + p.page_no">
            <tr>
              <td style="font-size: 12px">{{ p.file_name }}</td>
              <td class="num">{{ p.page_no }}</td>
              <td class="num" :class="{ warn: !p.chars }">{{ p.chars }}</td>
              <td class="num">{{ p.mean_conf ?? '—' }}</td>
              <td>
                <span v-if="p.has_table" class="tag t-pend">表格</span>
                <span v-else class="tag t-hit">没认出字</span>
              </td>
              <td class="num" :class="{ muted: !p.read_times }">
                {{ p.read_times || '—' }}
              </td>
              <td class="acts">
                <button class="mini" :disabled="!!busy" @click="readPage(p)">
                  读一页（出网）
                </button>
                <button class="mini" :disabled="!!busy" @click="openPage(p)">
                  {{ vopen === p.file_id + ':' + p.page_no ? '收起' : '看结果' }}
                </button>
              </td>
            </tr>
            <tr v-if="vopen === p.file_id + ':' + p.page_no">
              <td colspan="7" class="detailcell">
                <div v-if="vcross" class="kv">
                  <b>互相印证</b>
                  <span>
                    <span class="tag" :class="vcross.agree === true ? 't-pass'
                      : vcross.agree === false ? 't-hit' : 't-na'">
                      {{ vcross.verdict }}</span>
                    <span v-if="vcross.a" class="muted" style="margin-left: 6px">
                      {{ vcross.a.model_name }}（{{ vcross.a.numbers }} 个数字 /
                      {{ vcross.a.rows }} 行 / 看不清 {{ vcross.a.unreadable }}）
                      vs {{ vcross.b?.model_name }}（{{ vcross.b?.numbers }} /
                      {{ vcross.b?.rows }} / {{ vcross.b?.unreadable }}）
                    </span>
                  </span>
                  <b>怎么用</b><span style="font-size: 12px"><MarkText :text="vcross.note" /></span>
                  <template v-if="vcross.agree === false">
                    <b>只有前者有</b>
                    <span class="mono" style="font-size: 11px">
                      {{ (vcross.only_a || []).join('、') || '—' }}</span>
                    <b>只有后者有</b>
                    <span class="mono" style="font-size: 11px">
                      {{ (vcross.only_b || []).join('、') || '—' }}</span>
                  </template>
                </div>
                <div v-for="r in vreads" :key="r.model_name" class="readblk">
                  <div class="row" style="margin: 0 0 4px">
                    <b>{{ r.model_name }}</b>
                    <span class="muted" style="font-size: 12px">
                      第 {{ r.attempt }} 次 · {{ r.rows_parsed }} 行 ·
                      看不清 {{ r.unreadable }} 格 · {{ r.latency_ms }}ms ·
                      token {{ r.prompt_tokens }}/{{ r.completion_tokens }}
                    </span>
                  </div>
                  <pre class="vtext">{{ r.content }}</pre>
                </div>
                <div v-if="!vreads.length" class="muted">还没读过这一页。</div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <Async :load="() => api.visionStorage()">
      <template #default="{ data }">
        <div class="panel">
          <h3>读图 → 印证 → <b>人工签字</b> → 才谈得上录入</h3>
          <table>
            <thead>
              <tr>
                <th style="width: 150px">表</th><th style="width: 230px">存的是什么</th>
                <th style="width: 170px">谁写的</th><th style="width: 120px">重算时</th>
                <th>为什么这么存</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="x in (data as any).items" :key="x.table_name">
                <td class="mono" style="font-size: 12px">{{ x.table_name }}</td>
                <td style="font-size: 12px">{{ x.what }}</td>
                <td class="muted" style="font-size: 12px">{{ x.written_by }}</td>
                <td style="font-size: 12px">{{ x.on_recalc }}</td>
                <td class="muted" style="font-size: 12px"><MarkText :text="x.why" /></td>
              </tr>
            </tbody>
          </table>
          <div class="note"><MarkText :text="(data as any).note" /></div>
        </div>
      </template>
    </Async>
  </template>
</template>

<style scoped>
.readblk { border-left: 3px solid var(--line); padding: 4px 0 6px 9px; margin: 8px 0; }
.vtext {
  max-height: 320px; overflow: auto; background: #fafbfc; border: 1px solid var(--line);
  border-radius: 4px; padding: 8px; font-size: 11px; line-height: 1.55;
  white-space: pre-wrap; margin: 0;
}
.detailcell { background: #fafbfc; }
td.acts { white-space: nowrap; }
td.warn { color: var(--hit); }
.ok-note {
  background: #f1f8f2; border: 1px solid var(--pass); border-radius: var(--radius);
  padding: 7px 9px; margin-bottom: 10px; font-size: 13px;
}
button.on {
  background: var(--brand);
  color: #fff;
  border-color: var(--brand);
}
pre.body {
  max-height: 340px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 9px;
  border: 1px solid var(--line);
  background: #fafbfc;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
