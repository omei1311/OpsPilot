/**
 * 应用入口。
 *
 * 浏览器加载 index.html → 执行这个文件 → 创建 Vue 应用并挂载。
 * 这一步做完，页面才真正"活"起来。
 */

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIcons from '@element-plus/icons-vue'

// Element Plus 的样式。必须引入，否则组件是"裸"的没有样式
import 'element-plus/dist/index.css'
import '@/styles/main.css'

import App from '@/App.vue'
import router from '@/router'

const app = createApp(App)

// ── 全局注册所有图标 ───────────────────────────────────────
//
// Element Plus 的图标是独立包 @element-plus/icons-vue。
// 图标有 300 多个，一个个按需 import 太啰嗦，
// 这里一次性全局注册，任何组件里直接写 <el-icon><DataLine /></el-icon> 就能用。
//
// 代价是打包体积略大（约几十 KB）。项目规模小，这点体积换开发效率很划算。
for (const [name, component] of Object.entries(ElementPlusIcons)) {
  app.component(name, component)
}

// ── 挂载插件 ───────────────────────────────────────────────
// 顺序有讲究：Pinia 必须在 router 之前。
// 因为 router 的导航守卫里会调用 useUserStore()，
// 如果 Pinia 还没装上，守卫一执行就会报
// "getActivePinia() was called but there was no active Pinia"。
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
