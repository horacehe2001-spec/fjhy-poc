// 用 vitest/config 的 defineConfig，不是 vite 的：
// vite 的 UserConfig 类型里没有 test 字段，写上去 vue-tsc 会报 TS2769。
// 这个错不影响 vite build（build 不做类型检查），所以**只跑 build 是看不出来的**。
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 后端地址走环境变量，默认 127.0.0.1:8000。
// 实测需要：这台机器上 8000 已被别的进程占用，只能换端口起后端——
// 如果端口写死在代码里，换端口就得改代码，而改代码部署是最容易忘的一步。
//   FJHY_API_TARGET=http://127.0.0.1:8010 npm run dev
const API_TARGET = process.env.FJHY_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: Number(process.env.FJHY_WEB_PORT || 5173),
    host: '127.0.0.1',
    // 前端代码里一律用相对路径 /api，由这里代理过去。
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/health': { target: API_TARGET, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
