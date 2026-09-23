/**
 * Agent 相关类型。和后端 app/agent/events.py + schemas/agent.py 对应。
 */

/** 事件名。必须和后端 SSEEvent 枚举一致 */
export type AgentEventName =
  | 'run_started'
  | 'intent'
  | 'thought'
  | 'tool_call'
  | 'tool_result'
  | 'plan'
  | 'awaiting_approval'
  | 'action_executed'
  | 'action_finished'
  | 'token'
  | 'citations'
  | 'message'
  | 'error'
  | 'done'

export type AgentIntent =
  | 'create_ticket'
  | 'query_ticket'
  | 'analyze'
  | 'batch_update'
  | 'update_ticket'
  | 'chat'

/** 一帧 SSE 消息 */
export interface SSEMessage {
  event: string
  data: Record<string, any>
  id?: string
}

/** 执行过程中的一步（前端时间线用） */
export interface AgentStep {
  /** 本地生成的唯一 id，用于 v-for 的 key */
  key: string
  type: 'intent' | 'thought' | 'tool_call' | 'tool_result' | 'plan' | 'error'
  /** 工具调用时才有 */
  tool?: string
  args?: Record<string, any>
  /** 工具结果的摘要 */
  summary?: string
  ok?: boolean
  durationMs?: number
  /** 思考文本 / 错误信息 */
  text?: string
  /** 意图识别结果 */
  intent?: string
  confidence?: number
  entities?: Record<string, any>
  /** 时间戳 */
  at: number
}

/** 待确认的操作（HITL 确认卡片的数据） */
export interface PendingAction {
  actionId: string
  runId: string
  title: string
  riskLevel: string
  affectedCount: number
  preview: PreviewItem[]
  expiresAt: string
  timeoutSeconds: number
}

/** 确认卡片里逐条展示的工单 */
export interface PreviewItem {
  /** ★ 工单主键 ID。确认卡片靠它实现"逐条取消勾选"并回传 */
  id: number | null
  ticket_no: string | null
  title: string | null
  priority: string | null
  status: string | null
  sla_status?: string | null
}

/** 一条聊天消息 */
export interface ChatMessage {
  /** 后端返回的是 number，本地流式消息用字符串 id */
  id: number | string
  role: 'user' | 'assistant'
  content: string
  /** 执行过程（只有 assistant 消息有） */
  steps: AgentStep[]
  /** 待确认操作（如果有） */
  pendingAction?: PendingAction | null
  /** 引用到的工单 */
  citations?: { ticket_no: string; title: string }[]
  runId?: string
  /** 是否正在流式输出 */
  streaming?: boolean
  createdAt?: string
}

/** 后端会话 */
export interface Conversation {
  id: number
  title: string
  last_active_at: string
  created_at: string
}

/** 后端消息 */
export interface AgentMessageOut {
  id: number
  role: 'user' | 'assistant'
  content: string
  run_id: string | null
  created_at: string
}

/** 执行完成的结果统计 */
export interface ActionFinished {
  actionId: string
  succeeded: number
  failed: number
  total: number
  durationMs?: number
  rejected?: boolean
}

// ── 中文标签 ──────────────────────────────────────────────

export const INTENT_LABELS: Record<string, string> = {
  create_ticket: '创建工单',
  query_ticket: '查询工单',
  analyze: '数据分析',
  batch_update: '批量操作',
  update_ticket: '修改工单',
  chat: '闲聊',
}

export const TOOL_LABELS: Record<string, string> = {
  list_tickets: '查询工单列表',
  get_ticket: '查看工单详情',
  get_ticket_statistics: '统计工单',
  analyze_sla_risk: '分析 SLA 风险',
  summarize_ticket: '汇总工单信息',
  list_departments: '查询部门列表',
  create_ticket: '创建工单',
  update_ticket: '修改工单',
  assign_ticket: '分派工单',
  change_ticket_status: '变更状态',
  add_ticket_comment: '添加评论',
  batch_update_tickets: '批量修改工单',
  batch_assign_tickets: '批量分派工单',
}
