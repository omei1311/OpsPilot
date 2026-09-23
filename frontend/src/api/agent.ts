/** Agent 接口。 */

import request from '@/api/request'
import { TOKEN_KEY } from '@/api/request'
import { streamSSE, type SSEHandlers } from '@/api/sse'
import type { AgentMessageOut, Conversation } from '@/types/agent'

/** SSE 端点的完整路径。不走 axios，直接给 fetch 用 */
const BASE = '/api/v1'

/** 会话列表 */
export function listConversations(): Promise<Conversation[]> {
  return request.get('/agent/conversations')
}

/** 某个会话的消息历史 */
export function listMessages(conversationId: number): Promise<AgentMessageOut[]> {
  return request.get(`/agent/conversations/${conversationId}/messages`)
}

/** 删除会话 */
export function deleteConversation(conversationId: number): Promise<void> {
  return request.delete(`/agent/conversations/${conversationId}`)
}

/** 我的待确认列表 */
export function listPendingActions(): Promise<any[]> {
  return request.get('/agent/actions/pending')
}

/** 待确认详情 */
export function getPendingAction(actionId: string): Promise<any> {
  return request.get(`/agent/actions/${actionId}`)
}

/** 运行详情（含执行明细，用于回放） */
export function getRun(runId: string): Promise<any> {
  return request.get(`/agent/runs/${runId}`)
}

// ══════════════════════════════════════════════════════════════
// 流式接口（不走 axios，用 fetch + SSE）
// ══════════════════════════════════════════════════════════════

/** 发消息给 Agent，流式接收执行过程 */
export function chatStream(
  message: string,
  conversationId: number | null,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return streamSSE(
    `${BASE}/agent/chat`,
    { message, conversation_id: conversationId },
    handlers,
    signal,
  )
}

/** 确认执行批量操作 */
export function approveStream(
  actionId: string,
  editedPayload: { ticket_ids: number[] } | null,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return streamSSE(
    `${BASE}/agent/actions/${actionId}/approve`,
    editedPayload ? { edited_payload: editedPayload } : {},
    handlers,
    signal,
  )
}

/** 拒绝执行 */
export function rejectStream(
  actionId: string,
  note: string | null,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return streamSSE(
    `${BASE}/agent/actions/${actionId}/reject`,
    note ? { note } : {},
    handlers,
    signal,
  )
}

// 有些场景需要完整的 URL（比如在另一个标签页打开流），导出备用
export const AGENT_BASE = BASE
export { TOKEN_KEY }
