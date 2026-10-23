<script setup lang="ts">
/**
 * 单条规则详情：八要素 + 概念绑定 + 可得性 + 最近命中。
 *
 * **概念的可得性要和规则并排显示。** 一条规则「为什么跑不了」的答案
 * 几乎总是「它引用的某个字段取不到」，而那个字段叫什么、属于哪个缺口，
 * 是让业务侧去补数据的唯一依据。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Async from '../components/Async.vue'
import Outcome from '../components/Outcome.vue'
import { api } from '../api'

const route = useRoute()
const id = computed(() => String(route.params.id))

const AVAIL_OK = new Set(['样本可得', '样本可得(审批意见)', '已录入资产', '可推导'])
</script>

<template>
  <Async :load="() => api.rule(id)" :deps="id">
    <template #default="{ data }">
      <h2>{{ (data as any).rule_id }} {{ (data as any).rule_name }}</h2>
      <div class="sub">
        <router-link to="/asset/rules">← 回规则本体</router-link>
      </div>

      <div class="panel">
        <h3>八要素</h3>
        <div class="kv">
          <b>① 规则名称</b><span>{{ (data as any).rule_name }}</span>
          <b>② 审核维度</b><span class="mono">{{ (data as any).dimension }}</span>
          <b>③ 风险类别</b><span>{{ (data as any).risk_class }}</span>
          <b>④ 来源条款</b>
          <span>
            <template v-if="(data as any).source_clause">
              <code>{{ (data as any).source_clause.file }}</code>
              {{ (data as any).source_clause.clause }}
              <span class="tag" :class="(data as any).source_clause.confidence === 'OCR' ? 't-pend' : 't-pass'"
                    style="margin-left: 4px">
                {{ (data as any).source_clause.confidence }}
              </span>
              <div class="muted" style="margin-top: 3px">
                「{{ (data as any).source_clause.text }}」
              </div>
            </template>
          </span>
          <b>⑤ 适用范围</b>
          <span><pre class="json">{{ JSON.stringify((data as any).scope, null, 1) }}</pre></span>
          <b>⑥ 判定逻辑</b>
          <span><pre class="json">{{ JSON.stringify((data as any).logic, null, 1) }}</pre></span>
          <b>⑦ 例外</b><span>{{ (data as any).exception || '无' }}</span>
          <b>⑧ 优先级</b><span>{{ (data as any).priority }}</span>
          <b>责任部门</b><span>{{ (data as any).owner || '—' }}</span>
          <b>生效期</b>
          <span>{{ (data as any).effective_from || '—' }} ~ {{ (data as any).effective_to || '长期' }}</span>
        </div>
      </div>

      <div class="panel">
        <h3>对齐客户配置器</h3>
        <div class="kv">
          <b>算法编码</b><span class="mono">{{ (data as any).algo_code || '—' }}</span>
          <b>算法类型</b><span>{{ (data as any).algo_type || '—' }}</span>
          <b>控制类型</b>
          <span>
            <span class="tag" :class="(data as any).control_type === '禁止' ? 't-block' : 't-warn'">
              {{ (data as any).control_type }}
            </span>
          </span>
          <b>配置器状态</b><span>{{ (data as any).configurator_status || '—' }}</span>
          <b>客户自评可实现性</b><span>{{ (data as any).feasibility || '未评估' }}</span>
          <b>实现途径（原文）</b><span>{{ (data as any).impl_path || '—' }}</span>
        </div>
      </div>

      <div class="panel">
        <h3>可执行性</h3>
        <div class="kv">
          <b>规则资产层面</b>
          <span>
            <span class="tag" :class="(data as any).compiled ? 't-pass' : 't-na'">
              {{ (data as any).compiled ? '已编译' : '未编译' }}
            </span>
            <span v-if="(data as any).compiled_reason" class="muted" style="margin-left: 6px">
              <MarkText :text="(data as any).compiled_reason" />
            </span>
          </span>
          <b>当前数据能否跑</b>
          <span>
            <span class="tag" :class="(data as any).demo_executable ? 't-pass' : 't-pend'">
              {{ (data as any).demo_executable ? '在执行集' : '不在执行集' }}
            </span>
            <span v-if="(data as any).demo_scope" class="muted" style="margin-left: 6px">
              {{ (data as any).demo_scope }}
            </span>
          </span>
          <template v-if="(data as any).demo_blocker">
            <b>阻塞原因</b>
            <span class="note" style="margin: 0">{{ (data as any).demo_blocker }}</span>
          </template>
          <template v-if="(data as any).logic?.degraded?.cannot_hit_without">
            <b>结构性无法命中</b>
            <span class="err" style="margin: 0">
              缺 <code>{{ (data as any).logic.degraded.cannot_hit_without.join(', ') }}</code> 时本条永远不会命中。
              <div style="margin-top: 4px">
                现在实际退化成：<b>{{ (data as any).logic.degraded.degenerates_to }}</b>
              </div>
              <div class="muted" style="margin-top: 4px">
                <MarkText :text="(data as any).logic.degraded.cannot_hit_why" />
              </div>
            </span>
          </template>
          <template v-if="(data as any).logic?.rule_effect === 'exempt'">
            <b>规则作用</b>
            <span class="note" style="margin: 0">
              <b>豁免型</b>：命中 = 豁免生效，按设计不产生违规告警。
              <div class="muted"><MarkText :text="(data as any).logic.rule_effect_why" /></div>
            </span>
          </template>
        </div>
      </div>

      <div class="panel">
        <h3>引用的本体概念</h3>
        <table>
          <thead>
            <tr><th>概念</th><th>名称</th><th>所属实体</th><th>可得性</th><th>缺口</th></tr>
          </thead>
          <tbody>
            <tr v-for="c in (data as any).concepts" :key="c.concept_id">
              <td class="mono">{{ c.concept_id }}</td>
              <td>{{ c.concept_name }}</td>
              <td class="mono">{{ c.entity_id }}</td>
              <td>
                <span class="tag" :class="AVAIL_OK.has(c.availability) ? 't-pass' : 't-pend'">
                  {{ c.availability }}
                </span>
              </td>
              <td class="mono">{{ c.gap_id || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="(data as any).entities?.length" style="margin-top: 10px">
          <b>存在性判断引用的实体</b>
          <div class="muted" style="font-size: 12px">
            实体存在性判断与概念引用分开两张表：
            「这类附件在不在」和「某个字段的值是多少」是两件事。
          </div>
          <table style="margin-top: 6px">
            <tbody>
              <tr v-for="e in (data as any).entities" :key="e.entity_id">
                <td class="mono" style="width: 70px">{{ e.entity_id }}</td>
                <td class="muted">{{ e.semantics }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="(data as any).relations?.length" class="panel">
        <h3>与其他规则的关系</h3>
        <table>
          <thead><tr><th>关系</th><th>对方</th><th>需裁决</th><th>缺口</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(r, i) in (data as any).relations" :key="i">
              <td class="mono">{{ r.rel_type }}</td>
              <td class="mono">{{ r.other_rule }}</td>
              <td>
                <span v-if="r.needs_confirm" class="tag t-pend">需业务侧裁决</span>
                <span v-else class="muted">—</span>
              </td>
              <td class="mono">{{ r.gap_id || '—' }}</td>
              <td class="muted" style="font-size: 12px"><MarkText :text="(r.note || '').slice(0, 200)" /></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>最近的判定</h3>
        <div v-if="!(data as any).recent_findings?.length" class="muted">
          这条规则还没有产生过任何判定记录。
        </div>
        <table v-else>
          <thead><tr><th>单据</th><th>结果</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(f, i) in (data as any).recent_findings" :key="i">
              <td class="mono">
                <router-link :to="`/review/report/${f.run_id}`">{{ f.bill_no }}</router-link>
              </td>
              <td><Outcome :outcome="f.outcome" /></td>
              <td>{{ f.message }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="(data as any).note" class="panel">
        <h3>维护备注</h3>
        <div style="white-space: pre-wrap">{{ (data as any).note }}</div>
      </div>
    </template>
  </Async>
</template>
