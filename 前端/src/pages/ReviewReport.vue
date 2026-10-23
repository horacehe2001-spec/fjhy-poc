<script setup lang="ts">
/**
 * AI 审核报告 + 人工复核闸门。
 *
 * 这一页的设计要点，每一条都是为了防止「以为查过了」：
 *
 * 1. **覆盖面四个数并列**：候选 / 执行 / 命中 / 待人工 / 不适用 / 未执行。
 * 2. **被剔除的规则单列一节**，不藏在折叠里。
 * 3. **「待人工」用警示色**，不能长得像「通过」。
 * 4. **证据要能让人自己复核一遍**：实际值、标准值、出处条款都显示。
 * 5. **关键词类规则回显命中的原文片段**——命中「黄山」的单子，
 *    看到「福州黄山路XX酒店」就立刻知道是路名，三秒结束。
 * 6. **AI 结论标注为参考**，人工复核是真正的闸门。
 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import Async from '../components/Async.vue'
import Outcome from '../components/Outcome.vue'
import { api, type Finding, type ReviewReport } from '../api'

const route = useRoute()
const runId = computed(() => String(route.params.runId))

const decision = ref<'agree' | 'return' | 'agree_with_note'>('agree')
const reviewer = ref('')
const reason = ref('')
const disagree = ref<string[]>([])
const submitting = ref(false)
const submitMsg = ref('')
const submitErr = ref('')

async function submit(reload: () => void) {
  submitErr.value = ''
  submitMsg.value = ''
  if (decision.value === 'return' && !reason.value.trim()) {
    submitErr.value = '退回必须给理由——退回理由是误报/漏报回流的起点'
    return
  }
  submitting.value = true
  try {
    await api.submitManual(runId.value, {
      decision: decision.value,
      reviewer: reviewer.value || '未署名',
      return_reason: reason.value || undefined,
      disagree_with: disagree.value,
    })
    submitMsg.value = '已记录。不认可的命中会回流成 false_positive。'
    reload()
  } catch (e) {
    submitErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    submitting.value = false
  }
}

function toggle(rid: string) {
  const i = disagree.value.indexOf(rid)
  if (i >= 0) disagree.value.splice(i, 1)
  else disagree.value.push(rid)
}

function evidenceRows(f: Finding): [string, string][] {
  const out: [string, string][] = []
  for (const [k, v] of Object.entries(f.evidence || {})) {
    if (k === 'not_applicable') continue
    out.push([k, typeof v === 'object' ? JSON.stringify(v) : String(v)])
  }
  return out
}
</script>

<template>
  <Async :load="() => api.review(runId) as Promise<ReviewReport>" :deps="runId">
    <template #default="{ data, reload }">
      <h2>审核报告</h2>
      <div class="sub">
        <code>{{ data.run.bill_no }}</code>
        ·
        <router-link :to="`/review/bills/${data.run.bill_no}`">看单据</router-link>
        ·
        <router-link :to="`/review/trace/${data.run.bill_no}`">看审计追溯</router-link>
      </div>

      <!-- 人工复核闸门。**放在页首，不放页底。**
           它原先排在「结论 / 命中 / 待人工 / 未执行 / 通过折叠」五块之后，
           要滚过整页才看得见——而这页存在的理由就是让人做这个决定。
           AI 结论和两个计数一并显示在面板里：不先滚下去也知道在审什么。

           勾选「我不认可这条命中」**仍留在命中区每条证据旁边**，没有一起搬上来：
           那个勾选必须挨着证据看，搬到顶上就变成凭记忆勾。两处靠下面的
           已勾计数和锚点链接连起来。 -->
      <div class="panel panel-gate" id="gate">
        <h3>
          人工复核（这里才是闸门）
          <span class="muted" style="font-size: 12px; font-weight: normal">
            · AI 结论
            <span
              class="tag"
              :class="{ pass: 't-pass', review: 't-pend', reject: 't-hit' }[data.run.conclusion] || 't-na'"
            >{{ { pass: '通过', review: '待复核', reject: '不通过' }[data.run.conclusion] }}</span>
            仅供参考 · 命中 {{ data.coverage.hit }} · 待人工 {{ data.coverage.pending }}
          </span>
        </h3>
        <div class="row">
          <input v-model="reviewer" placeholder="复核人" style="width: 140px" />
          <select v-model="decision">
            <option value="agree">同意</option>
            <option value="agree_with_note">同意并备注</option>
            <option value="return">退回</option>
          </select>
          <input
            v-model="reason"
            :placeholder="decision === 'return' ? '退回理由（必填）' : '备注（可选）'"
            style="flex: 1; min-width: 240px"
          />
          <button :disabled="submitting" @click="submit(reload)">
            {{ submitting ? '提交中…' : '提交' }}
          </button>
        </div>
        <div v-if="disagree.length" class="note">
          标记为「不认可」的命中：<code>{{ disagree.join(', ') }}</code>
          —— 提交后会回流成 feedback_log(false_positive)，用于量化误报。
        </div>
        <div v-else-if="data.findings.hit.length" class="note" style="font-size: 12px">
          有 {{ data.findings.hit.length }} 条命中。觉得哪条是误报，在
          <a href="#hits">下方命中区</a>逐条勾「我不认可」，再回这里提交——
          勾选挨着证据，不凭记忆。
        </div>
        <div v-if="submitErr" class="err">{{ submitErr }}</div>
        <div v-if="submitMsg" class="note">{{ submitMsg }}</div>
      </div>

      <div class="panel">
        <h3>
          结论
          <span
            class="tag"
            :class="{ pass: 't-pass', review: 't-pend', reject: 't-hit' }[data.run.conclusion] || 't-na'"
            style="margin-left: 6px"
          >
            {{ { pass: '通过', review: '待复核', reject: '不通过' }[data.run.conclusion] }}
          </span>
        </h3>
        <div class="note">
          <b>这个结论是参考，不阻断流程。</b><MarkText :text="data.ai_is_advisory.why" />
        </div>
        <div class="stat">
          <div><b>{{ data.coverage.candidates }}</b><span>候选规则</span></div>
          <div><b style="color: var(--accent)">{{ data.coverage.executed }}</b><span>实际执行</span></div>
          <div><b style="color: var(--hit)">{{ data.coverage.hit }}</b><span>命中</span></div>
          <div><b style="color: var(--pend)">{{ data.coverage.pending }}</b><span>待人工</span></div>
          <div><b style="color: var(--pass)">{{ data.coverage.passed }}</b><span>通过</span></div>
          <div><b style="color: var(--na)">{{ data.coverage.not_applicable }}</b><span>不适用</span></div>
          <div><b style="color: var(--muted)">{{ data.coverage.excluded }}</b><span>未执行</span></div>
        </div>
        <div class="note"><MarkText :text="data.coverage.note" /></div>
      </div>

      <div v-if="data.findings.hit.length" class="panel" id="hits">
        <h3 style="color: var(--hit)">命中（{{ data.findings.hit.length }} 条）</h3>
        <div v-for="f in data.findings.hit" :key="f.rule_id" style="margin-bottom: 14px">
          <div>
            <router-link :to="`/asset/rules/${f.rule_id}`" class="mono">{{ f.rule_id }}</router-link>
            <Outcome :outcome="f.outcome" :control-type="f.control_type" />
            <span v-if="f.needs_human" class="tag t-pend" style="margin-left: 4px">需人工复核</span>
          </div>
          <div style="margin: 4px 0">{{ f.message }}</div>
          <div v-if="f.matched_text" class="note" style="margin: 4px 0">
            命中原文：<code>{{ f.matched_text }}</code>
            <div class="muted" style="font-size: 12px">
              回显原文是为了让你三秒判断是不是误报——比如命中「黄山」而原文是「黄山路XX酒店」。
            </div>
          </div>
          <table v-if="evidenceRows(f).length" style="margin-top: 4px">
            <tbody>
              <tr v-for="([k, v], i) in evidenceRows(f)" :key="i">
                <td style="width: 190px; color: var(--muted)">{{ k }}</td>
                <td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
          <label style="display: block; margin-top: 5px">
            <input type="checkbox" :checked="disagree.includes(f.rule_id)" @change="toggle(f.rule_id)" />
            我不认可这条命中（会回流成误报）
          </label>
        </div>
        <!-- 勾完要能回去提交。闸门在页首，勾选在这里，不给这条链接
             就得自己往上滚——勾了几条也看不见。 -->
        <div v-if="disagree.length" class="note">
          已勾 <b>{{ disagree.length }}</b> 条不认可 ·
          <a href="#gate">回到页首提交 ↑</a>
        </div>
      </div>

      <div v-if="data.findings.pending.length" class="panel">
        <h3 style="color: var(--pend)">待人工（{{ data.findings.pending.length }} 条）</h3>
        <div class="note">
          <b>「待人工」不等于「通过」。</b>
          这些条目是判不了，不是查过了没问题。把它们算进通过，
          就等于让系统声称复核了没复核的东西。
        </div>
        <div v-for="f in data.findings.pending" :key="f.rule_id" style="margin-bottom: 12px">
          <div>
            <router-link :to="`/asset/rules/${f.rule_id}`" class="mono">{{ f.rule_id }}</router-link>
            <Outcome :outcome="f.outcome" />
          </div>
          <div class="muted" style="margin: 3px 0"><MarkText :text="f.pending_reason || f.message" /></div>
          <table v-if="evidenceRows(f).length">
            <tbody>
              <tr v-for="([k, v], i) in evidenceRows(f)" :key="i">
                <td style="width: 190px; color: var(--muted)">{{ k }}</td>
                <td>{{ v }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3 style="color: var(--muted)">未执行的规则（{{ data.excluded_rules.length }} 条）</h3>
        <div class="err"><MarkText :text="data.excluded_note" /></div>
        <table>
          <thead>
            <tr><th style="width: 110px">规则</th><th>名称</th><th style="width: 52px">控制</th><th>为什么没跑</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in data.excluded_rules" :key="e.rule_id">
              <td class="mono">
                <router-link :to="`/asset/rules/${e.rule_id}`">{{ e.rule_id }}</router-link>
              </td>
              <td>{{ e.rule_name }}</td>
              <td>
                <span class="tag" :class="e.control_type === '禁止' ? 't-block' : 't-warn'">
                  {{ e.control_type }}
                </span>
              </td>
              <td class="muted" style="font-size: 12px"><MarkText :text="e.why" /></td>
            </tr>
          </tbody>
        </table>
      </div>

      <details class="panel">
        <summary style="cursor: pointer">
          通过 {{ data.findings.passed.length }} 条 ·
          不适用 {{ data.findings.not_applicable.length }} 条（展开）
        </summary>
        <table style="margin-top: 8px">
          <tbody>
            <tr v-for="f in data.findings.passed" :key="'p' + f.rule_id">
              <td class="mono" style="width: 110px">{{ f.rule_id }}</td>
              <td><Outcome :outcome="f.outcome" /></td>
              <td>{{ f.message }}</td>
            </tr>
            <tr v-for="f in data.findings.not_applicable" :key="'n' + f.rule_id">
              <td class="mono" style="width: 110px">{{ f.rule_id }}</td>
              <td><Outcome outcome="pass" not-applicable /></td>
              <td class="muted">{{ f.message }}</td>
            </tr>
          </tbody>
        </table>
      </details>

    </template>
  </Async>
</template>

<style scoped>
/* 人工复核面板。左侧一条强调边，和 BillDetail 的 .panel-violations /
   .panel-pending 同一套写法——这页五个 panel 长得一样，不给它一条边，
   移到页首也还是会被当成「又一块说明文字」滑过去。
   用品牌色而不是警示色：它是**要你做的动作**，不是一条警告。 */
.panel-gate {
  border-left: 3px solid var(--brand, #1668dc);
}
</style>
