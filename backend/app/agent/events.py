"""SSE 事件定义。

事件名和数据结构集中在这里，前端照着这个契约解析。

SSE 帧格式（标准协议）::

    event: tool_call
    id: 7
    data: {"call_id":"c1","tool":"list_tickets"}

    ← 空行表示一帧结束

前端用 fetch + ReadableStream 解析（不用 EventSource，
因为 EventSource 不支持自定义请求头，没法带 JWT）。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class SSEEvent(StrEnum):
    """所有事件名。

    前端遇到不认识的事件名应当【忽略而不是报错】——
    这样后端加新事件不会导致老前端崩溃（前向兼容）。
    """

    # ── 生命周期 ───────────────────────────────────────────
    RUN_STARTED = "run_started"        # 一次运行开始
    DONE = "done"                      # 一次运行结束（必定发送）

    # ── 理解阶段 ───────────────────────────────────────────
    INTENT = "intent"                  # 意图识别结果
    THOUGHT = "thought"                # 模型思考文本

    # ── 工具调用 ───────────────────────────────────────────
    TOOL_CALL = "tool_call"            # 准备调用工具
    TOOL_RESULT = "tool_result"        # 工具返回

    # ── HITL ──────────────────────────────────────────────
    PLAN = "plan"                      # 生成了待确认的执行计划
    AWAITING_APPROVAL = "awaiting_approval"  # 图挂起，等待人工确认
    ACTION_EXECUTED = "action_executed"      # 批量执行中的一条
    ACTION_FINISHED = "action_finished"      # 批量执行结束

    # ── 输出 ───────────────────────────────────────────────
    TOKEN = "token"                    # 回答的增量文本（打字机效果）
    CITATIONS = "citations"            # 引用到的工单
    MESSAGE = "message"                # 消息已落库

    # ── 异常 ───────────────────────────────────────────────
    ERROR = "error"


def format_sse(event: SSEEvent | str, data: dict[str, Any], event_id: int | None = None) -> str:
    """把一个事件格式化成 SSE 帧。

    ⚠️ 两个容易踩的点：

    1. data 必须是【单行】。
       SSE 协议里多行 data 会被拼成多行内容，前端解析会错位。
       所以这里用 json.dumps 且不能带缩进（indent 会产生换行）。

    2. 帧必须以【两个换行】结尾。
       一个换行只是行结束，两个换行才是"这一帧结束"。
       少一个换行前端会一直等下一帧，表现为"页面卡住不出内容"。

    Args:
        event: 事件名
        data: 事件数据（会被 JSON 序列化）
        event_id: 可选的事件序号，用于断线重连时定位
    """
    lines: list[str] = []

    if event_id is not None:
        lines.append(f"id: {event_id}")

    lines.append(f"event: {event}")

    # ensure_ascii=False 让中文原样输出（而不是 \uXXXX 转义），
    # 省带宽也方便调试时直接看
    payload = json.dumps(data, ensure_ascii=False, default=str)
    lines.append(f"data: {payload}")

    return "\n".join(lines) + "\n\n"


def heartbeat() -> str:
    """心跳。

    以冒号开头的行是 SSE 的注释，客户端会忽略。
    作用是防止长连接被中间的代理/网关判定为空闲而掐断。

    场景 4 里用户可能花几分钟看确认卡片，这段时间没有数据流动，
    没有心跳的话连接很可能已经被 nginx 断掉了。
    """
    return ": ping\n\n"
