/**
 * 用户状态管理（Pinia）。
 *
 * 解决的问题：登录状态（token、用户信息）是【很多页面都要用】的数据。
 * 如果不集中管理，就得在登录页存一遍、在 Dashboard 再读一遍、
 * 在路由守卫里又读一遍，很容易不一致。
 *
 * Pinia 的做法：把状态和操作它的方法放在一个"仓库"里，
 * 任何组件 import 一下就能用，改一处全都变。
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import { TOKEN_KEY } from '@/api/request'
import type { LoginPayload, RegisterPayload, UserInfo } from '@/types/auth'

export const useUserStore = defineStore('user', () => {
  // ── 状态 ────────────────────────────────────────────────
  // token 从 localStorage 初始化：这样刷新页面后依然是登录状态
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) ?? '')
  const profile = ref<UserInfo | null>(null)

  // ── 派生状态 ────────────────────────────────────────────
  // computed 会随依赖自动更新。用它而不是普通函数，
  // 是为了让模板里 `userStore.isLoggedIn` 这种写法能自动响应变化。
  const isLoggedIn = computed(() => token.value !== '')
  const isAdmin = computed(() => profile.value?.role === 'admin')
  const displayName = computed(() => profile.value?.username ?? '未登录')

  // ── 内部工具 ────────────────────────────────────────────

  function setToken(value: string): void {
    token.value = value
    // 同时写进 localStorage，否则一刷新就丢了
    localStorage.setItem(TOKEN_KEY, value)
  }

  function clearAuth(): void {
    token.value = ''
    profile.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  // ── 对外操作 ────────────────────────────────────────────

  /** 注册。成功不自动登录，让用户回登录页手动登一次（体验更清晰） */
  async function register(payload: RegisterPayload): Promise<UserInfo> {
    return authApi.register(payload)
  }

  /** 登录：拿到 token 存起来，同时把返回的用户信息也存下 */
  async function login(payload: LoginPayload): Promise<void> {
    const result = await authApi.login(payload)
    setToken(result.access_token)
    profile.value = result.user
  }

  /** 拉取当前用户信息。
   *
   * 用于两个时机：
   *   1. 刷新页面后，用 localStorage 里的 token 恢复用户信息
   *   2. 路由守卫里发现"有 token 但没有用户信息"时补一次
   */
  async function fetchProfile(): Promise<void> {
    profile.value = await authApi.getMe()
  }

  /** 登出。当前是纯前端行为（清掉本地 token）。
   *
   * 后端没有做 token 黑名单，所以旧 token 在过期前依然有效。
   * 这在实习项目里可以接受，面试时如果被问到要如实说明，
   * 并给出方案：用 Redis 存一个失效 token 集合，校验时多查一次。
   */
  function logout(): void {
    clearAuth()
  }

  return {
    token,
    profile,
    isLoggedIn,
    isAdmin,
    displayName,
    register,
    login,
    fetchProfile,
    logout,
  }
})
