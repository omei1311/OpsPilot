/**
 * Agent 对话状态管理。
 *
 * 这个 store 的核心职责：把后端的 SSE 事件流，
 * 翻译成前端可以直接渲染的消息 + 步骤列表。
 *
 * 事件 → UI 的映射关系：
 *     intent          → 往当前消息的 steps 里加一条"意图"步骤
 *     thought         → 加/追加一条"思考"步骤
 *     tool_call       → 加一条"工具调用"步骤（加载态）
 *     tool_result     → 找到对应的那条，改成完成态
 *     plan            → 生成确认卡片数据
 *     awaiting_approval → 锁定输入框，显示倒计时
 *     token           → 往消息正文追加文字（打字机效果）
 *     action_executed → 更新确认卡片的进度
 *     citations       → 记录引用工单
 *     done            → 结束本轮，恢复输入框
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'

import * as agentApi from '@/api/agent'
import type {
  ActionFinished,
  ChatMessage,
  Conversation,
  PendingAction,
  SSEMessage,
} from '@/types/agent'

let stepCounter = 0
function nextKey(): string {
  stepCounter += 1
  return `step-${stepCounter}`
}

export const useAgentStore = defineStore('agent', () => {
  // ── 状态 ────────────────────────────────────────────────
  const visible = ref(false)
  const messages = ref<ChatMessage[]>([])
  const conversationId = ref<number | null>(null)
  const conversations = ref<Conversation[]>([])

  /** 是否正在流式接收 */
  const streaming = ref(false)
  /** 当前是否在等待用户确认（此时输入框应禁用） */
  const awaitingApproval = ref(false)
  /** 本次批量执行的进度 */
  const actionProgress = ref<ActionFinished | null>(null)

  /** 用于取消请求 */
  let controller: AbortController | null = null

  // ── 派生状态 ────────────────────────────────────────────
  const hasMessages = computed(() => messages.value.length > 0)
  const lastMessage = computed(() => messages.value[messages.value.length - 1] ?? null)

  /** 当前是否忙（流式接收中或等待确认） */
  const busy = computed(() => streaming.value || awaitingApproval.value)

  // ── 面板开关 ────────────────────────────────────────────
  function toggle(): void {
    visible.value = !visible.value
  }

  function open(): void {
    visible.value = true
  }

  function close(): void {
    visible.value = false
  }

  // ── 会话管理 ────────────────────────────────────────────
  async function loadConversations(): Promise<void> {
    try {
      conversations.value = await agentApi.listConversations()
    } catch {
      /* 拦截器已提示 */
    }
  }

  /** 切换到某个历史会话，把消息恢复出来 */
  async function switchConversation(id: number): Promise<void> {
    if (streaming.value) {
      ElMessage.warning('正在执行中，请稍候再切换会话')
      return
    }
    conversationId.value = id
    try {
      const list = await agentApi.listMessages(id)
      messages.value = list.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        steps: [], // 历史消息不还原执行过程（想看可以查 run 详情）
        runId: m.run_id ?? undefined,
        createdAt: m.created_at,
      }))
    } catch {
      /* 拦截器已提示 */
    }
  }

  /** 开一个新会话 */
  function newConversation(): void {
    if (streaming.value) {
      ElMessage.warning('正在执行中，请稍候')
      return
    }
    conversationId.value = null
    messages.value = []
    awaitingApproval.value = false
    actionProgress.value = null
  }

  // ── 发送消息 ────────────────────────────────────────────

  /**
   * 处理一条 SSE 事件，更新 UI 状态。
   */
  function handleEvent(msg: SSEMessage): void {
    const current = lastMessage.value
    if (!current || current.role !== 'assistant') return

    const { event, data } = msg

    switch (event) {
      case 'run_started':
        if (data.conversation_id) conversationId.value = data.conversation_id
        if (data.run_id) current.runId = data.run_id
        break

      case 'intent':
        current.steps.push({
          key: nextKey(),
          type: 'intent',
          intent: data.intent,
          confidence: data.confidence,
          entities: data.entities,
          text: data.reasoning,
          at: Date.now(),
        })
        break

      case 'thought': {
        // 连续多段思考要合并到同一条，否则步骤列表会被刷屏
        const last = current.steps[current.steps.length - 1]
        if (last && last.type === 'thought') {
          last.text = (last.text ?? '') + (data.delta ?? '')
        } else {
          current.steps.push({
            key: nextKey(),
            type: 'thought',
            text: data.delta ?? '',
            at: Date.now(),
          })
        }
        break
      }

      case 'tool_call':
        current.steps.push({
          key: data.call_id || nextKey(),
          type: 'tool_call',
          tool: data.tool,
          args: data.args,
          ok: undefined, // 还没结果
          at: Date.now(),
        })
        break

      case 'tool_result': {
        // 按 call_id 找到刚才那条，补上结果
        const target = current.steps.find(
          (s) => s.type === 'tool_call' && s.key === data.call_id,
        )
        if (target) {
          target.summary = data.summary
          target.ok = data.ok
          target.durationMs = data.duration_ms
        } else {
          // 没找到就单加一条（比如 guard 之后直接发结果的场景）
          current.steps.push({
            key: nextKey(),
            type: 'tool_result',
            tool: data.tool,
            summary: data.summary,
            ok: data.ok,
            durationMs: data.duration_ms,
            at: Date.now(),
          })
        }
        break
      }

      case 'plan': {
        const action: PendingAction = {
          actionId: data.action_id,
          runId: current.runId ?? '',
          title: data.title,
          riskLevel: data.risk_level,
          affectedCount: data.affected_count,
          preview: data.preview ?? [],
          expiresAt: data.expires_at,
          timeoutSeconds: data.timeout_seconds,
        }
        current.steps.push({
          key: nextKey(),
          type: 'plan',
          text: data.title,
          at: Date.now(),
        })
        current.pendingAction = action
        break
      }

      case 'awaiting_approval':
        awaitingApproval.value = true
        break

      case 'token':
        // 打字机效果：不断往正文后面追加
        current.content += data.delta ?? ''
        break

      case 'citations':
        current.citations = data.items ?? []
        break

      case 'message':
        // 后端的持久化消息到了，用它的 id 替换本地的临时 id
        if (typeof data.message_id === 'number') current.id = data.message_id
        break

      case 'action_executed': {
        // 逐条进度：累加已完成的数量
        if (!actionProgress.value) {
          actionProgress.value = { actionId: '', succeeded: 0, failed: 0, total: 0 }
        }
        if (data.ok) actionProgress.value.succeeded += 1
        else actionProgress.value.failed += 1
        break
      }

      case 'action_finished':
        actionProgress.value = {
          actionId: data.action_id ?? '',
          succeeded: data.succeeded ?? 0,
          failed: data.failed ?? 0,
          total: data.total ?? 0,
          durationMs: data.duration_ms,
          rejected: data.rejected,
        }
        break

      case 'error':
        current.steps.push({
          key: nextKey(),
          type: 'error',
          text: data.message ?? '出错了',
          at: Date.now(),
        })
        ElMessage.error(data.message ?? '处理时出错了')
        break

      case 'done':
        current.streaming = false
        awaitingApproval.value = false
        if (current.runId === undefined && data.run_id) current.runId = data.run_id
        break

      default:
        // 未知事件忽略，不报错 —— 保证后端加新事件不会让老前端崩掉
        break
    }
  }

  /**
   * 发送一条消息。
   *
   * 会立刻在界面上插入"用户消息"和一条空的"助手消息"，
   * 助手消息的内容随着 SSE 事件逐步填充 —— 这就是流式的观感来源。
   */
  async function send(text: string): Promise<void> {
    const content = text.trim()
    if (!content || busy.value) return

    messages.value.push({
      id: `local-user-${Date.now()}`,
      role: 'user',
      content,
      steps: [],
    })

    const assistant: ChatMessage = {
      id: `local-assistant-${Date.now()}`,
      role: 'assistant',
      content: '',
      steps: [],
      streaming: true,
      pendingAction: null,
      citations: [],
    }
    messages.value.push(assistant)

    streaming.value = true
    controller = new AbortController()

    await agentApi.chatStream(
      content,
      conversationId.value,
      {
        onEvent: handleEvent,
        onError: (err) => {
          assistant.steps.push({
            key: nextKey(),
            type: 'error',
            text: err.message,
            at: Date.now(),
          })
          if (!assistant.content) {
            assistant.content = `抱歉，出错了：${err.message}`
          }
        },
        onClose: () => {
          assistant.streaming = false
          streaming.value = false
          controller = null
        },
      },
      controller.signal,
    )
  }

  // ── 确认 / 拒绝 ─────────────────────────────────────────

  /** 确认执行。selectedIds 是用户在卡片上勾选的工单 ID */
  async function approve(actionId: string, selectedIds: number[] | null): Promise<void> {
    if (streaming.value) return
    await runDecision(actionId, () => agentApi.approveStream(
      actionId,
      selectedIds ? { ticket_ids: selectedIds } : null,
      decisionHandlers(),
      controller?.signal,
    ))
  }

  /** 拒绝执行 */
  async function reject(actionId: string, note: string | null = null): Promise<void> {
    if (streaming.value) return
    await runDecision(actionId, () => agentApi.rejectStream(
      actionId,
      note,
      decisionHandlers(),
      controller?.signal,
    ))
  }

  function decisionHandlers() {
    return {
      onEvent: (msg: SSEMessage) => {
        // 确认/拒绝的结果追加到当前这条助手消息上
        handleEvent(msg)
        if (msg.event === 'plan') {
          /* 不会出现 */
        }
      },
      onError: (err: Error) => {
        ElMessage.error(err.message)
        streaming.value = false
        awaitingApproval.value = false
      },
      onClose: () => {
        streaming.value = false
        awaitingApproval.value = false
        controller = null
      },
    }
  }

  async function runDecision(
    _actionId: string,
    runner: () => Promise<void>,
  ): Promise<void> {
    // 复用最后一条助手消息作为"结果输出区"。
    // 如果没有（比如用户刷新后直接点确认），就新建一条。
    let target = lastMessage.value
    if (!target || target.role !== 'assistant') {
      target = {
        id: `local-assistant-${Date.now()}`,
        role: 'assistant',
        content: '',
        steps: [],
        streaming: true,
      }
      messages.value.push(target)
    }

    streaming.value = true
    actionProgress.value = null
    controller = new AbortController()

    try {
      await runner()
    } catch (err) {
      ElMessage.error(err instanceof Error ? err.message : String(err))
      streaming.value = false
    }
  }

  /** 中断当前请求 */
  function abort(): void {
    controller?.abort()
    controller = null
    streaming.value = false
    awaitingApproval.value = false
    const last = lastMessage.value
    if (last?.streaming) last.streaming = false
  }

  return {
    // 状态
    visible,
    messages,
    conversationId,
    conversations,
    streaming,
    awaitingApproval,
    actionProgress,
    // 派生
    hasMessages,
    lastMessage,
    busy,
    // 操作
    toggle,
    open,
    close,
    send,
    approve,
    reject,
    abort,
    loadConversations,
    switchConversation,
    newConversation,
  }
})
