<script setup lang="ts">
/**
 * 文件夹选择器。从盘符逐级点进去，**可多选**一起加入。
 *
 * 为什么不用浏览器原生的目录选择对话框（File System Access API）：
 * 那套拿到的是浏览器内部的句柄，给不出绝对路径，后端读不到文件，
 * 规则溯源也就无从谈起；而且「记住选了哪些文件夹」只能存在该浏览器里，
 * 换个浏览器、换台机器就没了。这里走后端扫盘，选中的路径存进 PostgreSQL，
 * 重启服务、换浏览器都还在。
 *
 * 代价要说清：这意味着后端把本机目录结构暴露给了前端。
 * 本系统是本机单用户的 POC，后端对所有外来路径做了校验（doclib.safe_dir）。
 * 若要对外提供，必须先加一层「允许的根目录白名单」。
 */
import { onMounted, ref } from 'vue'
import { api } from '../api'
import type { DlBrowse } from '../api'

const emit = defineEmits<{ (e: 'add', paths: string[]): void; (e: 'close'): void }>()

const cur = ref<DlBrowse | null>(null)
const picked = ref<string[]>([])
const manual = ref('')
const err = ref('')
const busy = ref(false)

async function go(path?: string) {
  busy.value = true
  err.value = ''
  try {
    cur.value = await api.dlBrowse(path)
  } catch (e) {
    // 取不到就说取不到，**不要退化成空目录**——
    // 空目录看着像「这里没东西」，实际是没权限或路径不存在。
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

function toggle(p: string) {
  const i = picked.value.indexOf(p)
  if (i < 0) picked.value.push(p)
  else picked.value.splice(i, 1)
}

/** 制度资料常常就是「这个文件夹连同子文件夹全都要」，所以单给一个按钮 */
function pickCurrent() {
  const p = cur.value?.path
  if (p && !picked.value.includes(p)) picked.value.push(p)
}

function addManual() {
  const p = manual.value.trim()
  if (p && !picked.value.includes(p)) picked.value.push(p)
  manual.value = ''
}

onMounted(() => go())
</script>

<template>
  <div class="panel" style="border-color: var(--brand)">
    <h3>选择文件夹</h3>
    <div class="row">
      <span class="muted">驱动器</span>
      <button v-for="d in cur?.drives || []" :key="d.path" @click="go(d.path)">
        {{ d.name }}
      </button>
      <button :disabled="!cur?.parent" @click="go(cur?.parent || undefined)">↑ 上一层</button>
      <button :disabled="!cur?.path" @click="pickCurrent">＋ 选中当前文件夹</button>
    </div>

    <div class="note">
      当前位置：<code class="mono">{{ cur?.path || '（驱动器根）' }}</code>
      <template v-if="cur?.path">
        ｜本层文档 <b>{{ cur.files }}</b> 份｜子文件夹 {{ cur.dirs.length }} 个
      </template>
    </div>
    <div v-if="err" class="err">{{ err }}</div>

    <div class="dirlist">
      <div v-if="busy" class="muted" style="padding: 8px">读取中…</div>
      <div v-else-if="!cur?.dirs.length" class="muted" style="padding: 8px">
        没有可进入的子文件夹
      </div>
      <label v-for="d in cur?.dirs || []" :key="d.path" class="dirrow">
        <input type="checkbox" :checked="picked.includes(d.path)" @change="toggle(d.path)" />
        <span class="nm">{{ d.name }}</span>
        <button @click.prevent="go(d.path)">进入 ›</button>
      </label>
    </div>

    <div class="row" style="margin-top: 10px">
      <span class="muted">或直接粘贴路径</span>
      <input v-model="manual" style="flex: 1; min-width: 220px"
             placeholder="D:\制度文件\2025" @keyup.enter="addManual" />
      <button @click="addManual">加入待选</button>
    </div>

    <div v-if="picked.length" class="note">
      <b>待加入 {{ picked.length }} 个文件夹：</b>
      <div v-for="p in picked" :key="p" class="row" style="margin: 4px 0">
        <code class="mono" style="flex: 1">{{ p }}</code>
        <button @click="toggle(p)">移除</button>
      </div>
    </div>

    <div class="row" style="margin-top: 10px">
      <button :disabled="!picked.length" @click="emit('add', [...picked])">
        加入文件库（{{ picked.length }}）
      </button>
      <button @click="emit('close')">取消</button>
    </div>
  </div>
</template>

<style scoped>
.dirlist {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid var(--line);
  background: #fff;
  margin-top: 8px;
}
.dirrow {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 9px;
  border-bottom: 1px solid var(--line);
  cursor: pointer;
}
.dirrow:last-child { border-bottom: 0; }
.dirrow:hover { background: #f5f8fc; }
.dirrow .nm { flex: 1; }
</style>
