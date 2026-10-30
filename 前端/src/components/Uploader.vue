<script setup lang="ts">
/**
 * 制度文件上传。拖进来、选文件、或者选整个文件夹。
 *
 * 为什么要有这个组件（和 FolderPicker 的分工）：
 *   FolderPicker 选的是**服务器上**的目录（后端扫盘，原地只读扫描）。
 *   浏览器和后端不在同一台机器上时，那里根本选不到使用者本机的文件——
 *   而客户给制度文件的常态就是「我这边有一堆 PDF」。
 *   所以上传是默认入口，FolderPicker 退为「服务器上已经有一堆资料」时的补充。
 *
 * 三条界面纪律：
 *
 * 1. **逐份报结果，不报一个总数。** 后端对每一份独立判定，有「已入库」
 *    「同名另存」「同内容已存在」「扩展名不支持」等七种结果。
 *    只显示「成功 17 份」，使用者就不知道另外 3 份去哪了——
 *    而那三份里可能有他最在意的那一版。
 *
 * 2. **「同名另存」和「同内容已存在」必须分开显示。** 这两件事相反：
 *    前者=制度换了一版，新版本进库了（旧的没覆盖）；
 *    后者=重复点了上传，什么都没变。合并成「重复」，使用者就不知道
 *    自己刚传的新版本到底进没进库。
 *
 * 3. **选好的文件在上传前可以逐个移除。** 一次拖进来几十份、发现夹带了
 *    不该传的，没有移除就只能全部取消重来。
 *
 * 不做前端的扩展名/大小预筛（只做提示）：真正的闸在后端（safe_rel +
 * MAX_UPLOAD_BYTES），前端再写一套就会和后端分叉，而分叉的表现是
 * 「前端说能传，后端拒了」或者更糟的反过来。前端只负责把后端的理由显示清楚。
 */
import { computed, ref } from 'vue'
import { api } from '../api'
import type { DlStorage, DlUploadResult } from '../api'

const props = defineProps<{ storage: DlStorage | null }>()
const emit = defineEmits<{ (e: 'done', r: DlUploadResult): void }>()

const picked = ref<File[]>([])
const subdir = ref('')
const who = ref('')
const busy = ref(false)
const err = ref('')
const result = ref<DlUploadResult | null>(null)
const dragging = ref(false)

const fileInput = ref<HTMLInputElement | null>(null)
const dirInput = ref<HTMLInputElement | null>(null)

/**
 * 当前环境支不支持「按文件夹选」。
 *
 * **不支持时要禁用按钮并说明，不能留一个点了没反应的按钮。**
 * 死按钮是最难查的一类故障：使用者无从判断是自己没点中、是页面卡了、
 * 还是系统坏了，而界面什么线索都不给。宁可明说「这个浏览器做不到，
 * 请用旁边那个」，也不要让人对着一个按钮反复点。
 *
 * webkitdirectory 是 Chrome/Edge/Firefox 都有的非标准属性，但部分内嵌
 * 浏览器（办公软件自带的、WebView 壳子）把它摘掉了——那种环境下
 * 属性探测为 false，正好能提前拦住。
 */
const canPickDir = typeof document !== 'undefined'
  && 'webkitdirectory' in document.createElement('input')

const dirHint = computed(() => canPickDir
  ? '选中文件夹后，子目录里的文件会一起带上'
  : '当前浏览器不支持按文件夹选择。请改用左边的「选择文件…」，'
    + '或者把文件直接拖进来；文件已经在服务器上的话，用下面的「纳入服务器已有目录」更省事')

/**
 * 打开文件选择框。
 *
 * 原来这里写的是 `dirInput?.click()`。那个 `?.` 是个陷阱：ref 万一没拿到，
 * 表达式安静地求值成 undefined，**界面上的表现就是「按钮点了没反应」**，
 * 控制台也没有任何报错。失败必须说出来。
 *
 * 点之前再把 webkitdirectory 落到 DOM 属性上：模板里那个写法依赖框架
 * 把静态属性翻译成 DOM 属性（Vue 对布尔属性确实是这么做的），
 * 但这条翻译链不归我们管。直接设一次属性，结果就不依赖它了。
 */
function openPicker(el: HTMLInputElement | null, dir: boolean) {
  if (!el) {
    err.value = '文件选择框没能打开：页面可能没加载完整。刷新一次再试；'
      + '如果刷新后还是这样，请把浏览器控制台（F12）里的红字发来'
    return
  }
  if (dir) el.webkitdirectory = true
  err.value = ''
  el.click()
}

const pickFiles = () => openPicker(fileInput.value, false)
const pickDir = () => openPicker(dirInput.value, true)

/** 结果标签样式。七态各有各的颜色——合并颜色就等于合并了处置动作。 */
const RESULT_CLS: Record<string, string> = {
  已入库: 't-pass',
  同名另存: 't-demo',          // 进库了，但要让人看见「这是新版本」
  同内容已存在: 't-na',        // 什么都没变
  扩展名不支持: 't-hit',
  超过大小上限: 't-hit',
  文件名不合法: 't-hit',
  写盘失败: 't-hit',
}

const totalBytes = computed(() => picked.value.reduce((s, f) => s + f.size, 0))
const limit = computed(() => props.storage?.limits)
/** 超限的那几份：**只提示不拦**，闸在后端。这里是为了让人上传前就看见。 */
const oversize = computed(() =>
  limit.value ? picked.value.filter(f => f.size > limit.value!.max_upload_bytes) : [])
const badExt = computed(() => {
  const ok = limit.value?.allowed_ext
  if (!ok) return []
  return picked.value.filter(f => {
    const i = f.name.lastIndexOf('.')
    return !ok.includes(i < 0 ? '' : f.name.slice(i).toLowerCase())
  })
})

function relOf(f: File): string {
  return (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name
}

function fmt(n: number): string {
  return n < 1024 ? `${n} B`
    : n < 1048576 ? `${(n / 1024).toFixed(1)} KB`
      : `${(n / 1048576).toFixed(1)} MB`
}

/** 按「相对路径」去重，不按文件名：两个子目录下的同名文件是两份不同的文件。 */
function add(list: FileList | File[] | null) {
  if (!list) return
  const seen = new Set(picked.value.map(relOf))
  for (const f of Array.from(list)) {
    if (!seen.has(relOf(f))) {
      picked.value.push(f)
      seen.add(relOf(f))
    }
  }
}

function drop(e: DragEvent) {
  dragging.value = false
  add(e.dataTransfer?.files || null)
}

function remove(i: number) {
  picked.value.splice(i, 1)
}

function clearAll() {
  picked.value = []
  result.value = null
  err.value = ''
}

async function send() {
  if (!picked.value.length) return
  busy.value = true
  err.value = ''
  result.value = null
  try {
    const r = await api.dlUpload(picked.value, {
      subdir: subdir.value.trim() || undefined,
      by: who.value.trim() || undefined,
    })
    result.value = r
    // **只清掉成功的那些**，被拒的留在待传列表里：
    // 全清之后使用者要重新去文件管理器里找那几份，而他此时正需要
    // 对着拒绝理由改文件名或换格式。
    const done = new Set(r.items.filter(i => i.result === '已入库' || i.result === '同名另存')
      .map(i => i.rel_path || i.file_name))
    picked.value = picked.value.filter(f => !done.has(relOf(f)))
    emit('done', r)
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div class="drop" :class="{ hot: dragging }"
         @dragover.prevent="dragging = true" @dragleave="dragging = false"
         @drop.prevent="drop">
      <div class="big">把制度文件拖到这里</div>
      <!-- type="button" 是必需的：button 不写 type 时默认是 submit，
           哪天这段被挪进 <form> 里，点一下会整页刷新而不是开选择框。 -->
      <div class="row" style="justify-content: center; margin: 10px 0 4px">
        <button type="button" @click="pickFiles">选择文件…</button>
        <button type="button" :disabled="!canPickDir" :title="dirHint" @click="pickDir">
          选择整个文件夹…
        </button>
        <button v-if="picked.length" type="button" @click="clearAll">清空待传</button>
      </div>
      <div class="muted" style="font-size: 12px">
        <template v-if="canPickDir">
          选整个文件夹时，<b>子目录结构会一起保留</b>。
        </template>
        <b v-else style="color: var(--warn, #b45309)">{{ dirHint }}</b>
        <template v-if="limit">
          单份上限 <b>{{ limit.max_upload_bytes_text }}</b>，
          一次最多 <b>{{ limit.max_upload_files }}</b> 份。
        </template>
      </div>
      <!-- 这两个 input 用 .offscreen 挪出视口，**不用 hidden**：
           display:none 的 file input 在部分内嵌浏览器里 .click() 不弹选择框，
           而表现恰好是「按钮点了没反应」。挪到视口外则一直参与布局。 -->
      <input ref="fileInput" class="offscreen" type="file" multiple tabindex="-1"
             aria-hidden="true"
             @change="add(($event.target as HTMLInputElement).files);
                      ($event.target as HTMLInputElement).value = ''" />
      <!-- webkitdirectory：浏览器会给每个 File 一个 webkitRelativePath，
           api.dlUpload 把它一并传给后端，于是子目录结构照样保留。
           属性在这里声明一次，openPicker() 点击前还会再设一次 DOM 属性。 -->
      <input ref="dirInput" class="offscreen" type="file" multiple webkitdirectory
             tabindex="-1" aria-hidden="true"
             @change="add(($event.target as HTMLInputElement).files);
                      ($event.target as HTMLInputElement).value = ''" />
    </div>

    <div class="row" style="margin-top: 12px">
      <span class="muted">放进子目录（可空）</span>
      <input v-model="subdir" placeholder="如 2025年度" style="width: 160px" />
      <span class="muted">上传人</span>
      <input v-model="who" placeholder="署名，可空" style="width: 120px" />
      <button :disabled="!picked.length || busy" @click="send">
        {{ busy ? '上传中…' : `上传 ${picked.length} 份（${fmt(totalBytes)}）` }}
      </button>
    </div>

    <div v-if="err" class="err">{{ err }}</div>

    <!-- 上传前的提示。**只提示不拦**：真正的闸在后端，前端再写一套必然分叉 -->
    <div v-if="badExt.length || oversize.length" class="note">
      <template v-if="badExt.length">
        有 <b>{{ badExt.length }}</b> 份的扩展名不在白名单里（{{
          badExt.slice(0, 3).map(f => f.name).join('、')
        }}{{ badExt.length > 3 ? ' 等' : '' }}），后端会拒掉它们。<br />
      </template>
      <template v-if="oversize.length">
        有 <b>{{ oversize.length }}</b> 份超过单份上限（{{
          oversize.slice(0, 3).map(f => `${f.name} ${fmt(f.size)}`).join('、')
        }}），后端会拒掉。
      </template>
    </div>

    <!-- 待传清单。可以逐个移除：夹带了不该传的，不必全部取消重来 -->
    <details v-if="picked.length" class="panel" open style="margin-top: 10px">
      <summary>待传 {{ picked.length }} 份 · {{ fmt(totalBytes) }}</summary>
      <table style="margin-top: 8px">
        <thead>
          <tr><th>相对路径</th><th class="num">大小</th><th style="width: 70px"></th></tr>
        </thead>
        <tbody>
          <tr v-for="(f, i) in picked" :key="relOf(f) + i">
            <td class="mono">{{ relOf(f) }}</td>
            <td class="num">{{ fmt(f.size) }}</td>
            <td><button @click="remove(i)">移除</button></td>
          </tr>
        </tbody>
      </table>
    </details>

    <!-- 逐份结果。**不只报一个总数**：被拒的那几份里可能有最要紧的那一版 -->
    <div v-if="result" class="panel" style="margin-top: 10px">
      <h3>
        本次上传 · 进库 {{ result.ok_count }} 份，未入库 {{ result.rejected.length }} 份
      </h3>
      <div class="note">
        落盘位置：<code class="mono">{{ result.dir }}</code><br />
        归属：<b>{{ result.domain_label }} / {{ result.instance_label }}</b>
        （托管目录「{{ result.root_label }}」，可写可删）
        <template v-if="result.scan">
          <br />已自动扫描：新增 <b>{{ result.scan.added }}</b>、
          修改 {{ result.scan.updated }}、未变 {{ result.scan.unchanged }}。
          六态判定与「待抽取」由扫描那条路产出，这里不另算一份。
        </template>
      </div>
      <table>
        <thead>
          <tr>
            <th>文件</th><th>结果</th><th class="num">大小</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(it, i) in result.items" :key="i">
            <td>
              <div class="mono">{{ it.rel_path || it.file_name }}</div>
              <div v-if="it.stored_name && it.stored_name !== it.file_name"
                   class="muted" style="font-size: 12px">
                落盘名 → <b>{{ it.stored_name }}</b>
              </div>
            </td>
            <td>
              <span class="tag" :class="RESULT_CLS[it.result] || 't-na'">{{ it.result }}</span>
            </td>
            <td class="num">{{ it.file_size == null ? '—' : fmt(it.file_size) }}</td>
            <!-- 后端的文案里有 ** 强调，必须过 MarkText 渲染，
                 直接插值会在页面上原样显示星号 -->
            <td style="max-width: 420px"><MarkText :text="it.reason || '—'" /></td>
          </tr>
        </tbody>
      </table>
      <div class="note" style="margin-bottom: 0"><MarkText :text="result.note" /></div>
    </div>
  </div>
</template>

<style scoped>
.drop {
  border: 2px dashed var(--line-strong);
  border-radius: var(--radius);
  padding: 22px 18px;
  text-align: center;
  background: #fafcfe;
  transition: background .12s, border-color .12s;
}
.drop.hot { border-color: var(--brand-light); background: #eef6ff; }
.drop .big { font-size: 15px; font-weight: 600; color: var(--brand); }

/* 代替 hidden：一个像素都不占，但**没有 display:none**。
   见上面 input 那里的注释——display:none 的 file input 在某些内嵌浏览器里
   .click() 不弹框，而那种失败在界面上和「按钮坏了」长得一模一样。 */
.offscreen {
  position: absolute;
  width: 1px; height: 1px;
  padding: 0; margin: -1px; border: 0;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
</style>
