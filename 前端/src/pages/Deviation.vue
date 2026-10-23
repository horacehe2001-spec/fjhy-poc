<script setup lang="ts">
/**
 * 偏差与回流。
 *
 * **误报与漏报分开统计，不合成准确率。**
 * 漏报放过风险；误报把正常单据判成问题件，审核岗很快就不再信任这套系统。
 * 合成一个「准确率」会把误报的代价藏起来。
 *
 * 「仅待人工」必须单列——它不是误报，是没把握。
 * 第一版把它算进误报，于是 9 张「只有待人工」的真实单被记成误报，
 * 虚报了误报率，而误报率恰恰是审核岗决定信不信任这套系统的依据。
 */
import Async from '../components/Async.vue'
import { api } from '../api'

const KIND: Record<string, { label: string; cls: string }> = {
  false_positive: { label: '误报（AI 命中、人工放行）', cls: 't-hit' },
  false_negative: { label: '漏报（AI 放行、人工退回）', cls: 't-hit' },
  needs_review_only: { label: '仅待人工（不是误报）', cls: 't-pend' },
}
</script>

<template>
  <h2>偏差与回流</h2>
  <div class="sub">人工复核结论回流，量化误报与漏报</div>

  <Async :load="() => api.deviation()">
    <template #default="{ data }">
      <div class="panel">
        <h3>偏差分布</h3>
        <table>
          <thead><tr><th>类型</th><th class="num">条数</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in (data as any).by_kind" :key="i">
              <td>
                <span class="tag" :class="KIND[r.deviation_kind]?.cls || 't-pass'">
                  {{ KIND[r.deviation_kind]?.label || '一致 / 无人工结论' }}
                </span>
              </td>
              <td class="num">{{ r.n }}</td>
            </tr>
          </tbody>
        </table>
        <div class="note"><MarkText :text="(data as any).note" /></div>
      </div>

      <div v-if="(data as any).false_positive_by_rule?.length" class="panel">
        <h3>误报按规则排序</h3>
        <table>
          <thead><tr><th>规则</th><th class="num">误报次数</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in (data as any).false_positive_by_rule" :key="i">
              <td class="mono">
                <router-link :to="`/asset/rules/${r.rule_id}`">{{ r.rule_id }}</router-link>
              </td>
              <td class="num">{{ r.n }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </Async>

  <Async :load="() => api.quarantine()">
    <template #default="{ data: q }">
      <div class="panel">
        <h3>装载隔离区（未处理 {{ (q as any).unresolved }} 行）</h3>
        <div class="err"><MarkText :text="(q as any).note" /></div>
        <table v-if="(q as any).items.length">
          <thead>
            <tr><th>来源</th><th>本该进</th><th>原因</th><th>缺口</th><th>原始行</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in (q as any).items" :key="r.id">
              <td class="mono" style="font-size: 11px">{{ r.source }}</td>
              <td class="mono">{{ r.target }}</td>
              <td class="muted"><MarkText :text="r.reason" /></td>
              <td class="mono">{{ r.gap_id || '—' }}</td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(r.row_data) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </Async>

  <Async :load="() => api.consistency()">
    <template #default="{ data: c }">
      <div class="panel">
        <h3>
          审计一致性自检
          <span class="tag" :class="(c as any).ok ? 't-pass' : 't-hit'" style="margin-left: 6px">
            {{ (c as any).ok ? '通过' : '不一致' }}
          </span>
        </h3>
        <table>
          <tbody>
            <tr v-for="(v, k) in (c as any).counts" :key="k">
              <td class="mono" style="width: 220px">{{ k }}</td>
              <td class="num">{{ v }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="(c as any).problems?.length" class="err">
          <div v-for="(p, i) in (c as any).problems" :key="i">{{ p }}</div>
        </div>
      </div>
    </template>
  </Async>
</template>
