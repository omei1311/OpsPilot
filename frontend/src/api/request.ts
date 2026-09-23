/**
 * Axios 实例与全局拦截器。
 *
 * 这是前端所有网络请求的统一出口。整个项目里只有这一个地方创建 axios，
 * 业务代码一律 import 这个 request 对象。
 *
 * 好处是「带 token」和「统一报错」这两件每请求都要做的事，
 * 只写一遍，不用在每个页面里重复。
 */

// @ts-ignore
import axios, { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
// @ts-ignore
import { ElMessage } from 'element-plus'

import type { ApiError } from '@/types/auth'

/** localStorage 里存 token 用的 key。集中定义，避免各处手写字符串写错 */
export const TOKEN_KEY = 'opspilot_token'

/** 登录页 / 注册页的路径。这两个页面上收到 401 不需要跳转 */
const AUTH_PAGES = ['/login', '/register']

const request = axios.create({
  // 只写 /api/v1，不写完整域名。
  // 开发时 Vite 代理会转发到 8000；生产时 nginx 反代。
  // 前端代码两种环境都不用改。
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// ══════════════════════════════════════════════════════════════
// 请求拦截器：发出前做点什么
// ══════════════════════════════════════════════════════════════

request.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // ★ 自动带上 token。
    // 这就是"登录之后所有请求都认识我"的实现方式 ——
    // 登录成功时把 token 存进 localStorage，之后每次请求自动塞进请求头：
    //     Authorization: Bearer eyJhbGciOi...
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error: AxiosError) => Promise.reject(error),
)

// ══════════════════════════════════════════════════════════════
// 响应拦截器：收到后做点什么
// ══════════════════════════════════════════════════════════════

request.interceptors.response.use(
  // 【成功分支】HTTP 状态码 2xx 时走这里
  (response: AxiosResponse) => {
    // 直接返回 response.data，业务代码就不用每次都写 .data 了：
    //     const user = await getMe()        // 而不是 (await getMe()).data
    return response.data
  },

  // 【失败分支】非 2xx 时走这里
  (error: AxiosError<ApiError>) => {
    const status = error.response?.status
    // 后端所有错误都是 {code, message, detail} 格式（见 core/exceptions.py），
    // 所以这里能直接读到一句给人看的中文提示
    const apiError = error.response?.data
    const message =
      apiError?.message ||
      (error.code === 'ECONNABORTED' ? '请求超时，请稍后重试' : '网络异常，请检查后端服务是否启动')

    if (status === 401) {
      // ── 401：凭证失效 ──────────────────────────────────
      // 清掉本地 token，然后跳回登录页。
      //
      // 注意这里【不弹错误提示】：应用刚启动时用旧 token 调 /auth/me
      // 拿到 401 是完全正常的情况，弹个红条只会让用户困惑。
      localStorage.removeItem(TOKEN_KEY)

      const currentPath = window.location.pathname
      if (!AUTH_PAGES.includes(currentPath)) {
        // 用 window.location 而不是 router.push：
        // router 依赖 store，store 依赖 api，api 再 import router 就成环了。
        // 整页跳转同时也把内存里的脏状态清干净了。
        window.location.href = '/login'
      }
    } else {
      // ── 其他错误：弹提示 ───────────────────────────────
      ElMessage.error(message)
    }

    // 继续把错误往上抛，这样调用方还能自己 catch 做额外处理
    // （比如登录页要拿到 401 来显示"密码错误"）
    return Promise.reject(error)
  },
)

export default request
