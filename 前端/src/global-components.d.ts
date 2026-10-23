// 全局组件的 TypeScript 类型声明。
// 在这里声明的组件名可在所有模板里直接使用，vue-tsc 不会报「未知组件」。
import MarkText from './components/MarkText.vue'

declare module 'vue' {
  interface GlobalComponents {
    MarkText: typeof MarkText
  }
}

export {}
