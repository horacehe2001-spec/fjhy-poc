<script setup lang="ts" generic="T">
/**
 * 异步数据容器。三态显式：加载中 / 出错 / 有数据。
 *
 * **出错必须显示出来，不能退化成「没有数据」。**
 * 前端最常见的隐藏故障方式就是 catch 之后 return []：
 * 页面上看着是空列表，实际是接口挂了。
 * 审核岗据此以为「这批单没问题」，而真相是什么都没查。
 */
import { onMounted, ref, watch } from 'vue'

const props = defineProps<{
  /** 取数函数。变化时会重新取 */
  load: () => Promise<T>
  /** 依赖项变化时重新取数 */
  deps?: unknown
}>()

const data = ref<T | null>(null)
const error = ref('')
const loading = ref(true)

async function run() {
  loading.value = true
  error.value = ''
  try {
    data.value = await props.load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    data.value = null
  } finally {
    loading.value = false
  }
}

onMounted(run)
watch(() => props.deps, run)
defineExpose({ reload: run })
</script>

<template>
  <div v-if="loading" class="muted">加载中…</div>
  <div v-else-if="error" class="err">
    <b>取数失败</b>：{{ error }}
    <div class="muted" style="margin-top: 4px">
      这不是「没有数据」。后端没起的话先跑：
      <code>cd 服务端 &amp;&amp; python -I -m uvicorn app.main:app --port 8000</code>
    </div>
    <button style="margin-top: 6px" @click="run">重试</button>
  </div>
  <slot v-else :data="data as T" :reload="run" />
</template>
