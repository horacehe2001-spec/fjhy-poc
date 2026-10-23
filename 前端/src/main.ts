import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import MarkText from './components/MarkText.vue'
import './style.css'

// 用 hash 路由而不是 history 路由：
// POC 要能直接双击 dist/index.html 打开看，history 模式下那样打开会白屏
// （所有子路由都 404），而「本地打开就白屏」会让人以为构建坏了。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/sys/startup' },
    // 领域知识资产的落地页：选领域/实例。**放在 /asset/* 之前无所谓**，
    // vue-router 按最具体的匹配，不是按先后。
    { path: '/asset', component: () => import('./pages/DomainHome.vue') },
    { path: '/asset/docs', component: () => import('./pages/DocLib.vue') },
    { path: '/asset/extract', component: () => import('./pages/Extract.vue') },
    { path: '/asset/workshop', component: () => import('./pages/Workshop.vue') },
    { path: '/asset/biz', component: () => import('./pages/BizOnto.vue') },
    { path: '/asset/rules', component: () => import('./pages/RuleOnto.vue') },
    { path: '/asset/rules/:id', component: () => import('./pages/RuleDetail.vue') },
    { path: '/asset/process', component: () => import('./pages/ProcessOnto.vue') },
    { path: '/asset/releases', component: () => import('./pages/Publish.vue') },
    { path: '/review/ai-review', component: () => import('./pages/AIReview.vue') },
    { path: '/review/bills', component: () => import('./pages/Workbench.vue') },
    { path: '/review/bills/:no', component: () => import('./pages/BillDetail.vue') },
    { path: '/review/report/:runId', component: () => import('./pages/ReviewReport.vue') },
    { path: '/review/trace', component: () => import('./pages/AuditTrace.vue') },
    { path: '/review/trace/:no', component: () => import('./pages/AuditTrace.vue') },
    { path: '/review/deviation', component: () => import('./pages/Deviation.vue') },
    { path: '/sys/agents', component: () => import('./pages/AgentHub.vue') },
    { path: '/sys/startup', component: () => import('./pages/Startup.vue') },
    { path: '/sys/llm', component: () => import('./pages/LlmConfig.vue') },
    // 大模型配置 2026-10-18 从领域知识资产移到系统组。老路径保留，
    // 否则已经发出去的链接会白屏——白屏会被当成构建坏了，而不是「菜单挪了」。
    { path: '/asset/llm', redirect: '/sys/llm' },
  ],
})

// **全局注册 MarkText**：
// 18 个页面都要渲染后端文案里的 **加粗**，全局注册省去每个文件各自 import，
// 且类型声明在 global-components.d.ts 里统一维护。
const app = createApp(App)
app.component('MarkText', MarkText)
app.use(router).mount('#app')
