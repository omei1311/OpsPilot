/** 工单接口。 */

import request from '@/api/request'
import type {
  Ticket,
  TicketAssignPayload,
  TicketBrief,
  TicketComment,
  TicketCreatePayload,
  TicketDetail,
  TicketListParams,
  TicketStats,
  TicketStatus,
  TicketUpdatePayload,
} from '@/types/ticket'

/** 后端统一的分页响应结构 */
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/**
 * 把查询参数转成后端能识别的格式。
 *
 * 关键点：数组类型（status / priority / category）要展开成重复的 key，
 * 后端才能收到列表：
 *     ?status=pending&status=processing   →  ["pending", "processing"]
 *
 * 用 URLSearchParams 自动处理，比手拼字符串可靠。
 */
function buildParams(params: TicketListParams): URLSearchParams {
  const sp = new URLSearchParams()

  const appendList = (key: string, value?: string[]) => {
    value?.forEach((v) => sp.append(key, v))
  }

  appendList('status', params.status)
  appendList('priority', params.priority)
  appendList('category', params.category)

  if (params.assignee_id != null) sp.set('assignee_id', String(params.assignee_id))
  if (params.department_id != null) sp.set('department_id', String(params.department_id))
  if (params.keyword) sp.set('keyword', params.keyword)
  if (params.unhandled_hours != null) sp.set('unhandled_hours', String(params.unhandled_hours))
  if (params.page != null) sp.set('page', String(params.page))
  if (params.page_size != null) sp.set('page_size', String(params.page_size))

  return sp
}

export function listTickets(params: TicketListParams = {}): Promise<PageResult<TicketBrief>> {
  return request.get('/tickets', { params: buildParams(params) })
}

export function getTicket(id: number): Promise<TicketDetail> {
  return request.get(`/tickets/${id}`)
}

export function createTicket(payload: TicketCreatePayload): Promise<Ticket> {
  return request.post('/tickets', payload)
}

export function updateTicket(id: number, payload: TicketUpdatePayload): Promise<Ticket> {
  return request.put(`/tickets/${id}`, payload)
}

export function changeTicketStatus(
  id: number,
  status: TicketStatus,
  reason?: string,
): Promise<Ticket> {
  return request.patch(`/tickets/${id}/status`, { status, reason })
}

export function assignTicket(id: number, payload: TicketAssignPayload): Promise<Ticket> {
  return request.patch(`/tickets/${id}/assign`, payload)
}

export function deleteTicket(id: number): Promise<void> {
  return request.delete(`/tickets/${id}`)
}

export function addComment(id: number, content: string): Promise<TicketComment> {
  return request.post(`/tickets/${id}/comments`, { content })
}

export function getTicketStats(days?: number): Promise<TicketStats> {
  return request.get('/tickets/statistics', { params: days ? { days } : {} })
}
