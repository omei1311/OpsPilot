/**
 * 工单相关类型。和后端 app/schemas/ticket.py 一一对应。
 */

export type TicketCategory =
  | 'account'
  | 'payment'
  | 'technical'
  | 'logistics'
  | 'operation'
  | 'other'

export type TicketPriority = 'low' | 'medium' | 'high' | 'urgent'

export type TicketStatus =
  | 'pending'
  | 'processing'
  | 'waiting'
  | 'resolved'
  | 'closed'

export type SlaStatus = 'normal' | 'at_risk' | 'overdue' | 'done'

export type TicketEventType =
  | 'created'
  | 'updated'
  | 'assigned'
  | 'status_changed'
  | 'priority_changed'
  | 'commented'

/** 工单完整信息 */
export interface Ticket {
  id: number
  ticket_no: string
  title: string
  description: string | null
  category: TicketCategory
  priority: TicketPriority
  status: TicketStatus
  creator_id: number
  assignee_id: number | null
  department_id: number | null
  creator_name: string | null
  assignee_name: string | null
  department_name: string | null
  sla_deadline: string | null
  sla_status: SlaStatus | null
  sla_remaining_minutes: number | null
  created_at: string
  updated_at: string
  resolved_at: string | null
}

/** 列表项（字段比详情少） */
export interface TicketBrief {
  id: number
  ticket_no: string
  title: string
  category: TicketCategory
  priority: TicketPriority
  status: TicketStatus
  assignee_name: string | null
  sla_deadline: string | null
  sla_status: SlaStatus | null
  created_at: string
}

export interface TicketComment {
  id: number
  ticket_id: number
  user_id: number
  author_name: string | null
  content: string
  created_at: string
}

export interface TicketEvent {
  id: number
  ticket_id: number
  user_id: number | null
  actor_name: string | null
  event_type: TicketEventType
  event_data: Record<string, unknown> | null
  created_at: string
}

/** 详情 = 基础信息 + 评论 + 操作记录 */
export interface TicketDetail extends Ticket {
  comments: TicketComment[]
  events: TicketEvent[]
}

/** 列表查询参数 */
export interface TicketListParams {
  status?: TicketStatus[]
  priority?: TicketPriority[]
  category?: TicketCategory[]
  assignee_id?: number
  department_id?: number
  keyword?: string
  /** 超过 N 小时未处理 */
  unhandled_hours?: number
  page?: number
  page_size?: number
}

export interface TicketCreatePayload {
  title: string
  description?: string | null
  category?: TicketCategory
  priority?: TicketPriority
  assignee_id?: number | null
  department_id?: number | null
}

export interface TicketUpdatePayload {
  title?: string
  description?: string | null
  category?: TicketCategory
  priority?: TicketPriority
  department_id?: number | null
}

export interface TicketAssignPayload {
  assignee_id?: number | null
  department_id?: number | null
}

/** 统计 */
export interface TicketStats {
  total: number
  by_status: Record<string, number>
  by_priority: Record<string, number>
  by_category: Record<string, number>
  overdue: number
  unassigned: number
}

// ── 中文标签映射 ──────────────────────────────────────────
// 集中放这里，各个组件 import 用，避免在模板里到处写 if-else

export const STATUS_LABELS: Record<TicketStatus, string> = {
  pending: '待处理',
  processing: '处理中',
  waiting: '等待中',
  resolved: '已解决',
  closed: '已关闭',
}

export const PRIORITY_LABELS: Record<TicketPriority, string> = {
  low: '低',
  medium: '中',
  high: '高',
  urgent: '紧急',
}

export const CATEGORY_LABELS: Record<TicketCategory, string> = {
  account: '账号',
  payment: '支付',
  technical: '技术',
  logistics: '物流',
  operation: '运营',
  other: '其他',
}

export const EVENT_LABELS: Record<TicketEventType, string> = {
  created: '创建工单',
  updated: '修改工单',
  assigned: '分派',
  status_changed: '变更状态',
  priority_changed: '变更优先级',
  commented: '添加评论',
}

export const SLA_LABELS: Record<SlaStatus, string> = {
  normal: '正常',
  at_risk: '即将超时',
  overdue: '已超时',
  done: '已完成',
}

/** Element Plus 的 tag type 取值 */
export const STATUS_TAG_TYPE: Record<TicketStatus, string> = {
  pending: 'info',
  processing: 'primary',
  waiting: 'warning',
  resolved: 'success',
  closed: '',
}

export const PRIORITY_TAG_TYPE: Record<TicketPriority, string> = {
  low: 'info',
  medium: '',
  high: 'warning',
  urgent: 'danger',
}

export const SLA_TAG_TYPE: Record<SlaStatus, string> = {
  normal: 'success',
  at_risk: 'warning',
  overdue: 'danger',
  done: 'info',
}
