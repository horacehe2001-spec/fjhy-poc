<script setup lang="ts">
/**
 * 单据与附件。
 *
 * 2026-10-21 附件这一块改了：原来它只显示「附件类型 / 文件名 / 页数」三列，
 * 而 40 张真实单一条附件记录都没有，于是整块永远只显示一条红色的
 * 「附件清单 100% 为空（G-29）」。
 *
 * 实际上附件一直在交付物里——**内嵌在 xlsx 里的 PNG**（219 张，覆盖全部 40 张单），
 * 只是我们的 xlsx 轻解析只读 XML 文本，看不见 xl/media/ 下的图片。
 * 现在它们抽出来了，所以这一块要能**看到原图**：审核岗复核「这张发票对不对」
 * 靠的是看票面，不是看文件名。
 *
 * 两条口径必须写在页面上：
 *
 * 1. **附件类型是空的，而空表示「还没判」，不是「没有类型」。**
 *    从一张 PNG 判它是出差审批单还是住宿费发票必须读图（必要附件决策表 11 种
 *    类型里 5 种标着 ocr_needed）。所以 TR-D6-01（必要附件完整性）**仍然开不了**。
 *    不写清楚的话，「有附件了」会被当成「附件规则能跑了」。
 *
 * 2. **「申报张数」与「实际附件数」是两个指标，并列显示，谁也不覆盖谁。**
 *    实测 40 张真实单里 34 张两者不等（申报多 32、申报少 2）。
 *    那**不等于「申报不实」**：「张数」可能按纸质页数算、一张图可能是两页合并、
 *    申报数也可能含未扫描的原件。所以没有做成规则——在口径搞清楚之前，
 *    那会是一条 32/40 命中的误报机。
 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import Async from '../components/Async.vue'
import { api, type Attachment, type AttachmentClassifyItem } from '../api'

const route = useRoute()
const no = computed(() => String(route.params.no))

/** 展开看大图的那一张。附件是扫描件，缩略图上根本看不清票面。 */
const openAtt = ref<number | null>(null)
function toggle(id: number) {
  openAtt.value = openAtt.value === id ? null : id
}
const fileUrl = (id: number) => `/api/attachments/${id}/file`

function kb(n: number | null | undefined) {
  if (n == null) return '—'
  return n < 1024 ? `${n} B`
    : n < 1048576 ? `${(n / 1024).toFixed(1)} KB`
      : `${(n / 1048576).toFixed(1)} MB`
}

// ── 读图判类型 ────────────────────────────────────────────────────────────
// **真出网、真计费**，发出去的是真实发票与出差申请单。所以这里走
// 两段确认——第一下只是「上膛」，把要调几次模型摆出来；第二下才真跑。
// 这和 Extract.vue 的增量抽取、Uploader.vue 的上传确认是同一套纪律。
const classifyBusy = ref(false)
const classifyErr = ref('')
const classifyMsg = ref('')
/** 正在等第二次确认的目标：单条附件 id，或 'batch'（本单剩余全部）*/
const armed = ref<number | 'batch' | null>(null)
/** 最近一次分类结果，按 attachment_id 存，用来在表格里就地显示依据/备注 */
const lastResult = ref<Record<number, AttachmentClassifyItem>>({})

function untypedOf(atts: Attachment[]): Attachment[] {
  return atts.filter(a => a.source_kind === 'xlsx内嵌' && !a.attach_type)
}

async function classifyOne(id: number, reload: () => Promise<void>) {
  if (armed.value !== id) {
    armed.value = id            // 第一下只上膛
    return
  }
  armed.value = null
  classifyBusy.value = true
  classifyErr.value = ''
  try {
    const r = await api.attClassify({ attachment_ids: [id], limit: 1 })
    const it = r.items[0]
    if (it) lastResult.value = { ...lastResult.value, [id]: it }
    classifyMsg.value = it?.applied
      ? `判定为「${it.type_label}」（把握：${it.confidence}），已写入`
      : it?.ok
        ? `读图完成，但${it?.type_code ? `把握只有「${it.confidence}」` : '模型判不出'}，未写入——见下方依据`
        : `读图失败：${it?.message || '未知错误'}`
    await reload()
  } catch (e) {
    classifyErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    classifyBusy.value = false
  }
}

async function classifyBatch(atts: Attachment[], reload: () => Promise<void>) {
  const ids = untypedOf(atts).map(a => a.id)
  if (!ids.length) return
  if (armed.value !== 'batch') {
    armed.value = 'batch'
    return
  }
  armed.value = null
  classifyBusy.value = true
  classifyErr.value = ''
  try {
    const r = await api.attClassify({ attachment_ids: ids, limit: ids.length })
    for (const it of r.items) lastResult.value = { ...lastResult.value, [it.attachment_id]: it }
    classifyMsg.value = `本单读图 ${r.tried} 张：写入 ${r.applied} 条，`
      + `${r.tried - r.applied} 条未写入（判不出或把握不够，见表格里的依据列）`
    await reload()
  } catch (e) {
    classifyErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    classifyBusy.value = false
  }
}
</script>

<template>
  <Async :load="() => api.bill(no)" :deps="no">
    <template #default="{ data, reload }">
      <h2>
        {{ (data as any).bill_no }}
        <span v-if="(data as any).is_demo" class="tag t-demo">演示数据</span>
      </h2>
      <div class="sub">
        <router-link to="/review/bills">← 回工作台</router-link>
        ·
        <router-link :to="`/review/trace/${(data as any).bill_no}`">审计追溯</router-link>
      </div>

      <!-- AI审核结论 Banner -->
      <template v-if="(data as any).latest_review">
        <div class="review-banner"
             :class="{ 'banner-reject': (data as any).latest_review.conclusion === 'reject',
                       'banner-review': (data as any).latest_review.conclusion === 'review',
                       'banner-pass':   (data as any).latest_review.conclusion === 'pass' }">
          <div class="banner-left">
            <span class="banner-icon">
              {{ (data as any).latest_review.conclusion === 'reject' ? '✗'
               : (data as any).latest_review.conclusion === 'review' ? '⚠'
               : '✓' }}
            </span>
            <div>
              <div class="banner-title">
                AI 审核：{{
                  (data as any).latest_review.conclusion === 'reject' ? '不通过'
                : (data as any).latest_review.conclusion === 'review' ? '待复核'
                : '通过'
                }}
              </div>
              <div class="banner-sub muted">
                执行 {{ (data as any).latest_review.rule_executed ?? '—' }} 条规则 ·
                命中 {{ (data as any).latest_review.hit_count ?? 0 }} 条 ·
                待人工 {{ (data as any).latest_review.pending_count ?? 0 }} 条
              </div>
            </div>
          </div>
          <router-link v-if="(data as any).latest_review.run_id"
                       :to="`/review/report/${(data as any).latest_review.run_id}`"
                       class="banner-link">查看完整报告 →</router-link>
        </div>

        <!-- 违规制度表 -->
        <div v-if="(data as any).latest_review.hits?.length" class="panel panel-violations">
          <h3 style="color: var(--hit)">
            违规制度（{{ (data as any).latest_review.hits.length }} 条命中）
          </h3>
          <table>
            <thead>
              <tr>
                <th style="width: 120px">规则</th>
                <th style="width: 200px">制度名称</th>
                <th style="width: 60px">控制</th>
                <th>违规说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="h in (data as any).latest_review.hits" :key="h.rule_id">
                <td class="mono">
                  <router-link :to="`/asset/rules/${h.rule_id}`">{{ h.rule_id }}</router-link>
                </td>
                <td>{{ h.rule_name || '—' }}</td>
                <td>
                  <span class="tag" :class="h.control_type === '禁止' ? 't-hit' : 't-pend'">
                    {{ h.control_type }}
                  </span>
                </td>
                <td style="font-size: 13px">{{ h.message }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 待人工项 -->
        <div v-if="(data as any).latest_review.pending?.length" class="panel panel-pending">
          <h3 style="color: var(--pend)">
            待人工判断（{{ (data as any).latest_review.pending.length }} 条）
          </h3>
          <table>
            <thead>
              <tr>
                <th style="width: 120px">规则</th>
                <th style="width: 200px">制度名称</th>
                <th>待判断原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in (data as any).latest_review.pending" :key="p.rule_id">
                <td class="mono">
                  <router-link :to="`/asset/rules/${p.rule_id}`">{{ p.rule_id }}</router-link>
                </td>
                <td>{{ p.rule_name || '—' }}</td>
                <td class="muted" style="font-size: 13px">{{ p.pending_reason || p.message }}</td>
              </tr>
            </tbody>
          </table>
          <div class="note" style="font-size: 12px">
            「待人工」不等于「通过」，需审核岗人工判断后在报告页提交复核意见。
          </div>
        </div>
      </template>

      <div class="panel">
        <h3>单据</h3>
        <div class="kv">
          <b>单据类型</b><span>{{ (data as any).doc_type }}</span>
          <b>单据日期</b><span class="mono">{{ String((data as any).doc_date || '').slice(0, 19) }}</span>
          <b>业务类型</b><span>{{ (data as any).biz_type }}</span>
          <b>核算主体</b><span>{{ (data as any).org_name || '—' }}</span>
          <b>报账部门</b><span>{{ (data as any).claim_dept || '—' }}</span>
          <b>申请人职级</b><span>{{ (data as any).applicant_rank || '—' }}</span>
          <b>报账金额</b><span class="mono">{{ (data as any).amount }}</span>
          <b>摘要</b><span>{{ (data as any).summary }}</span>
          <b>补充说明</b>
          <span>
            <span v-if="(data as any).remark">{{ (data as any).remark }}</span>
            <span v-else class="tag t-pend">空</span>
          </span>
          <b>收款方</b><span class="mono">{{ (data as any).payee || '—' }}</span>
          <b>收款账号</b><span class="mono">{{ (data as any).payee_account || '—' }}</span>
        </div>
      </div>

      <div class="panel">
        <h3>费用明细（{{ (data as any).details.length }} 行）</h3>
        <table>
          <thead><tr><th>费用小类</th><th class="num">金额</th><th>分摊部门</th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in (data as any).details" :key="i">
              <td>{{ d.expense_item }}</td>
              <td class="num">{{ d.amount }}</td>
              <td>{{ d.alloc_dept || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="(data as any).travelers?.length" class="panel">
        <h3>出行人员（{{ (data as any).travelers.length }} 人）</h3>
        <table>
          <tbody>
            <tr v-for="(t, i) in (data as any).travelers" :key="i">
              <td class="mono" style="width: 140px">{{ t.emp_no }}</td>
              <td>{{ t.name }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>附件（{{ (data as any).attachments.length }} 个）</h3>

        <!-- 两个指标并列。**不要只显示一个**：哪个都不是「附件数」的唯一答案 -->
        <div class="stat" style="margin-bottom: 10px">
          <div>
            <b>{{ (data as any).attach_pages ?? '—' }}</b>
            <span>申报张数（审批意见 PDF）</span>
          </div>
          <div>
            <b>{{ (data as any).attach_count ?? 0 }}</b>
            <span>真实原图（xlsx 内嵌）</span>
          </div>
          <!-- 演示合成单独一格，只在有的时候出现。
               原来只摆一个「实际附件数」，演示单于是头部写「附件（2 个）」、
               指标卡写「0」，同一个页面两个数对不上。 -->
          <div v-if="(data as any).attach_demo_count">
            <b>{{ (data as any).attach_demo_count }}</b>
            <span>演示合成（示意图）</span>
          </div>
        </div>
        <div v-if="(data as any)._attach_metrics_note" class="note">
          <MarkText :text="(data as any)._attach_metrics_note" />
        </div>

        <!-- 没有附件时这条是红的（要去查）；有附件时它说的是「类型还没判」，
             那是说明不是故障，用 note 不用 err。 -->
        <div v-if="(data as any)._attachment_note"
             :class="(data as any).attachments.length ? 'note' : 'err'">
          <MarkText :text="(data as any)._attachment_note" />
        </div>

        <!-- 读图判类型。**真出网、真计费**——两段确认，第一下只是把要调几次
             模型摆出来。批量按钮只对**这一单**的未判附件生效，不是全库 219 张。 -->
        <div v-if="untypedOf((data as any).attachments as Attachment[]).length"
             class="row" style="margin: 8px 0">
          <span class="muted">
            本单还有 {{ untypedOf((data as any).attachments as Attachment[]).length }} 张未判类型
          </span>
          <button :disabled="classifyBusy"
                  @click="classifyBatch((data as any).attachments as Attachment[], reload)">
            {{ armed === 'batch'
              ? `确认：对这 ${untypedOf((data as any).attachments as Attachment[]).length} 张调模型（出网计费）`
              : '批量判类型' }}
          </button>
        </div>
        <div v-if="classifyErr" class="err">{{ classifyErr }}</div>
        <div v-if="classifyMsg" class="note">{{ classifyMsg }}</div>

        <table v-if="(data as any).attachments.length">
          <thead>
            <tr>
              <th class="num" style="width: 44px">序</th>
              <th style="width: 92px">预览</th>
              <th>文件名</th>
              <th style="width: 130px">附件类型</th>
              <th class="num" style="width: 80px">大小</th>
              <th style="width: 78px">来源</th>
              <th>出处</th>
              <th style="width: 150px">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(a, i) in (data as any).attachments as Attachment[]" :key="a.id ?? i">
              <tr>
                <td class="num">{{ a.seq ?? i + 1 }}</td>
                <td>
                  <img v-if="a.id && a.has_file" class="thumb"
                       :src="fileUrl(a.id)" :alt="a.file_name"
                       @click="toggle(a.id)" />
                  <span v-else class="muted" style="font-size: 11px">无原图</span>
                </td>
                <td>
                  <a v-if="a.id && a.has_file" href="#"
                     @click.prevent="toggle(a.id)">{{ a.file_name }}</a>
                  <span v-else>{{ a.file_name }}</span>
                </td>
                <td>
                  <!-- 空 = 还没判，**不是「没有类型」**。显示「未判定」而不是「—」：
                       「—」会被读成「这份附件没有类型」。 -->
                  <span v-if="a.attach_type" class="tag t-pass">{{ a.attach_type }}</span>
                  <span v-else class="tag t-pend" title="需要读图才能判，见下方说明">
                    未判定
                  </span>
                  <div v-if="a.type_source" class="muted" style="font-size: 11px">
                    {{ a.type_source }}{{ a.type_conf ? ` · 把握${a.type_conf}` : '' }}
                  </div>
                </td>
                <td class="num">{{ kb(a.byte_size) }}</td>
                <td>
                  <span class="tag" :class="a.source_kind === 'xlsx内嵌' ? 't-pass' : 't-demo'">
                    {{ a.source_kind }}
                  </span>
                </td>
                <td class="muted mono" style="font-size: 11px">{{ a.source_ref || '—' }}</td>
                <td>
                  <button v-if="a.source_kind === 'xlsx内嵌' && !a.attach_type"
                          :disabled="classifyBusy" class="mini"
                          @click="classifyOne(a.id, reload)">
                    {{ armed === a.id ? '确认调模型（计费）' : '判类型' }}
                  </button>
                  <span v-else class="muted" style="font-size: 11px">—</span>
                </td>
              </tr>
              <!-- 读图结果：判不出 / 把握不够 时依据必须看得见，
                   否则使用者不知道「为什么没写入类型」。 -->
              <tr v-if="lastResult[a.id]">
                <td colspan="8" class="muted" style="font-size: 11px; background: #fafcfe">
                  模型 {{ lastResult[a.id].model }} ·
                  {{ lastResult[a.id].ok
                    ? (lastResult[a.id].type_code
                        ? `判为 ${lastResult[a.id].type_label}（把握 ${lastResult[a.id].confidence}）`
                        : '判不出') + ' · 依据：' + (lastResult[a.id].reason || '—')
                    : `失败：${lastResult[a.id].message}` }}
                  <span v-if="lastResult[a.id].note && lastResult[a.id].note !== '无'">
                    · 备注：{{ lastResult[a.id].note }}
                  </span>
                </td>
              </tr>
              <!-- 大图。附件是扫描件，缩略图上看不清票面，而审核岗要看的正是票面 -->
              <tr v-if="openAtt === a.id">
                <td colspan="8" style="background: #fafcfe">
                  <img :src="fileUrl(a.id)" :alt="a.file_name" class="full" />
                  <div class="muted" style="font-size: 11px; margin-top: 6px">
                    {{ a.file_name }}（{{ kb(a.byte_size) }}）·
                    <a :href="fileUrl(a.id)" target="_blank" rel="noopener">在新标签页打开</a>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>审批记录（{{ (data as any).approvals.length }} 条）</h3>
        <table>
          <thead>
            <tr><th class="num">序</th><th>节点</th><th>审批人</th><th>意见</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="(a, i) in (data as any).approvals" :key="i">
              <td class="num">{{ a.seq }}</td>
              <td>
                {{ a.node }}
                <span v-if="a.node === 'AI审核'" class="tag t-na">AI</span>
              </td>
              <td class="mono">{{ a.approver }}</td>
              <td>{{ a.opinion || '' }}</td>
              <td class="mono">{{ String(a.approved_at || '').slice(0, 19) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <Async :load="() => api.billReviews(no)" :deps="no">
        <template #default="{ data: rv }">
          <div class="panel">
            <h3>历次审核</h3>
            <table>
              <thead>
                <tr>
                  <th>审核号</th><th>结论</th><th class="num">命中</th><th class="num">待人工</th>
                  <th class="num">执行/候选</th><th>规则集</th><th>时间</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in (rv as any).items" :key="r.run_id">
                  <td class="mono">
                    <router-link :to="`/review/report/${r.run_id}`">{{ r.run_id }}</router-link>
                  </td>
                  <td class="mono">{{ r.conclusion }}</td>
                  <td class="num">{{ r.hit_count }}</td>
                  <td class="num">{{ r.pending_count }}</td>
                  <td class="num">{{ r.rule_executed }} / {{ r.rule_candidates }}</td>
                  <td class="mono">{{ r.rule_set_version }}</td>
                  <td class="mono">{{ String(r.finished_at || '').slice(0, 19) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </Async>
    </template>
  </Async>
</template>

<style scoped>
.review-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-radius: 8px;
  margin-bottom: 12px;
  border-width: 1px;
  border-style: solid;
}
.banner-reject { background: #fff1f0; border-color: #ffa39e; }
.banner-review { background: #fffbe6; border-color: #ffe58f; }
.banner-pass   { background: #f6ffed; border-color: #b7eb8f; }
.banner-left { display: flex; align-items: center; gap: 14px; }
.banner-icon {
  font-size: 28px;
  line-height: 1;
  font-weight: 700;
}
.banner-reject .banner-icon { color: var(--hit, #c0392b); }
.banner-review .banner-icon { color: var(--pend, #d48806); }
.banner-pass   .banner-icon { color: var(--pass, #27ae60); }
.banner-title { font-size: 16px; font-weight: 700; }
.banner-reject .banner-title { color: var(--hit, #c0392b); }
.banner-review .banner-title { color: var(--pend, #d48806); }
.banner-pass   .banner-title { color: var(--pass, #27ae60); }
.banner-sub { font-size: 12px; margin-top: 2px; }
.banner-link { font-size: 13px; color: var(--brand, #1668dc); white-space: nowrap; }
.panel-violations { border-left: 3px solid var(--hit, #c0392b); }
.panel-pending    { border-left: 3px solid var(--pend, #d48806); }

/* 附件缩略图。扫描件在 72px 上只能看出「是张票还是张表」，
   看票面必须点开——所以缩略图给个明显的可点样式。 */
.thumb {
  width: 72px; height: 54px; object-fit: cover; cursor: pointer;
  border: 1px solid var(--line); border-radius: var(--radius); background: #fff;
}
.thumb:hover { border-color: var(--brand-light); }
/* 大图不设高度上限：出差申请单那种是整表一张长图，截断了等于没给。
   宽度跟随容器，窄屏上也能看全。 */
.full {
  max-width: 100%; border: 1px solid var(--line);
  border-radius: var(--radius); background: #fff;
}
</style>
