<script setup lang="ts">
/**
 * 系统自检页。
 *
 * **失败项要原样列出来，不能只给一个 ok / false。**
 * 「哪几项没过、为什么」才是能让人去修的信息；
 * 一个红灯加一句「系统异常」只会让人去翻日志。
 */
import Async from '../components/Async.vue'
import { api } from '../api'

interface Check { name: string; ok: boolean; detail: string }
interface Report {
  contract: {
    checks: Check[]; errors: string[]; warnings: string[]
    executable: string[]
    excluded: { rule_id: string; why: string }[]
  }
  agents: {
    checks: Check[]; errors: string[]
    items: {
      agent_id: string; version: string; layer: string; name: string
      triggers: string[]; reads: string[]; writes: string[]
      params: Record<string, { lo: number; hi: number; default: number; why: string }>
      forbidden: string[]
    }[]
  }
  mask: {
    mode: string; outlets: string[]; policies: string[]
    sensitive_fields: Record<string, string>
  }
}
</script>

<template>
  <h2>系统自检</h2>
  <div class="sub">
    启动时两道契约校验的结果。任一不过就拒绝启动（Fail Fast）——
    不是「启动后报警」：启动后报警的系统会在报警被忽略的情况下继续给出错误的审核结论。
  </div>

  <Async :load="() => api.startupReport() as unknown as Promise<Report>">
    <template #default="{ data }">
      <div class="panel">
        <h3>规则资产契约</h3>
        <div class="stat" style="margin-bottom: 10px">
          <div>
            <b>{{ data.contract.checks.length }}</b><span>检查项</span>
          </div>
          <div>
            <b :style="{ color: data.contract.errors.length ? 'var(--hit)' : 'var(--pass)' }">
              {{ data.contract.errors.length }}
            </b><span>失败</span>
          </div>
          <div>
            <b :style="{ color: data.contract.warnings.length ? 'var(--pend)' : 'var(--pass)' }">
              {{ data.contract.warnings.length }}
            </b><span>警告</span>
          </div>
          <div><b>{{ data.contract.executable.length }}</b><span>执行集</span></div>
          <div><b>{{ data.contract.excluded.length }}</b><span>剔除</span></div>
        </div>
        <table>
          <thead><tr><th style="width: 60px">结果</th><th>检查项</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in data.contract.checks" :key="c.name">
              <td>
                <span class="tag" :class="c.ok ? 't-pass' : 't-hit'">
                  {{ c.ok ? '通过' : '失败' }}
                </span>
              </td>
              <td>{{ c.name }}</td>
              <td class="muted"><MarkText :text="c.detail" /></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>被剔出执行集的规则（{{ data.contract.excluded.length }} 条）</h3>
        <div class="note">
          这些规则本次<b>没有执行</b>。不是通过，是没跑。
          逐条记原因是必须的——静默剔除会让一条本该拦截的闸悄悄消失，而且没人知道。
        </div>
        <table>
          <thead><tr><th style="width: 110px">规则</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="e in data.contract.excluded" :key="e.rule_id">
              <td class="mono">{{ e.rule_id }}</td>
              <td class="muted"><MarkText :text="e.why" /></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>Agent 配置（{{ data.agents.items.length }} 个，检查 {{ data.agents.checks.length }} 项，失败 {{ data.agents.errors.length }}）</h3>
        <div v-for="a in data.agents.items" :key="a.agent_id" style="margin-bottom: 14px">
          <b>{{ a.name }}</b>
          <code style="margin-left: 6px">{{ a.agent_id }}</code>
          <span class="tag t-na" style="margin-left: 6px">{{ a.layer }}</span>
          <span class="tag t-pass" style="margin-left: 4px">v{{ a.version }}</span>
          <div class="kv" style="margin-top: 6px">
            <b>触发</b><span>{{ a.triggers.join(' + ') }}</span>
            <b>S 阶段可读表</b><span class="mono">{{ a.reads.join(', ') }}</span>
            <b>A 阶段可写表</b><span class="mono">{{ a.writes.join(', ') }}</span>
            <b>I 阶段可调参数</b>
            <span>
              <div v-for="(v, k) in a.params" :key="k">
                <code>{{ k }}</code> = {{ v.default }}，区间 [{{ v.lo }}, {{ v.hi }}]
                <div class="muted" style="font-size: 12px"><MarkText :text="v.why" /></div>
              </div>
            </span>
            <b>I 阶段禁止动的</b>
            <span>
              <span v-for="f in a.forbidden" :key="f" class="tag t-hit" style="margin-right: 4px">
                {{ f }}
              </span>
            </span>
          </div>
        </div>
      </div>

      <div class="panel">
        <h3>脱敏</h3>
        <div class="kv">
          <b>开关</b>
          <span>
            <span class="tag" :class="data.mask.mode === 'on' ? 't-pass' : 't-hit'">
              {{ data.mask.mode }}
            </span>
            <span v-if="data.mask.mode !== 'on'" class="muted" style="margin-left: 6px">
              生产环境绝不允许关闭
            </span>
          </span>
          <b>策略（{{ data.mask.policies.length }} 条）</b>
          <span><div v-for="p in data.mask.policies" :key="p">{{ p }}</div></span>
          <b>覆盖出口（{{ data.mask.outlets.length }} 个）</b>
          <span><div v-for="o in data.mask.outlets" :key="o">· {{ o }}</div></span>
          <b>敏感字段</b>
          <span>
            <code v-for="(v, k) in data.mask.sensitive_fields" :key="k" style="margin-right: 8px">
              {{ k }} {{ v }}
            </code>
          </span>
        </div>
      </div>
    </template>
  </Async>
</template>
