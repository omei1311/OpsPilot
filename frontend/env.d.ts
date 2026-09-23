/// <reference types="vite/client" />

// 让 TypeScript 认识 .vue 文件。
// 没有这段声明，import LoginView from './LoginView.vue' 会报
// "找不到模块" —— 因为 TS 默认只认 .ts/.js，不认识单文件组件。
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
