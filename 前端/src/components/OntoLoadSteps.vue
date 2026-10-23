<script setup lang="ts">
/**
 * 本体工场第②③步：装载本体源文件 / 装载规则资产。
 *
 * **为什么单独做成组件而不是写在页面里**：P1 的「本体工场」页要用它，
 * 现在先挂在业务本体的「建立」段上。写在页里就得写两遍，
 * 而两遍迟早不一样——尤其是「闸没过时按钮什么样」这种细节。
 *
 * 三条口径：
 *
 * 1. **闸不是前端判的。** 灰不灰、为什么灰，全部照抄后端 `precheck()`
 *    的结论（`/api/onto/load-assets/ready`）。前端自己判一遍的下场是
 *    两套规则，而且前端那套永远先过期。
 *
 * 2. **两档照显，不许折叠。** blocker 拦住、warning 只报不拦。
 *    warning 折进 blocker 会把能用的功能锁死（库里现在就有一条
 *    binding_node='OUT' 绑不上）；blocker 折进 warning 会让坏数据静默进库。
 *
 * 3. **灰着的按钮必须说得出为什么灰。** 不给理由的灰按钮和没有按钮一样挡路，
 *    所以理由直接显示在按钮下面，不做成悬停提示。
 */
import { onMounted, ref } from 'vue'
import { api } from '../api'
import type { OntoGate, OntoGateCheck } from '../api'

const emit = defineEmits<{ loaded: [] }>()

const busy = ref('')
const err = ref('')
const msg = ref('')
const gate = ref<OntoGate | null>(null)
const ontoNote = ref('')


async function loadGate() {
  try {
    gate.value = await api.ontoLoadAssetsReady()
  } catch (e) {
    // 闸取不到就当没过：**失败要落在保守的一侧**。
    // 当成过了去放行，等于网络抖一下就能绕过这道闸。
    gate.value = { ok: false, checks: [],
                   reason: '取不到装载前检查的结论：' + (e instanceof Error ? e.message : e) }
  }
}

async function run(tag: string, fn: () => Promise<void>) {
  busy.value = tag
  err.value = ''
  try {
    await fn()
  } catch (e) {
    err.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = ''
    await loadGate()
  }
}

/** 第②步。重装会先清掉同版本的旧投影再重建——和「只比对」是两件事，两个入口。 */
const loadOnto = () =>
  run('onto', async () => {
    const r = await api.ontoReload(false)
    ontoNote.value = r.note
    msg.value = '本体已装载：版本 ' + (r.version || '?')
      + '，源文件与库' + (r.in_sync ? '同步' : '**不同步**')
    emit('loaded')
  })

const checkOnto = () =>
  run('check', async () => {
    const r = await api.ontoReload(true)
    msg.value = r.in_sync ? '比对过了：源文件与库一致，不用重装'
      : '**源文件改了但没重装**：库里的 hash 对不上，点「装载本体源文件」'
  })

/** 第③步。闸没过时后端会回 409，这里按钮本来就是灰的，双保险。 */
const loadAssets = () =>
  run('assets', async () => {
    const r = await api.ontoLoadAssets()
    msg.value = '规则资产已装载：' + r.rules + ' 条规则（可编译 ' + r.compiled
      + ' / 演示可执行 ' + r.executable + '）· 标准限额 ' + r.standards
      + ' 行 · 新建草稿 ' + r.release
      + '；人采纳进来的 ' + r.adopted_kept + ' 条保留，当前生效发布未动'
    emit('loaded')
  })

function level(c: OntoGateCheck) {
  return c.ok ? 't-pass' : c.level === 'blocker' ? 't-hit' : 't-pend'
}
function levelText(c: OntoGateCheck) {
  return c.ok ? '过' : c.level === 'blocker' ? '拦' : '警'
}

onMounted(loadGate)
</script>

<template>
  <div class="panel">
    <h3>装载：② 本体源文件 → ③ 规则资产</h3>

    <div class="row">
      <button :disabled="!!busy" @click="loadOnto">
        {{ busy === 'onto' ? '装载中…' : '② 装载本体源文件' }}
      </button>
      <button :disabled="!!busy" @click="checkOnto">
        {{ busy === 'check' ? '比对中…' : '只比对指纹（不写库）' }}
      </button>
      <button :disabled="!!busy || !gate?.ok" @click="loadAssets">
        {{ busy === 'assets' ? '装载中…' : '③ 装载规则资产' }}
      </button>
    </div>

    <!-- 灰按钮的理由，直说，不做成悬停提示 -->
    <div v-if="gate && !gate.ok" class="note" style="margin-top: 8px">
      <b>③ 现在点不动</b>：<MarkText :text="gate.reason" />
    </div>

    <table v-if="gate?.checks.length" style="margin-top: 10px">
      <thead>
        <tr>
          <th style="width: 44px">档</th>
          <th style="width: 210px">装载前检查</th>
          <th>结论</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in gate.checks" :key="c.check">
          <td><span class="tag" :class="level(c)">{{ levelText(c) }}</span></td>
          <td>{{ c.check }}</td>
          <td style="font-size: 12px"><MarkText :text="c.detail" /></td>
        </tr>
      </tbody>
    </table>

    <div v-if="msg" class="note" style="margin-top: 8px">{{ msg }}</div>
    <div v-if="ontoNote" class="muted" style="font-size: 12px; margin-top: 4px">
      {{ ontoNote }}
    </div>
    <div v-if="err" class="err" style="margin-top: 8px">{{ err }}</div>

    <!-- 原来这里是装载口径四行（③会动什么 / 绝不动什么 / 闸 / 命令行）。
         2026-10-21 按要求从页面去掉。**后端没动**：/api/onto/load-assets/note
         仍然返回这四条，tests/test_asset_gate.py 还钉着「该说的要说到」。 -->
  </div>
</template>
