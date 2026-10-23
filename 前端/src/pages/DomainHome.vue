<script setup lang="ts">
/**
 * 领域知识资产 · 落地页。点菜单组名进来的那一页。
 *
 * 两件事：
 *   ① 选领域、选实例。选择是**全局单例**（库里一行，所有人共用，
 *      换浏览器也一样），不是存在本地的偏好。
 *   ② 显示当前实例的资产概览，让人一眼看出「现在这套资产有多大」。
 *
 * **口径必须写在页面上**：本期做的是「归属 + 展示」，接口**尚未按实例过滤**。
 * 所以下面那些数字是全库数字。把它们默认当成「当前实例筛出来的」，
 * 等有了第二个实例就会发现对不上——而那时已经说不清是从哪天开始错的。
 *
 * 选完**不自动跳转**到某个栏目：跳转会让人以为「选领域」是个向导的第一步，
 * 而它其实是个随时可改的开关。
 */
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { current, domains, load, loadErr, setCurrent, isComplete } from '../data/domainState'

type Overview = {
  counts: Record<string, number>
  current_release: string | null
  current_release_why: string
  rule_set_version: string | null
  onto_version: string | null
  scope_note: string
}

const ov = ref<Overview | null>(null)
const busy = ref('')
const err = ref('')

async function loadOverview() {
  try {
    ov.value = await api.domainOverview() as Overview
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  }
}

async function pick(domainId: string, instanceId?: string | null) {
  if (busy.value) return
  busy.value = domainId + ':' + (instanceId || '')
  err.value = ''
  try {
    await setCurrent(domainId, instanceId)
    await loadOverview()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await load(true)
  await loadOverview()
})
</script>

<template>
  <h2>领域知识资产</h2>

  <div class="panel" style="margin-top: 16px">
    <h3>领域与实例</h3>

    <div v-if="loadErr" class="err">取不到领域列表：{{ loadErr }}</div>

    <div class="pickrow">
      <div class="pickcol">
        <div class="cap">领域</div>
        <button
          v-for="d in domains"
          :key="d.domain_id"
          class="pick"
          :class="{ on: current?.domain_id === d.domain_id }"
          :disabled="!d.enabled || !!busy"
          :title="d.note || ''"
          @click="pick(d.domain_id)"
        >
          {{ d.label }}
          <span class="n">{{ d.instances.length }} 实例</span>
        </button>
      </div>

      <div class="pickcol">
        <div class="cap">实例</div>
        <template v-for="d in domains" :key="d.domain_id">
          <template v-if="current?.domain_id === d.domain_id">
            <button
              v-for="i in d.instances"
              :key="i.instance_id"
              class="pick"
              :class="{ on: current?.instance_id === i.instance_id }"
              :disabled="!!busy"
              :title="i.note || ''"
              @click="pick(d.domain_id, i.instance_id)"
            >
              {{ i.label }}
              <span v-if="i.is_default" class="n">默认</span>
            </button>
            <!-- 没有实例时如实说，不显示一个空白区域让人以为没加载出来 -->
            <span v-if="!d.instances.length" class="muted" style="font-size: 13px">
              「{{ d.label }}」领域下还没有实例。它是占位：结构上支持多领域，
              但这个领域目前没有任何资产。
            </span>
          </template>
        </template>
      </div>
    </div>

    <div v-if="err" class="err">{{ err }}</div>

    <div v-if="current?.mismatch" class="err">
      当前实例不属于当前领域（库里数据不一致）。重新点一次实例即可修正。
    </div>

    <div class="note" style="margin-top: 10px">
      当前：<b>{{ current?.domain_label || '未选领域' }}</b>
      /
      <b>{{ current?.instance_label || '未选实例' }}</b>
      · 这个选择是<b>全局</b>的：存在库里，所有人共用一份，换浏览器也一样。
    </div>
  </div>

  <!-- 资产概览 -->
  <div v-if="ov" class="panel">
    <h3>当前实例的资产</h3>
    <div v-if="!isComplete(current)" class="note" style="margin-bottom: 8px">
      当前没有选中实例，下面的数字不代表任何一个实例。
    </div>
    <div class="stat-row">
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.doc_files }}</div>
        <div class="stat-lbl muted">制度文件</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.clauses }}</div>
        <div class="stat-lbl muted">条款</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.rules_total }}</div>
        <div class="stat-lbl muted">规则</div>
      </div>
      <div class="stat-box t-pass-bg">
        <div class="stat-n">{{ ov.counts.rules_executable }}</div>
        <div class="stat-lbl">执行集</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.onto_entities }}</div>
        <div class="stat-lbl muted">本体实体</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.onto_concepts }}</div>
        <div class="stat-lbl muted">本体概念</div>
      </div>
      <div class="stat-box">
        <div class="stat-n">{{ ov.counts.bills_real }}</div>
        <div class="stat-lbl muted">真实单据</div>
      </div>
    </div>

    <div class="note one-line">
      本体版本 <span class="mono">{{ ov.onto_version || '—' }}</span> ·
      生效规则集 <span class="mono">{{ ov.rule_set_version || '—' }}</span>
      （发布版本 <span class="mono">{{ ov.current_release || '—' }}</span>，
      {{ ov.current_release_why }}）·
      规则 {{ ov.counts.rules_total }} 条中可编译 {{ ov.counts.rules_compiled }}、
      进执行集 {{ ov.counts.rules_executable }}
    </div>

    <div class="note warn-note">
      <b>口径</b>：{{ ov.scope_note }}
    </div>
  </div>
</template>

<style scoped>
.pickrow {
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.pickcol {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cap {
  font-size: 12px;
  color: var(--muted);
  margin-right: 2px;
}
.pick {
  padding: 6px 14px;
  border: 1px solid var(--line, #e3e8ef);
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-size: 14px;
}
.pick.on {
  border-color: var(--brand, #1668dc);
  color: var(--brand, #1668dc);
  background: #f3f8ff;
  font-weight: 600;
}
.pick:disabled { opacity: .5; cursor: not-allowed; }
.pick .n {
  font-size: 11px;
  color: var(--muted);
  margin-left: 6px;
  font-weight: 400;
}
.stat-row { display: flex; gap: 8px; flex-wrap: wrap; }
.stat-box {
  flex: 1 1 0;
  min-width: 78px;
  max-width: 140px;
  padding: 7px 10px;
  border: 1px solid var(--line, #e3e8ef);
  border-radius: 8px;
  text-align: center;
}
.stat-n { font-size: 22px; font-weight: 700; line-height: 1.15; }
.stat-lbl { font-size: 11px; margin-top: 1px; white-space: nowrap; }
.t-pass-bg { border-color: var(--pass, #27ae60); }
.t-pass-bg .stat-n { color: var(--pass, #27ae60); }
.one-line { margin-top: 8px; line-height: 1.7; }
.warn-note { margin-top: 8px; }
.mono { font-family: monospace; font-size: 12px; }
</style>
