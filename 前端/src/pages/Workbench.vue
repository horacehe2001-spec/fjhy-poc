<script setup lang="ts">
/**
 * 审核岗工作台。
 *
 * **演示数据与真实单据必须一眼分开。**
 * 混在一起之后，「这个结论是跑真实单据得出的还是跑编的数据得出的」就说不清了，
 * 而这正是演示时最容易被问到、也最该答清楚的问题。
 *
 * 列表上每张单显示四个数：执行 / 命中 / 待人工 / 候选。
 * 只显示「命中 0」会让人以为 82 条都查过而且都没问题。
 */
import { computed, ref } from 'vue'
import Async from '../components/Async.vue'
import { api, type Bill, type Page } from '../api'

const concl = ref('')
const demo = ref('')
const q = ref('')
const deps = computed(() => [concl.value, demo.value, q.value].join('|'))

function load(): Promise<Page<Bill>> {
  return api.bills({
    conclusion: concl.value || undefined,
    is_demo: demo.value === '' ? undefined : demo.value === 'true',
    q: q.value || undefined,
    size: 100,
  })
}

const CONCL: Record<string, { label: string; cls: string }> = {
  pass: { label: '通过', cls: 't-pass' },
  review: { label: '待复核', cls: 't-pend' },
  reject: { label: '不通过', cls: 't-hit' },
}
</script>

<template>
  <h2>审核岗工作台</h2>
  <div class="sub">
    AI 结论为<b>参考</b>，不阻断流程。实测印证：某张单在 17:25:03 得到「AI审核不通过」，
    17:35:41 仍进入共享审核并由人工给出退回理由——闸门在人工手里。
  </div>

  <Async :load="load" :deps="deps">
    <template #default="{ data }">
      <div v-if="data.banner" class="note">
        真实单据 <b>{{ data.banner.real_bills }}</b> 张 ·
        演示数据 <b>{{ data.banner.demo_bills }}</b> 张。
        <MarkText :text="data.banner.note" />
      </div>

      <div class="panel">
        <div class="row">
          <select v-model="concl">
            <option value="">全部结论</option>
            <option value="pass">通过</option>
            <option value="review">待复核</option>
            <option value="reject">不通过</option>
          </select>
          <select v-model="demo">
            <option value="">真实 + 演示</option>
            <option value="false">只看真实单据</option>
            <option value="true">只看演示数据</option>
          </select>
          <input v-model="q" placeholder="搜单据号或摘要" style="width: 220px" />
          <span class="muted">共 {{ data.total }} 张</span>
        </div>

        <table>
          <thead>
            <tr>
              <th style="width: 196px">单据编号</th>
              <th style="width: 88px">日期</th>
              <th>摘要</th>
              <th class="num" style="width: 86px">金额</th>
              <th style="width: 68px">结论</th>
              <th class="num" style="width: 52px">执行</th>
              <th class="num" style="width: 52px">命中</th>
              <th class="num" style="width: 62px">待人工</th>
              <th class="num" style="width: 52px">候选</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="b in data.items" :key="b.bill_no">
              <td class="mono">
                <router-link :to="`/review/bills/${b.bill_no}`">{{ b.bill_no }}</router-link>
                <span v-if="b.is_demo" class="tag t-demo" style="margin-left: 4px">演示</span>
              </td>
              <td class="mono">{{ (b.doc_date || '').slice(0, 10) }}</td>
              <td>
                {{ b.summary }}
                <span v-if="!b.has_remark" class="muted" style="font-size: 11px">（无补充说明）</span>
              </td>
              <td class="num">{{ b.amount?.toLocaleString() }}</td>
              <td>
                <router-link v-if="b.run_id" :to="`/review/report/${b.run_id}`">
                  <span class="tag" :class="CONCL[b.conclusion || '']?.cls || 't-na'">
                    {{ CONCL[b.conclusion || '']?.label || '未审' }}
                  </span>
                </router-link>
                <span v-else class="tag t-na">未审</span>
              </td>
              <td class="num">{{ b.rule_executed ?? '—' }}</td>
              <td class="num" :style="{ color: b.hit_count ? 'var(--hit)' : undefined }">
                {{ b.hit_count ?? '—' }}
              </td>
              <td class="num" :style="{ color: b.pending_count ? 'var(--pend)' : undefined }">
                {{ b.pending_count ?? '—' }}
              </td>
              <td class="num muted">{{ b.rule_candidates ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="note">
          「执行」与「候选」两个数要一起看：执行 12 / 候选 82 的意思是
          <b>另有 70 条没跑</b>，不是「都查过了」。
          每张单的报告里会把被剔除的规则单列一节。
        </div>
      </div>
    </template>
  </Async>
</template>
