<script setup lang="ts">
/**
 * 判定结果标签。**四态必须视觉上分得开。**
 *
 * 最要紧的是「待人工」不能长得像「通过」：
 * 把 pending 显示成绿色或干脆不显示，审核岗就会以为这条查过了而且没问题，
 * 而实际是「判不了」。那等于让系统声称复核了没复核的东西。
 */
const props = defineProps<{
  outcome: string
  controlType?: string
  notApplicable?: boolean
}>()

const LABEL: Record<string, string> = {
  hit: '命中',
  pass: '通过',
  pending: '待人工',
  excluded: '未执行',
}
const CLS: Record<string, string> = {
  hit: 't-hit',
  pass: 't-pass',
  pending: 't-pend',
  excluded: 't-na',
}
</script>

<template>
  <span v-if="notApplicable" class="tag t-na">不适用</span>
  <span v-else class="tag" :class="CLS[props.outcome] || 't-na'">
    {{ LABEL[props.outcome] || props.outcome }}
  </span>
  <span
    v-if="props.outcome === 'hit' && props.controlType"
    class="tag"
    :class="props.controlType === '禁止' ? 't-block' : 't-warn'"
    style="margin-left: 4px"
  >
    {{ props.controlType }}
  </span>
</template>
