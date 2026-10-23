<script setup lang="ts">
/**
 * 栏目五 · 发布管理。四段：工作区 / 版本 / 消费 / 存储。
 *
 * 用户 2026-10-19 的原话：
 *   「发布管理，就是当前的 文件 → 规则 → 三元本体 以版本的形式资产化。
 *     并允许被其他业务消费。例如 API、本体等」
 *
 * 四条口径由用户定，页面按它排：
 *   ① **三元本体 = 业务 / 规则 / 流程**（不是 RDF 三元组）
 *   ② **快照冻结**，不是只记版本指针。v1.0 永远是 v1.0
 *   ③ **只读 API + 可下载资产包**
 *   ④ **装载只动工作区**，已发布版本与当前指针不受影响
 *
 * **发布必须有人签字。** 规则集一变，所有在跑的单据的判定口径就跟着变。
 * 库里有 CHECK 约束 ck_release_signed 钉着：reviewed_by 为空不允许进 published。
 * 而且**发布与生效是两件事**：签字只是入库，还要再切一次指针才生效。
 */
import { computed, onMounted, ref } from 'vue'
import Async from '../components/Async.vue'
import { api } from '../api'
import type { PubIndex, RelAccess, RelDetail, RelRow, RelWorkspace, StorageRow } from '../api'

type Tab = 'ws' | 'rel' | 'consume' | 'store'
const tab = ref<Tab>('ws')

const ws = ref<RelWorkspace | null>(null)
const rows = ref<RelRow[]>([])
const policy = ref<Record<string, string>>({})
const detail = ref<RelDetail | null>(null)
const openId = ref('')
const pay = ref<{ layer: string; text: string; sha: string } | null>(null)
const idx = ref<PubIndex | null>(null)
const log = ref<RelAccess[]>([])
const store = ref<{ items: StorageRow[]; note: string } | null>(null)

const who = ref('')
const version = ref('')
const note = ref('')
const reason = ref('')
const rbTarget = ref('')
const busy = ref('')
const err = ref('')
const msg = ref('')

const ST: Record<string, { label: string; cls: string }> = {
  draft: { label: '草稿', cls: 't-na' },
  reviewing: { label: '审阅中', cls: 't-pend' },
  published: { label: '已发布', cls: 't-pass' },
  rolled_back: { label: '已回滚', cls: 't-hit' },
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
  }
}

const loadWs = () =>
  run('ws', async () => {
    ws.value = await api.relWorkspace()
    if (!version.value) version.value = ws.value.suggest_version
  })

const loadRel = () =>
  run('rel', async () => {
    const r = await api.relReleases()
    rows.value = r.items
    policy.value = r.policy
  })

function pickTab(t: Tab) {
  tab.value = t
  if (t === 'ws' && !ws.value) loadWs()
  if (t === 'rel' && !rows.value.length) loadRel()
  if (t === 'consume') {
    if (!idx.value) run('idx', async () => { idx.value = await api.pubIndex() })
    run('log', async () => { log.value = (await api.relAccessLog(50)).items })
    if (!rows.value.length) loadRel()
  }
  if (t === 'store' && !store.value)
    run('st', async () => { store.value = await api.relStorage() })
}

const canFreeze = computed(() => !!ws.value && ws.value.gates.every((g) => g.ok))
const published = computed(() => rows.value.filter((r) => r.status === 'published'))
const current = computed(() => rows.value.find((r) => r.is_current) || null)

const doFreeze = () =>
  run('freeze', async () => {
    const r = await api.relFreeze({ version: version.value, by: who.value, note: note.value })
    msg.value = `已冻结 ${r.release_id}（${r.rule_set_version}）：${r.layers} 层，`
      + `包级校验 ${r.bundle_sha256.slice(0, 12)}…；`
      + `差异 新增 ${r.diff.added} / 改 ${r.diff.modified} / 退 ${r.diff.retired}`
    version.value = ''
    note.value = ''
    await Promise.all([loadWs(), loadRel()])
    tab.value = 'rel'
  })

const act = (label: string, fn: () => Promise<unknown>) =>
  run('act', async () => {
    const r = (await fn()) as { note?: string }
    msg.value = label + (r?.note ? '：' + r.note : '')
    await Promise.all([loadRel(), loadWs()])
    if (openId.value) detail.value = await api.relRelease(openId.value)
  })

const openDetail = (id: string) =>
  run('d', async () => {
    openId.value = openId.value === id ? '' : id
    detail.value = null
    pay.value = null
    detail.value = openId.value ? await api.relRelease(openId.value) : null
  })

const showPayload = (id: string, layer: string) =>
  run('p', async () => {
    const r = await api.relAsset(id, layer)
    const text = JSON.stringify(r.payload, null, 2)
    pay.value = {
      layer,
      sha: r.content_sha256,
      // 快照可以很大（规则层 82 条全字段）。只给前 20000 字，剩下的走接口或资产包。
      text: text.length > 20000 ? text.slice(0, 20000) + '\n…（截断，完整内容走接口或资产包）'
        : text,
    }
  })

const CHANGE: Record<string, string> = {
  added: '新增', modified: '修改', retired: '退役', unchanged: '未变',
}

onMounted(() => loadWs())
</script>

<template>
  <h2>发布管理</h2>
  <div class="row">
    <button :class="{ on: tab === 'ws' }" @click="pickTab('ws')">工作区</button>
    <button :class="{ on: tab === 'rel' }" @click="pickTab('rel')">版本</button>
    <button :class="{ on: tab === 'consume' }" @click="pickTab('consume')">消费</button>
    <button :class="{ on: tab === 'store' }" @click="pickTab('store')">存储</button>
    <span class="muted" style="margin-left: auto">
      <span v-if="ws?.current" :title="ws.current.why">
        引擎在用 <code>{{ ws.current.rule_set_version }}</code>
        <span class="tag" :class="ws.current.is_pointer ? 't-pass' : 't-pend'"
              style="margin-left:4px">{{ ws.current.is_pointer ? '已指定' : '自动' }}</span>
      </span>
      <span v-else class="tag t-pend">库里一版都没有</span>
    </span>
  </div>

  <div v-if="err" class="err">{{ err }}</div>
  <div v-if="msg" class="ok-note">{{ msg }}</div>

  <!-- ① 工作区 --------------------------------------------------------------->
  <template v-if="tab === 'ws'">
    <div v-if="ws" class="panel">
      <h3>现在能发什么</h3>
      <div class="sub"><MarkText :text="ws.note" /></div>
      <table>
        <thead>
          <tr>
            <th style="width: 30px">#</th><th style="width: 110px">层</th>
            <th style="width: 58px">条目</th><th style="width: 56px">来源</th>
            <th style="width: 150px">出处</th>
            <th style="width: 112px">内容校验</th>
            <th>这一层是什么</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in ws.layers" :key="l.layer" :class="{ tri: l.is_triple }">
            <td class="muted">{{ l.ord }}</td>
            <td>
              <b>{{ l.layer }}</b>
              <span v-if="l.is_triple" class="tag t-pass" style="margin-left: 4px">三元</span>
            </td>
            <td class="num">{{ l.item_count }}</td>
            <td class="muted" style="font-size: 12px">{{ l.source_kind }}</td>
            <td class="mono" style="font-size: 11px">{{ l.source_ref }}</td>
            <td class="mono" style="font-size: 11px">{{ l.content_sha256.slice(0, 12) }}…</td>
            <td style="font-size: 12px"><MarkText :text="l.note" /></td>
          </tr>
        </tbody>
      </table>
      <div class="kv" style="margin-top: 8px">
        <b>包级校验</b>
        <span class="mono">{{ ws.bundle_sha256 }}
          <span class="muted" style="font-size: 12px">
            （各层校验值按层名排序后再算一次；<b>不是 zip 文件的哈希</b>——
            zip 里有时间戳，同样内容两次打包字节不同）
          </span>
        </span>
        <b>与当前生效版本的差异</b>
        <span>
          <template v-if="ws.diff.base">
            规则 新增 <b>{{ ws.diff.added }}</b> / 修改 <b>{{ ws.diff.modified }}</b>
            / 退役 <b>{{ ws.diff.retired }}</b> / 未变 {{ ws.diff.unchanged }}
            <span v-if="ws.diff.layers_changed.length" class="muted">
              · 变了的层：{{ ws.diff.layers_changed.join('、') }}
            </span>
            <span v-else class="muted">· 九层都没变，这一版和生效版内容相同</span>
          </template>
          <template v-else>
            <span class="tag t-pend">没有可比的已发布版本</span>
            <span class="muted" style="margin-left: 6px"><MarkText :text="ws.diff.note" /></span>
          </template>
        </span>
      </div>
    </div>

    <div v-if="ws" class="panel">
      <h3>冻结为新版本</h3>
      <table class="gates">
        <tbody>
          <tr v-for="g in ws.gates" :key="g.gate">
            <td style="width: 150px"><b>{{ g.gate }}</b></td>
            <td style="width: 60px">
              <span class="tag" :class="g.ok ? 't-pass' : 't-hit'">{{ g.ok ? '过' : '挡住' }}</span>
            </td>
            <td class="mono" style="width: 230px; font-size: 11px"><MarkText :text="g.detail" /></td>
            <td style="font-size: 12px" :class="{ muted: g.ok }">{{ g.how }}</td>
          </tr>
        </tbody>
      </table>
      <div class="row" style="margin-top: 8px">
        <input v-model="version" placeholder="版本号" style="width: 170px" />
        <input v-model="who" placeholder="冻结人署名（必填）" style="width: 150px" />
        <input v-model="note" placeholder="发布说明：为什么发这一版" style="flex: 1" />
        <button :disabled="!!busy || !canFreeze || !version || !who" @click="doFreeze">
          冻结为新版本
        </button>
      </div>
    </div>
  </template>

  <!-- ② 版本 ----------------------------------------------------------------->
  <template v-if="tab === 'rel'">
    <div class="panel">
      <h3>版本（{{ rows.length }}）</h3>
      <!-- 「哪一版在生效」要写成一句话。只靠表格里的高亮，没设指针时
           看不出高亮是「有人指定的」还是「自动认的」。 -->
      <div v-if="ws?.current" class="muted" style="margin-bottom:6px">
        规则引擎正在用 <code>{{ ws.current.rule_set_version }}</code>
        （{{ ws.current.release_id }}）—— {{ ws.current.why }}
      </div>
      <div class="row">
        <input v-model="who" placeholder="操作人署名（选填，只记在痕迹里）" style="width: 150px" />
      </div>
      <table>
        <thead>
          <tr>
            <th style="width: 76px">发布号</th><th style="width: 128px">版本号</th>
            <th style="width: 62px">状态</th><th style="width: 40px">层</th>
            <th class="num" style="width: 44px">规则</th>
            <th class="num" style="width: 52px">可编译</th>
            <th style="width: 92px">签字人</th>
            <th style="width: 50px">被拉取</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="r in rows" :key="r.release_id">
            <tr :class="{ cur: r.is_effective }">
              <td class="mono">
                <a href="#" @click.prevent="openDetail(r.release_id)">{{ r.release_id }}</a>
              </td>
              <td class="mono">
                {{ r.rule_set_version }}
                <span v-if="r.is_current" class="tag t-pass" style="margin-left:4px">生效中</span>
                <span v-else-if="r.is_effective" class="tag t-pend" style="margin-left:4px">生效中（自动）</span>
              </td>
              <td><span class="tag" :class="ST[r.status]?.cls">{{ ST[r.status]?.label }}</span></td>
              <td class="num" :class="{ warn: !r.layers }">{{ r.layers || '—' }}</td>
              <td class="num">{{ r.rule_count }}</td>
              <td class="num">{{ r.compiled_count }}</td>
              <td>
                <span v-if="r.reviewed_by">{{ r.reviewed_by }}</span>
                <span v-else class="tag t-pend">未签字</span>
              </td>
              <td class="num">{{ r.accesses || '—' }}</td>
              <td class="acts">
                <template v-if="r.status === 'draft'">
                  <button v-if="r.layers" class="mini" :disabled="!!busy || !who"
                          @click="act('已送审', () => api.relReview(r.release_id, who))">送审</button>
                  <button v-if="r.layers" class="mini" :disabled="!!busy || !who"
                          @click="act('已签字发布', () => api.relPublish(r.release_id, who))">
                    签字发布</button>
                  <button class="mini" :disabled="!!busy || !who"
                          @click="act('已删草稿', () => api.relDiscard(r.release_id, who))">删草稿</button>
                  <span v-if="!r.layers" class="muted" style="font-size: 11px">
                    没冻内容（装载脚本建的工作区草稿）
                  </span>
                </template>
                <template v-else-if="r.status === 'reviewing'">
                  <button class="mini" :disabled="!!busy || !who"
                          @click="act('已签字发布', () => api.relPublish(r.release_id, who))">
                    签字发布</button>
                </template>
                <template v-else-if="r.status === 'published'">
                  <button v-if="!r.is_current" class="mini" :disabled="!!busy"
                          @click="act('已切为当前', () => api.relSwitch(r.release_id, who))">
                    切为当前</button>
                  <a class="mini dl" :href="api.pubBundleUrl(r.rule_set_version)">下载资产包</a>
                </template>
                <span v-else class="muted" style="font-size: 11px">
                  <MarkText :text="r.rollback_reason" />
                </span>
              </td>
            </tr>
            <tr v-if="openId === r.release_id">
              <td colspan="9" class="detailcell">
                <div v-if="!detail" class="muted">读取中…</div>
                <template v-else>
                  <div class="kv">
                    <b>冻结</b>
                    <span>
                      {{ detail.created_by }} ·
                      {{ (detail.frozen_at || '—').toString().slice(0, 19) }}
                    </span>
                    <b>签字</b>
                    <span>
                      {{ detail.reviewed_by || '—' }} ·
                      {{ (detail.published_at || '—').toString().slice(0, 19) }}
                    </span>
                    <b>包级校验</b><span class="mono">{{ detail.bundle_sha256 || '—' }}</span>
                    <b v-if="detail.note">说明</b><span v-if="detail.note"><MarkText :text="detail.note" /></span>
                  </div>
                  <table v-if="detail.assets.length">
                    <thead>
                      <tr>
                        <th style="width: 110px">层</th><th style="width: 58px">条目</th>
                        <th style="width: 112px">内容校验</th><th>出处</th>
                        <th style="width: 76px"></th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="a in detail.assets" :key="a.layer">
                        <td>
                          <b>{{ a.layer }}</b>
                          <span v-if="a.is_triple" class="tag t-pass">三元</span>
                        </td>
                        <td class="num">{{ a.item_count }}</td>
                        <td class="mono" style="font-size: 11px">
                          {{ a.content_sha256.slice(0, 12) }}…
                        </td>
                        <td class="mono" style="font-size: 11px">{{ a.source_ref }}</td>
                        <td>
                          <button class="mini" :disabled="!!busy"
                                  @click="showPayload(r.release_id, a.layer)">看快照</button>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                  <div v-else class="note">
                    这一版没有快照内容（<code>frozen_at</code> 为空的空壳）。
                    装载脚本建的工作区草稿就是这样——它只记了规则条数，没抄九层。
                    要发版请到「工作区」点<b>冻结为新版本</b>。
                  </div>
                  <pre v-if="pay" class="snap">{{ pay.layer }}  sha {{ pay.sha }}
{{ pay.text }}</pre>
                  <div v-if="detail.status === 'published'" class="row" style="margin-top: 8px">
                    <select v-model="rbTarget" style="width: 180px">
                      <option value="">回滚后：没有生效版本</option>
                      <option v-for="p in published.filter((x) => x.release_id !== r.release_id)"
                              :key="p.release_id" :value="p.release_id">
                        指回 {{ p.rule_set_version }}
                      </option>
                    </select>
                    <input v-model="reason" placeholder="回滚原因（必填）" style="flex: 1" />
                    <button class="mini" :disabled="!!busy || !who || !reason"
                            @click="act('已回滚', () => api.relRollback(r.release_id,
                                   { to: rbTarget, by: who, reason }))">回滚这一版</button>
                  </div>
                  <div v-if="detail.items.length" class="muted" style="font-size: 12px">
                    逐条规则的变更：
                    <span v-for="k in ['added', 'modified', 'retired', 'unchanged']" :key="k">
                      {{ CHANGE[k] }} {{ detail.items.filter((x) => x.change === k).length }}
                      <span class="sep">·</span>
                    </span>
                    <b>这不是该版本的内容</b>——内容在「规则」那一层的快照里。
                  </div>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <div class="note">
        <div v-for="(v, k) in policy" :key="k">· {{ v }}</div>
      </div>
    </div>

  </template>

  <!-- ③ 消费 ----------------------------------------------------------------->
  <template v-if="tab === 'consume'">
    <div v-if="idx" class="panel">
      <h3>对外消费：{{ idx.service_name }}</h3>
      <table>
        <thead><tr><th style="width: 330px">接口</th><th>给的是什么</th></tr></thead>
        <tbody>
          <tr v-for="e in idx.endpoints" :key="e.path">
            <td class="mono" style="font-size: 12px">{{ e.path }}</td>
            <td style="font-size: 12px">{{ e.what }}</td>
          </tr>
        </tbody>
      </table>
      <div class="kv" style="margin-top: 8px">
        <template v-for="(v, k) in idx.rules_of_the_road" :key="k">
          <b>{{ k }}</b><span style="font-size: 12px">{{ v }}</span>
        </template>
      </div>
    </div>

    <div v-if="idx" class="panel">
      <h3>九层的层名就是对外契约</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 30px">#</th><th style="width: 110px">层名</th>
            <th style="width: 150px">URL 别名</th><th>这一层是什么</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in idx.layers" :key="l.layer"
              :class="{ tri: idx.triple_ontology.includes(l.layer) }">
            <td class="muted">{{ l.ord }}</td>
            <td>
              <b>{{ l.layer }}</b>
              <span v-if="idx.triple_ontology.includes(l.layer)" class="tag t-pass">三元</span>
            </td>
            <td class="mono" style="font-size: 11px">{{ l.alias || '—' }}</td>
            <td style="font-size: 12px"><MarkText :text="l.note" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>可消费的版本</h3>
      <div v-if="!published.length" class="note">
        还没有任何一版签字发布，所以对外接口现在是空的——
        <b>这不等于接口没做</b>。去「工作区」冻结一版，再「签字发布」。
      </div>
      <table v-else>
        <thead>
          <tr>
            <th style="width: 128px">版本号</th><th style="width: 60px">当前</th>
            <th style="width: 112px">包级校验</th><th style="width: 92px">签字人</th>
            <th>取法</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in published" :key="p.release_id">
            <td class="mono">{{ p.rule_set_version }}</td>
            <td><span v-if="p.is_current" class="tag t-pass">当前</span></td>
            <td class="mono" style="font-size: 11px">
              {{ (p.bundle_sha256 || '').slice(0, 12) }}…
            </td>
            <td>{{ p.reviewed_by }}</td>
            <td style="font-size: 12px">
              <code>/api/pub/v1/releases/{{ p.rule_set_version }}</code>
              <a class="mini dl" :href="api.pubBundleUrl(p.rule_set_version)">下载资产包</a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h3>消费台账（{{ log.length }}）</h3>
      <table v-if="log.length">
        <thead>
          <tr>
            <th style="width: 140px">时间</th><th style="width: 128px">版本</th>
            <th style="width: 170px">资源</th><th style="width: 140px">调用方自报</th>
            <th class="num" style="width: 70px">字节</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in log" :key="l.id">
            <td class="mono" style="font-size: 11px">{{ l.at.toString().slice(0, 19) }}</td>
            <td class="mono" style="font-size: 11px">{{ l.rule_set_version || '—' }}</td>
            <td class="mono" style="font-size: 11px">{{ l.resource }}</td>
            <td style="font-size: 12px">{{ l.client || '—' }}</td>
            <td class="num">{{ l.bytes ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="note">还没有人拉过。</div>
    </div>
  </template>

  <!-- ④ 存储 ----------------------------------------------------------------->
  <template v-if="tab === 'store'">
    <div v-if="store" class="panel">
      <h3>存储</h3>
      <table>
        <thead>
          <tr>
            <th style="width: 230px">表</th><th class="num" style="width: 54px">行数</th>
            <th style="width: 190px">存的是什么</th><th style="width: 130px">谁写的</th>
            <th style="width: 150px">重算时</th><th>为什么这么存</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in store.items" :key="t.table_name">
            <td class="mono" style="font-size: 12px">{{ t.table_name }}</td>
            <td class="num">{{ t.rows ?? '—' }}</td>
            <td style="font-size: 12px">{{ t.what }}</td>
            <td class="muted" style="font-size: 12px">{{ t.written_by }}</td>
            <td style="font-size: 12px">{{ t.on_recalc }}</td>
            <td class="muted" style="font-size: 12px"><MarkText :text="t.why" /></td>
          </tr>
        </tbody>
      </table>
      <div class="note"><MarkText :text="store.note" /></div>
    </div>

  </template>
</template>

<style scoped>
button.on { background: var(--brand); color: #fff; }
button.mini { padding: 2px 7px; font-size: 12px; }
a.mini.dl {
  display: inline-block; padding: 2px 7px; font-size: 12px; margin-left: 4px;
  border: 1px solid var(--line); border-radius: 4px; text-decoration: none;
}
td.acts { white-space: nowrap; }
tr.tri td { background: #f7fbf8; }
tr.cur td { background: #fffdf5; }
td.warn { color: var(--hit); }
.detailcell { background: #fafbfc; }
.gates td { vertical-align: top; }
.ok-note {
  background: #f1f8f2; border: 1px solid var(--pass); border-radius: var(--radius);
  padding: 7px 9px; margin-bottom: 10px; font-size: 13px;
}
.snap {
  max-height: 300px; overflow: auto; background: #fff; border: 1px solid var(--line);
  border-radius: 4px; padding: 8px; font-size: 11px; line-height: 1.5; white-space: pre;
}
.sep { margin: 0 4px; color: var(--line); }
</style>
