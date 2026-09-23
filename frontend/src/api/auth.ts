/**
 * 认证相关的接口调用。
 *
 * 约定：每个函数对应一个后端接口，只负责"发请求、返回结果"。
 * 不掺任何页面逻辑 —— 页面状态归 store 管。
 *
 * 这样拆的好处：以后要加接口，只改这个文件；
 * 要改页面交互，只改视图文件，两边互不干扰。
 */

import request from '@/api/request'
import type { LoginPayload, LoginResponse, RegisterPayload, UserInfo } from '@/types/auth'

/** 注册。POST /api/v1/auth/register */
export function register(payload: RegisterPayload): Promise<UserInfo> {
  return request.post('/auth/register', payload)
}

/** 登录。POST /api/v1/auth/login */
export function login(payload: LoginPayload): Promise<LoginResponse> {
  return request.post('/auth/login', payload)
}

/** 获取当前登录用户。GET /api/v1/auth/me */
export function getMe(): Promise<UserInfo> {
  return request.get('/auth/me')
}
