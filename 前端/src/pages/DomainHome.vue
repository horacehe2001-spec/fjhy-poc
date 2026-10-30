<script setup lang="ts">
/**
 * 领域知识资产 · 落地页。点菜单组名进来的那一页。
 *
 * 两件事：
 *   ① 选领域、选实例，以及**添加实例**。选择是**全局单例**（库里一行，
 *      所有人共用，换浏览器也一样），不是存在本地的偏好。
 *   ② 显示当前实例的资产概览，让人一眼看出「现在这套资产有多大」。
 *
 * **口径变了，这段话跟着改**：原来写的是「本期只做归属+展示，接口尚未按实例
 * 过滤，所以下面是全库数字」。2026-10-29 审核领域加了第二个实例（822港航）之后
 * 做了真实隔离（迁移 0040/0041 + 各接口加 WHERE instance_id），
 * 所以**制度文件/条款/规则/单据这几个数现在是当前实例的数**。
 * 唯一还没隔离的是本体（ontology_* 表还没有 instance_id，两个实例共用同一份
 * 本体投影，因为本体的源头是单一文件 本体_v0.1.yaml）——
 * 这一条由接口的 scope_note 原样说出来，页面不替它圆场。
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

// ── 添加实例 ────────────────────────────────────────────────────────────────
// 加在哪个领域下：**用当前选中的领域，不给用户再选一次**。这一页上领域已经是
// 选中状态了，再出一个下拉让他选一遍，两处不一致时谁说得清建到哪去了。
const adding = ref(false)
const newLabel = ref('')
const newNote = ref('')
const addedTip = ref('')

async function submitInstance() {
  const did = current.value?.domain_id
  if (!did || !newLabel.value.trim() || busy.value) return
  busy.value = 'add'
  err.value = ''
  addedTip.value = ''
  try {
    const r = await api.domainAddInstance(did, newLabel.value.trim(),
                                          newNote.value.trim() || undefined)
    // 重新拉领域树：新实例要出现在上面的实例列表里。
    // **不自动切过去**（后端也不切），所以概览数字不动——那是对的，
    // 当前实例没变。提示里把这件事说明白，否则用户会以为"建了但没生效"。
    await load(true)
    adding.value = false
    newLabel.value = ''
    newNote.value = ''
    addedTip.value = `已建好「${r.label}」（${r.instance_id}）。` +
      '它里面是空的：制度文件 0、规则 0、发布版本 0。' +
      '当前实例**没有**自动切过去——要用它，点一下上面的实例按钮。'
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
              「{{ d.label }}」领域下还没有实例。点下面的「添加实例」建一个。
            </span>
          </template>
        </template>

        <!-- 添加实例：只往**当前选中的领域**里加，不另给一个领域下拉 -->
        <button v-if="!adding && current?.domain_id" class="pick add"
                :disabled="!!busy" @click="adding = true">+ 添加实例</button>
        <div v-if="adding" class="addbox">
          <input v-model="newLabel" maxlength="40" placeholder="实例名称，如 822港航"
                 @keyup.enter="submitInstance" />
          <input v-model="newNote" maxlength="200" placeholder="备注（可不填）" />
          <div class="addact">
            <button class="pick" :disabled="!newLabel.trim() || !!busy"
                    @click="submitInstance">建到「{{ current?.domain_label }}」下</button>
            <button class="pick" :disabled="!!busy"
                    @click="adding = false; newLabel = ''; newNote = ''">取消</button>
          </div>
          <div class="muted" style="font-size: 12px">
            新实例**是空的**：制度文件、规则、本体、发布版本都为 0，
            需要切过去之后自己上传制度文件、走抽取与采纳建立。不会克隆现有实例的资产。
          </div>
        </div>
      </div>
    </div>

    <div v-if="addedTip" class="note" style="margin-top: 10px">{{ addedTip }}</div>

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
/* 「添加实例」是建东西的动作，和「选哪个实例」不是一类，所以用虚线框区分开：
   实线=切换（会影响所有人看到的数据），虚线=新建（不影响当前选择）。 */
.pick.add {
  border-style: dashed;
  color: var(--muted);
}
.addbox {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px dashed var(--line, #e3e8ef);
  border-radius: 6px;
  max-width: 340px;
}
.addbox input {
  padding: 6px 10px;
  border: 1px solid var(--line, #e3e8ef);
  border-radius: 4px;
  font-size: 14px;
}
.addact { display: flex; gap: 6px; }
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
