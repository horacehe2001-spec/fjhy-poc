<script setup lang="ts">
/**
 * 审计追溯：一张单的 S→D→A→F→I 全过程。
 *
 * **五张审计表分开存的价值就在这一页上。**
 * 合成一张「日志表」之后，「模型看到的输入」和「模型造成的写入」
 * 就混在同一批行里，这里没法分开追——
 * 而出事后最关键的两个问题恰恰是：
 *   当时它看到的数据是什么？（没有 S 就只能靠猜）
 *   它到底写了什么？（没有 A 就要去业务表反查，而业务表已经被覆盖了）
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Async from '../components/Async.vue'
import { api } from '../api'

const route = useRoute()
const router = useRouter()
const billNo = ref(String(route.params.no || ''))
watch(() => route.params.no, (v) => { billNo.value = String(v || '') })

const key = computed(() => billNo.value)

function go() {
  if (billNo.value) router.push(`/review/trace/${billNo.value}`)
}
</script>

<template>
  <h2>审计追溯</h2>
  <div class="sub">S 看到什么 → D 判了什么 → A 写了什么 → F 偏差多少 → I 调了什么</div>

  <div class="panel">
    <div class="row">
      <input v-model="billNo" placeholder="单据编号，如 FJPG-BX2610132121" style="width: 280px" />
      <button @click="go">追溯</button>
    </div>
  </div>

  <Async v-if="key" :load="() => api.trace(key)" :deps="key">
    <template #default="{ data }">
      <div class="panel">
        <h3>S · 看到的数据快照（{{ (data as any).S_snapshots.length }} 次）</h3>
        <table>
          <thead>
            <tr>
              <th>快照号</th><th>时间</th><th>输入指纹</th>
              <th>已脱敏</th><th>读了哪些表</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in (data as any).S_snapshots" :key="s.snapshot_id">
              <td class="mono">{{ s.snapshot_id }}</td>
              <td class="mono">{{ String(s.taken_at).slice(0, 19) }}</td>
              <td class="mono">{{ String(s.payload_hash).slice(0, 12) }}…</td>
              <td>
                <span class="tag" :class="s.masked ? 't-pass' : 't-hit'">
                  {{ s.masked ? '是' : '否（泄露）' }}
                </span>
              </td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(s.row_counts) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>D · 判定周期（{{ (data as any).D_cycles.length }} 次）</h3>
        <table>
          <thead>
            <tr>
              <th>周期号</th><th>触发</th><th class="num">候选</th><th class="num">执行</th>
              <th>结论</th><th>各阶段耗时</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in (data as any).D_cycles" :key="c.cycle_id">
              <td class="mono">{{ c.cycle_id }}</td>
              <td><span class="tag t-na">{{ c.trigger_kind }}</span></td>
              <td class="num">{{ c.rule_candidates }}</td>
              <td class="num">{{ c.rule_executed }}</td>
              <td class="mono">{{ c.decision?.conclusion }}</td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(c.phase_timings_ms) }}</td>
              <td>
                <span class="tag" :class="c.status === 'ok' ? 't-pass' : 't-hit'">{{ c.status }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>A · 实际写了什么（{{ (data as any).A_actions.length }} 次）</h3>
        <table>
          <thead><tr><th>时间</th><th>动作</th><th>目标</th><th>写了哪些表</th><th>幂等键</th></tr></thead>
          <tbody>
            <tr v-for="(a, i) in (data as any).A_actions" :key="i">
              <td class="mono">{{ String(a.emitted_at).slice(0, 19) }}</td>
              <td class="mono">{{ a.action }}</td>
              <td class="mono">{{ a.target }}</td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(a.written_tables) }}</td>
              <td class="mono" style="font-size: 11px">{{ a.idempotency_key }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>F · 偏差（{{ (data as any).F_feedback.length }} 条）</h3>
        <table>
          <thead><tr><th>时间</th><th>指标</th><th>期望</th><th>实际</th><th class="num">偏差</th><th>类型</th><th>来源</th></tr></thead>
          <tbody>
            <tr v-for="(f, i) in (data as any).F_feedback" :key="i">
              <td class="mono">{{ String(f.observed_at).slice(0, 19) }}</td>
              <td class="mono">{{ f.metric }}</td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(f.expected) }}</td>
              <td class="mono" style="font-size: 11px">{{ JSON.stringify(f.actual) }}</td>
              <td class="num">{{ f.deviation ?? '—' }}</td>
              <td>
                <span v-if="f.deviation_kind === 'false_positive'" class="tag t-hit">误报</span>
                <span v-else-if="f.deviation_kind === 'false_negative'" class="tag t-hit">漏报</span>
                <span v-else-if="f.deviation_kind === 'needs_review_only'" class="tag t-pend">仅待人工</span>
                <span v-else class="tag t-pass">一致</span>
              </td>
              <td class="mono">{{ f.source }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>I · 参数调整（{{ (data as any).I_improvements.length }} 条）</h3>
        <div v-if="(data as any).I_scope_note" class="note">
          <MarkText :text="(data as any).I_scope_note" />
        </div>
        <div v-if="!(data as any).I_improvements.length" class="muted">
          本单所涉 Agent 近期没有调参记录。
        </div>
        <table v-else>
          <thead><tr><th>时间</th><th>参数</th><th>原值</th><th>新值</th><th>结果</th><th>理由</th></tr></thead>
          <tbody>
            <tr v-for="(x, i) in (data as any).I_improvements" :key="i">
              <td class="mono">
                {{ String(x.applied_at).slice(0, 19) }}
                <span v-if="!x.per_cycle" class="tag t-na">批次级</span>
              </td>
              <td class="mono">{{ x.param }}</td>
              <td class="mono">{{ JSON.stringify(x.old_value) }}</td>
              <td class="mono">{{ JSON.stringify(x.new_value) }}</td>
              <td>
                <span class="tag" :class="x.rejected ? 't-hit' : 't-pass'">
                  {{ x.rejected ? '已拒绝' : '已生效' }}
                </span>
                <div v-if="x.rejected_why" class="muted" style="font-size: 11px"><MarkText :text="x.rejected_why" /></div>
              </td>
              <td class="muted"><MarkText :text="x.reason" /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </Async>
</template>
