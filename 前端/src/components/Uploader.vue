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
      <div class="row" style="justify-content: center; margin: 10px 0 4px">
        <button @click="fileInput?.click()">选择文件…</button>
        <button @click="dirInput?.click()">选择整个文件夹…</button>
        <button v-if="picked.length" @click="clearAll">清空待传</button>
      </div>
      <div class="muted" style="font-size: 12px">
        选整个文件夹时，<b>子目录结构会一起保留</b>。
        <template v-if="limit">
          单份上限 <b>{{ limit.max_upload_bytes_text }}</b>，
          一次最多 <b>{{ limit.max_upload_files }}</b> 份。
        </template>
      </div>
      <input ref="fileInput" type="file" multiple hidden
             @change="add(($event.target as HTMLInputElement).files);
                      ($event.target as HTMLInputElement).value = ''" />
      <!-- webkitdirectory：浏览器会给每个 File 一个 webkitRelativePath，
           api.dlUpload 把它一并传给后端，于是子目录结构照样保留。 -->
      <input ref="dirInput" type="file" multiple hidden webkitdirectory
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
</style>
