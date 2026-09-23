/**
 * SSE 客户端（手写解析器）。
 *
 * ★ 为什么不用浏览器的 EventSource？
 *
 *   EventSource 有三个硬伤，正好全都踩在我们的需求上：
 *
 *   ① 不能自定义请求头
 *      我们的接口要 JWT，必须发 Authorization: Bearer xxx。
 *      EventSource 只能带 Cookie 或者把 token 塞进 URL ——
 *      后者会进 nginx 访问日志，等于泄露凭证。
 *
 *   ② 只能发 GET
 *      聊天内容要放 URL 查询串里。中文要编码、长度有上限、
 *      而且用户说的话会留在服务器日志里。
 *
 *   ③ 不能取消
 *      用户关掉面板或者想中断，EventSource 只能 close()，
 *      没法在请求发出前中止。
 *
 *   用 fetch + ReadableStream 全部解决：能带头、能发 POST、能 abort。
 *   代价是要自己写 SSE 协议解析 —— 就是这个文件，约 100 行。
 */

import { TOKEN_KEY } from '@/api/request'
import type { SSEMessage } from '@/types/agent'

export interface SSEHandlers {
  /** 收到一帧事件 */
  onEvent: (msg: SSEMessage) => void
  /** 网络层或协议层出错 */
  onError?: (error: Error) => void
  /** 流正常结束（无论成功失败都会调用） */
  onClose?: () => void
}

/**
 * 解析一帧 SSE 文本。
 *
 * 一帧长这样：
 *     event: tool_call
 *     id: 7
 *     data: {"tool":"list_tickets"}
 *
 * 多行 data 会被拼起来（协议规定），但我们后端保证 data 是单行 JSON。
 */
function parseFrame(raw: string): SSEMessage | null {
  const lines = raw.split('\n')
  let event = 'message'
  let id: string | undefined
  const dataLines: string[] = []

  for (const line of lines) {
    // 以冒号开头的是注释（我们用它做心跳），忽略
    if (line.startsWith(':')) continue

    const colon = line.indexOf(':')
    if (colon === -1) continue

    const field = line.slice(0, colon)
    // 协议规定：冒号后面的第一个空格要去掉
    const value = line.slice(colon + 1).replace(/^ /, '')

    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
    else if (field === 'id') id = value
  }

  if (dataLines.length === 0) return null

  const text = dataLines.join('\n')
  let data: Record<string, any>
  try {
    data = JSON.parse(text)
  } catch {
    // 解析失败不抛错 —— 可能是服务端发了非 JSON 的内容。
    // 把它当纯文本传下去，至少前端能显示出来，不会整个流崩掉。
    data = { _raw: text }
  }

  return { event, data, id }
}

/**
 * 发起一个 SSE 请求并持续消费事件。
 *
 * @param url      完整路径（如 /api/v1/agent/chat）
 * @param body     请求体（POST）
 * @param handlers 事件回调
 * @param signal   AbortSignal，用于中途取消
 */
export async function streamSSE(
  url: string,
  body: unknown,
  handlers: SSEHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem(TOKEN_KEY)

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(body),
      signal,
    })

    // ── 错误响应不是 SSE 流，要单独处理 ────────────────────
    //
    // 比如没配 API Key 时后端返回的是 503 + JSON 错误体，
    // 不是事件流。不能按 SSE 解析，否则会得到空事件列表，
    // 前端表现成"点了没反应"，很难查。
    if (!response.ok) {
      let message = `请求失败（${response.status}）`
      try {
        const errBody = await response.json()
        if (errBody?.message) message = errBody.message
      } catch {
        /* 响应体不是 JSON，用默认提示 */
      }
      throw new Error(message)
    }

    if (!response.body) {
      throw new Error('响应没有可读流（浏览器可能不支持）')
    }

    // ── 读取流 ─────────────────────────────────────────────
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()

      if (done) break

      // stream: true 让解码器保留不完整的多字节字符，
      // 等下一个 chunk 拼上再解码。不加的话中文会被截断成乱码。
      buffer += decoder.decode(value, { stream: true })

      // 统一换行符。有些代理会把 \n 变成 \r\n
      buffer = buffer.replace(/\r\n/g, '\n')

      // ── 按空行切帧 ──────────────────────────────────────
      //
      // ⚠️ 关键：一个 chunk 可能包含多个完整帧，
      //    也可能只包含半帧。必须循环切分并把剩下的留在 buffer 里。
      //    直接假设"一个 chunk = 一帧"是最常见的错误写法。
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)

        if (frame.trim()) {
          const msg = parseFrame(frame)
          if (msg) handlers.onEvent(msg)
        }

        boundary = buffer.indexOf('\n\n')
      }
    }

    // 流结束时 buffer 里可能还留着最后一帧（没有以空行结尾）
    if (buffer.trim()) {
      const msg = parseFrame(buffer)
      if (msg) handlers.onEvent(msg)
    }
  } catch (err) {
    // 用户主动取消不算错误 —— 主动 abort 会抛 AbortError，
    // 如果把它当错误弹提示，用户每次关闭面板都会看到报错。
    if (err instanceof DOMException && err.name === 'AbortError') {
      return
    }
    handlers.onError?.(err instanceof Error ? err : new Error(String(err)))
  } finally {
    handlers.onClose?.()
  }
}
