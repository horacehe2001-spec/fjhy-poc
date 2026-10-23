<script setup lang="ts">
/**
 * 把后端文案里的 **加粗** 渲染成真正的粗体。
 *
 * **为什么不用 v-html**：
 *   v-html 允许任意 HTML，遇到后端文案里带 < / > 的字符（比如规则表达式
 *   `amount > 100`）就会变成标签——XSS 风险，且这里的文案根本没有 HTML 结构。
 *
 * **为什么不在 API 层统一剥**：
 *   onto 原文照抄接口和 pub 层预览接口返回的是原文，
 *   里面有带 ** 的 YAML 注释；onto_yaml.py 写本体 YAML 时也会写入 ** 注释。
 *   API 层统一剥离会静默改写原文接口的返回内容，且会把脱敏输出里的
 *   sk-****3f2a 和 *** 也剥掉（不是加粗语法，而是掩码占位符）。
 *
 * **正确性：**
 *   `sk-****3f2a` — 四个星号之间没有非星号内容，`[^*]+` 匹配失败，不命中。
 *   `***`         — 两个星号后立即又是星号，同上，不命中。
 *   `** 未核对 **` — 中间有空格，命中，加粗显示。这是预期行为。
 */
import { computed } from 'vue'

const props = defineProps<{ text?: string | null }>()

interface Seg { bold: boolean; s: string }

const segs = computed<Seg[]>(() => {
  const t = props.text
  if (!t) return []
  const out: Seg[] = []
  // 匹配 **非星号内容**，捕获中间内容
  const re = /\*\*([^*]+)\*\*/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(t)) !== null) {
    if (m.index > last) out.push({ bold: false, s: t.slice(last, m.index) })
    out.push({ bold: true, s: m[1] })
    last = re.lastIndex
  }
  if (last < t.length) out.push({ bold: false, s: t.slice(last) })
  return out
})
</script>

<template>
  <!--
    segs 为空有两种情况：
    1. text 是 null/undefined/空字符串 → 什么都不渲染
    2. text 里没有 **...** → 渲染为普通文本（一个 plain segment）
    所以 segs 非空才渲染，segs 为空就渲染 text 本身（null/undefined 时什么都没有）。
  -->
  <template v-if="segs.length">
    <template v-for="(seg, i) in segs" :key="i">
      <b v-if="seg.bold">{{ seg.s }}</b>
      <template v-else>{{ seg.s }}</template>
    </template>
  </template>
  <template v-else>{{ text ?? '' }}</template>
</template>
