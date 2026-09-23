/**
 * 路由配置与导航守卫。
 *
 * 路由 = 「哪个网址显示哪个页面」的对应表。
 * 守卫 = 进入页面前的门卫，决定"让不让进"。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useUserStore } from '@/stores/user'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/dashboard',
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guestOnly: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { guestOnly: true, title: '注册' },
  },

  // ── 需要登录的页面 ─────────────────────────────────────
  //
  // 用「父路由 + children」的方式把多个页面套进同一个布局。
  // 父路由的 component 是 MainLayout（侧边栏 + 顶栏），
  // 子页面会被渲染进 MainLayout 里的 <router-view />。
  //
  // 子路由的 path 不写前导斜杠（写成 'dashboard' 而不是 '/dashboard'），
  // 这样它会自动拼到父路由的 '/' 后面。
  //
  // meta 会【继承】：父路由写了 requiresAuth，所有子路由自动都要求登录，
  // 不用每个子路由重复写一遍。
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/DashboardView.vue'),
        meta: { title: '数据看板' },
      },
      {
        path: 'tickets',
        name: 'tickets',
        component: () => import('@/views/ticket/TicketListView.vue'),
        meta: { title: '工单中心' },
      },
      {
        path: 'tickets/:id',
        name: 'ticket-detail',
        component: () => import('@/views/ticket/TicketDetailView.vue'),
        // props: true 会把路由参数（:id）作为 prop 传给组件。
        // 不开启的话组件里得用 useRoute().params.id 拿，稍微绕一点。
        props: true,
        meta: { title: '工单详情' },
      },
    ],
  },

  {
    // 兜底：匹配所有未定义的路径
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

const router = createRouter({
  // createWebHistory = 用真实的 URL（/login），没有 #
  // 另一种是 createWebHashHistory（/#/login），兼容性好但难看
  history: createWebHistory(),
  routes,
})

// ══════════════════════════════════════════════════════════════
// 导航守卫
// ══════════════════════════════════════════════════════════════

router.beforeEach(async (to) => {
  const userStore = useUserStore()

  // ── 1. 需要登录，但没登录 → 踢回登录页 ───────────────────
  if (to.meta.requiresAuth && !userStore.isLoggedIn) {
    return { name: 'login' }
  }

  // ── 2. 已登录还想去登录页 → 送回工作台 ───────────────────
  if (to.meta.guestOnly && userStore.isLoggedIn) {
    return { name: 'dashboard' }
  }

  // ── 3. 有 token 但用户信息是空的 → 补一次 ─────────────────
  //
  // 什么时候会发生？用户刷新页面时。
  // token 存在 localStorage 里所以还在，但 profile 存在内存里，刷新就没了。
  // 不补这一步的话，页面上会显示"未登录"。
  if (userStore.isLoggedIn && !userStore.profile) {
    try {
      await userStore.fetchProfile()
    } catch {
      // token 失效了。拦截器已经清掉 token，
      // 这里负责把用户送去登录页。
      return { name: 'login' }
    }
  }

  // 返回 undefined / true 表示"放行"
  return true
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · OpsPilot` : 'OpsPilot'
})

export default router
