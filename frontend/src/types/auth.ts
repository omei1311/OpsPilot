/**
 * 认证相关的类型定义。
 *
 * 作用：和后端 app/schemas/auth.py 一一对应。
 * 后端返回的 JSON 长什么样，这里就声明成什么样。
 * 好处是写代码时 IDE 能自动补全字段名，拼错了立刻报错。
 */

/** 角色。和后端 UserRole 枚举对齐 */
export type UserRole = 'admin' | 'operator' | 'user'

/** 用户公开信息。对应后端 schemas/auth.py 的 UserOut */
export interface UserInfo {
  id: number
  username: string
  email: string
  role: UserRole
  is_active: boolean
  /** ISO 8601 时间字符串，如 "2026-09-21T04:37:32" */
  created_at: string
}

/** 注册请求体。对应 RegisterRequest */
export interface RegisterPayload {
  username: string
  email: string
  password: string
}

/** 登录请求体。对应 LoginRequest */
export interface LoginPayload {
  /** 用户名或邮箱，后端两者都支持 */
  username: string
  password: string
}

/** 登录响应。对应 TokenResponse */
export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserInfo
}

/** 后端统一的错误响应体。对应 core/exceptions.py 的 build_error_body */
export interface ApiError {
  code: string
  message: string
  detail: Record<string, unknown> | null
}
