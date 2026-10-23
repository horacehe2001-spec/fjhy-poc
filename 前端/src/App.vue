<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { NAV } from './data/navTree'
import { api } from './api'
import { current as curDomain, label as domainLabel, load as loadDomain } from './data/domainState'

const route = useRoute()
const counts = ref<Record<string, number>>({})
const banner = ref<{ demo_bills: number; real_bills: number } | null>(null)
const navErr = ref('')

onMounted(async () => {
  // 当前领域/实例：页头徽标和落地页用同一份，各取各的会出现
  // 「切换了但页头没变」，看的人会以为切换没生效。
  loadDomain()
  try {
    const d = await api.assetNav()
    const c: Record<string, number> = {}
    for (const it of d.items) c[it.key] = it.count
    counts.value = c
    banner.value = d.banner
  } catch (e) {
    // 侧栏取不到条数不该让整个页面挂掉，但**也不能静默**——
    // 静默的结果是侧栏数字全空，看着像「系统里没数据」，
    // 而实际是「接口挂了」。这两件事必须分得开。
    navErr.value = e instanceof Error ? e.message : String(e)
  }
})
</script>

<template>
  <div class="shell">
    <!-- 顶栏仿官网 header：白底 + 底部深蓝 + 金色竖条，不放 logo 图
         （没有集团的正式 logo 文件，拿别的图凑不如不放） -->
    <header class="topbar">
      <span class="org"><i class="mark" />福建省港口集团有限责任公司</span>
      <span class="sys">差旅费 AI 智能审核 · 福建海运集团 POC</span>
      <span v-if="banner" class="right">
        真实单据 <b>{{ banner.real_bills }}</b> · 演示 <b>{{ banner.demo_bills }}</b>
      </span>
    </header>

    <div class="layout">
      <nav class="side">
        <div v-if="navErr" class="card">
          <div class="meta" style="color: #9a7b22">侧栏条数取不到：{{ navErr }}</div>
        </div>
        <div v-for="g in NAV" :key="g.key" class="card">
          <!-- 有落地页的组，组名本身是链接（目前只有领域知识资产）。
               没有落地页的组名保持纯文字：点了没反应的链接比没有链接更坏。 -->
          <router-link v-if="g.path" :to="g.path" class="cap caplink"
                       :class="{ on: route.path === g.path }">{{ g.label }}</router-link>
          <div v-else class="cap">{{ g.label }}</div>
          <template v-for="it in g.items" :key="it.key">
            <router-link :to="it.path" :class="{ on: route.path.startsWith(it.path) }">
              {{ it.label }}
              <span v-if="counts[it.key] !== undefined" class="n">{{ counts[it.key] }}</span>
              <span v-if="it.todo" class="todo">未接：{{ it.todo }}</span>
            </router-link>
            <!-- 子项（三元本体隶属本体工场）。缩进一格表示隶属，
                 **不做折叠**：折起来等于把三页藏了一半，而它们是天天要点的。 -->
            <router-link
              v-for="c in it.children || []"
              :key="c.key"
              :to="c.path"
              class="child"
              :class="{ on: route.path.startsWith(c.path) }"
            >
              {{ c.label }}
              <span v-if="counts[c.key] !== undefined" class="n">{{ counts[c.key] }}</span>
              <span v-if="c.todo" class="todo">未接：{{ c.todo }}</span>
            </router-link>
          </template>
        </div>
      </nav>
      <main class="main">
        <!-- 当前实例徽标：只在领域知识资产这一组的页面上出现。
             **各栏目页看到的资产属于哪个实例，要在页面上写死**，
             否则切换之后看的还是同一堆数字，没人分得清是哪个实例的。 -->
        <div v-if="route.path.startsWith('/asset')" class="scopebar">
          <span class="scopetag">{{ domainLabel(curDomain) }}</span>
          <router-link v-if="route.path !== '/asset'" to="/asset" class="scopelink">切换</router-link>
        </div>
        <router-view />
      </main>
    </div>
  </div>
</template>
