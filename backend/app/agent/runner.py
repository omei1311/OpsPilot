"""Agent 执行编排：把图运行翻译成 SSE 事件流。

★ 关于 HITL 的实现方案（重要设计决策）

架构文档里写的主方案是 LangGraph 的 interrupt() + Redis Checkpointer。
本项目实际用的是【两阶段方案】，两者对比如下：

                    两阶段方案（本文件采用）          interrupt + Checkpointer
  等待状态存哪       MySQL 的 agent_pending_actions   Redis 的 checkpoint
  服务重启          不影响，计划在库里                内存版会丢，Redis 版不丢
  多 worker         天然支持                        需要 Redis
  额外依赖          无                               需要 Redis
  上下文连续性      确认后重新构造上下文（简化版）    完整保留图内所有状态
  调试难度          低：直接查表就能看到计划          高：状态在 Redis 序列化结构里

选两阶段的理由：
  · 本项目已经有 MySQL，不需要为了这个功能再加 Redis
  · "等待确认"本质上是一个业务状态，放在业务库里比放在缓存里更合适
    （可以查询、可以统计、可以审计）
  · 面试时能讲清"为什么不直接用框架提供的能力"比"用了框架"更有说服力

代价：确认后无法恢复图内的完整推理上下文。对本项目影响不大，
因为批量操作的参数（工单 ID 列表）已经完整存在计划里了。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import ToolContext
from app.agent.events import SSEEvent, format_sse, heartbeat
from app.agent.executor import execute_action, summarize_result
from app.agent.graph import agent_graph
from app.agent.llm import get_llm
from app.agent.state import initial_state
from app.core.exceptions import BizError
from app.core.logging import get_logger
from app.models.agent import AgentRunStatus, PendingActionStatus
from app.models.user import User
from app.services.agent_service import AgentService

logger = get_logger(__name__)

# 心跳间隔（秒）。小于 nginx 的 proxy_read_timeout（我们配 300s）
# 和大多数负载均衡的 60s 空闲超时。
HEARTBEAT_INTERVAL = 15


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════


async def _with_heartbeat(
    source: AsyncGenerator[str, None],
    interval: int = HEARTBEAT_INTERVAL,
) -> AsyncGenerator[str, None]:
    """给 SSE 流加心跳。

    问题：一次 LLM 调用可能要 5~10 秒，这期间没有任何数据发出。
    中间的 nginx / 负载均衡会认为这条连接空闲了，直接掐断。
    表现就是"前端转着转着突然断了"。

    做法：用 wait_for 给每次读取加超时，超时了就发一个心跳注释行。
    心跳是 SSE 协议里的注释（以冒号开头），客户端会忽略它。

    ⚠️ 注意这里不能简单地 `async for` —— 那样读不到数据时会一直挂着，
    没机会插入心跳。必须用 __anext__() + wait_for 手动控制。
    """
    iterator = source.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=interval)
        except asyncio.TimeoutError:
            yield heartbeat()
            continue
        except StopAsyncIteration:
            break
        yield item


def _sse(event: SSEEvent | str, data: dict[str, Any], event_id: int | None = None) -> str:
    return format_sse(event, data, event_id)


# ══════════════════════════════════════════════════════════════
# 主流程：一次对话
# ══════════════════════════════════════════════════════════════


async def stream_chat(
    db: AsyncSession,
    user: User,
    message: str,
    conversation_id: int | None = None,
) -> AsyncGenerator[str, None]:
    """处理一次用户输入，产出 SSE 事件流。

    这是一个 async generator：调用方（FastAPI 的 StreamingResponse）
    会不断从它里面取字符串往客户端推。
    """
    started = time.perf_counter()
    seq = 0

    # ── 准备 ───────────────────────────────────────────────
    conv = await AgentService.get_or_create_conversation(db, user, conversation_id)
    await AgentService.add_message(
        db, conversation_id=conv.id, role="user", content=message
    )

    run_id = AgentService.new_run_id()
    await AgentService.create_run(
        db,
        run_id=run_id,
        conversation_id=conv.id,
        user_id=user.id,
        user_input=message,
    )

    seq += 1
    yield _sse(
        SSEEvent.RUN_STARTED,
        {
            "run_id": run_id,
            "conversation_id": conv.id,
            "conversation_title": conv.title,
        },
        seq,
    )

    ctx = ToolContext(db=db, user=user, run_id=run_id)

    # ── 加载历史（多轮对话的上下文）────────────────────────
    #
    # 只取最近几轮。取太多会把上下文撑爆，而且早期的对话
    # 对当前这一轮通常没有帮助。
    history_msgs = await AgentService.list_messages(db, conv.id, limit=20)
    history = []
    for m in history_msgs[:-1]:  # 最后一条是刚存进去的这条用户消息，跳过
        if m.role == "user":
            history.append(HumanMessage(content=m.content))
        else:
            history.append(AIMessage(content=m.content))
    # 再砍一半，保证不超过 10 轮
    history = history[-10:]

    state = initial_state(
        run_id=run_id,
        conversation_id=conv.id,
        user_id=user.id,
        user_role=user.role,
        user_input=message,
        history=history,
    )

    # ── 跑图 ───────────────────────────────────────────────
    final_state: dict[str, Any] = {}
    error_message: str | None = None

    try:
        # stream_mode 里同时要 custom 和 updates：
        #   custom  → 节点自己用 get_stream_writer() 发的事件（我们的事件都走这个）
        #   updates → 每个节点执行完的 State 增量（用来拿最终状态）
        async for mode, chunk in agent_graph.astream(
            state,
            config=ctx.to_config(),
            stream_mode=["custom", "updates"],
        ):
            if mode == "custom":
                # 节点自己发的事件，原样转发
                event_name = chunk.get("event", "unknown")
                event_data = chunk.get("data", {})
                seq += 1
                yield _sse(event_name, event_data, seq)

                # 顺手记执行明细（用于刷新后回放）
                await _record_step(db, run_id, event_name, event_data)

            elif mode == "updates":
                # chunk 形如 {"节点名": {状态增量}}
                for node_name, node_update in chunk.items():
                    if isinstance(node_update, dict):
                        final_state.update(node_update)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 执行失败 | run_id=%s", run_id)
        error_message = (
            exc.message if isinstance(exc, BizError) else f"{type(exc).__name__}: {exc}"
        )
        seq += 1
        yield _sse(
            SSEEvent.ERROR,
            {
                "code": "AGENT_ERROR",
                # ★ 不把原始堆栈发给前端 —— 那是信息泄露。
                # 只给一句人话，详细堆栈在服务端日志里。
                "message": error_message if isinstance(exc, BizError) else "处理时出错了，请稍后重试",
                "recoverable": True,
            },
            seq,
        )

    # ── 收尾 ───────────────────────────────────────────────
    duration_ms = int((time.perf_counter() - started) * 1000)
    status = final_state.get("status") or (
        AgentRunStatus.FAILED if error_message else AgentRunStatus.SUCCESS
    )

    answer = final_state.get("answer") or ""
    pending_action_id = final_state.get("pending_action_id")

    # guard 节点走完时给一句过渡语 —— 否则前端只有确认卡片没有文字，很突兀
    if status == AgentRunStatus.AWAITING_APPROVAL and not answer:
        plan = final_state.get("plan") or {}
        affected = plan.get("affected_count", 0)
        answer = (
            f"我找到了 {affected} 条符合条件的工单，准备执行「{plan.get('title', '批量操作')}」。"
            f"这个操作会修改多条数据，需要你确认后才会执行。"
        )
        seq += 1
        yield _sse(SSEEvent.TOKEN, {"delta": answer}, seq)

    # 落库：助手消息 + 运行结果
    if answer:
        msg = await AgentService.add_message(
            db, conversation_id=conv.id, role="assistant", content=answer, run_id=run_id
        )
        seq += 1
        yield _sse(
            SSEEvent.MESSAGE,
            {"message_id": msg.id, "role": "assistant", "content": answer, "run_id": run_id},
            seq,
        )

    await AgentService.finish_run(
        db,
        run_id,
        status=status,
        intent=final_state.get("intent"),
        answer=answer or None,
        error=error_message,
        tool_call_count=len(final_state.get("observations") or []),
        duration_ms=duration_ms,
    )

    seq += 1
    yield _sse(
        SSEEvent.DONE,
        {
            "run_id": run_id,
            "conversation_id": conv.id,
            "status": status,
            "duration_ms": duration_ms,
            "pending_action_id": pending_action_id,
        },
        seq,
    )


async def _record_step(
    db: AsyncSession, run_id: str, event_name: str, data: dict[str, Any]
) -> None:
    """把事件记进 agent_run_steps，用于前端刷新后回放。"""
    mapping = {
        SSEEvent.INTENT: "intent",
        SSEEvent.THOUGHT: "thought",
        SSEEvent.TOOL_CALL: "tool_call",
        SSEEvent.TOOL_RESULT: "tool_result",
        SSEEvent.PLAN: "plan",
        SSEEvent.AWAITING_APPROVAL: "approval",
        SSEEvent.ERROR: "error",
    }
    step_type = mapping.get(event_name)  # type: ignore[arg-type]
    if step_type is None:
        return

    try:
        await AgentService.add_step(
            db,
            run_id=run_id,
            step_type=step_type,
            tool_name=data.get("tool"),
            input_data=data.get("args") if step_type == "tool_call" else None,
            output_data=data if step_type != "tool_call" else None,
            status="failed" if event_name == SSEEvent.ERROR else "ok",
            duration_ms=data.get("duration_ms"),
            commit=True,
        )
    except Exception:  # noqa: BLE001
        # 记账失败不应该影响主流程
        logger.warning("记录执行明细失败 | run_id=%s event=%s", run_id, event_name)


# ══════════════════════════════════════════════════════════════
# 审批后执行
# ══════════════════════════════════════════════════════════════


async def stream_approve(
    db: AsyncSession,
    user: User,
    action_id: str,
    *,
    approve: bool,
    edited_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> AsyncGenerator[str, None]:
    """处理用户的确认 / 拒绝，产出 SSE 事件流。

    确认和拒绝走同一个入口，因为前端用同一个连接等待结果，
    事件格式也一致。
    """
    started = time.perf_counter()
    seq = 0

    action = await AgentService.get_pending_action(db, action_id, user)
    run_id = action.run_id
    conversation_id = action.conversation_id

    seq += 1
    yield _sse(
        SSEEvent.RUN_STARTED,
        {"run_id": run_id, "conversation_id": conversation_id, "resumed": True},
        seq,
    )

    # ── 记录决定 ───────────────────────────────────────────
    try:
        action = await AgentService.decide_action(
            db,
            action,
            approve=approve,
            user=user,
            edited_payload=edited_payload,
            note=note,
        )
    except BizError as exc:
        # 重复提交 / 已过期 —— 返回 409 类错误，不是 500
        seq += 1
        yield _sse(
            SSEEvent.ERROR,
            {"code": exc.code, "message": exc.message, "recoverable": False},
            seq,
        )
        seq += 1
        yield _sse(SSEEvent.DONE, {"run_id": run_id, "status": "failed"}, seq)
        return

    # ── 拒绝：直接结束 ─────────────────────────────────────
    if not approve:
        answer = "好的，已取消这次操作，数据没有任何改动。"
        await AgentService.add_message(
            db, conversation_id=conversation_id, role="assistant", content=answer, run_id=run_id
        )
        await AgentService.add_step(
            db,
            run_id=run_id,
            step_type="approval",
            output_data={"decision": "rejected", "note": note},
            commit=True,
        )
        await AgentService.finish_run(
            db, run_id, status=AgentRunStatus.REJECTED, answer=answer
        )

        seq += 1
        yield _sse(SSEEvent.TOKEN, {"delta": answer}, seq)
        seq += 1
        yield _sse(SSEEvent.ACTION_FINISHED, {"action_id": action_id, "rejected": True}, seq)
        seq += 1
        yield _sse(SSEEvent.DONE, {"run_id": run_id, "status": "rejected"}, seq)
        return

    # ── 批准：执行 ─────────────────────────────────────────
    await AgentService.add_step(
        db,
        run_id=run_id,
        step_type="approval",
        output_data={"decision": "approved", "edited": bool(edited_payload)},
        commit=True,
    )

    # 逐条进度直接推到 SSE 队列。
    #
    # ⚠️ 这里用 asyncio.Queue 而不是直接 yield，是因为 execute_action
    # 是一个普通协程，它没法往生成器里塞数据。
    # 解法：让它往队列里放，生成器这边同时从队列取。
    progress_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def on_progress(event: dict[str, Any]) -> None:
        await progress_queue.put(event)

    exec_task = asyncio.create_task(
        execute_action(db, action, user, on_progress=on_progress)
    )

    # 边执行边把进度推给前端
    while not exec_task.done() or not progress_queue.empty():
        try:
            event = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            # 队列空但任务还在跑 —— 发个心跳保持连接
            if not exec_task.done():
                yield heartbeat()
            continue

        if event is None:
            break
        seq += 1
        yield _sse(event["event"], event["data"], seq)

    result = await exec_task

    # ── 结果汇报 ───────────────────────────────────────────
    summary = summarize_result(result)

    # 失败清单：清楚告诉用户哪几条没成功、为什么
    if result.get("failed_count"):
        lines = [
            f"  · #{f['ticket_id']}：{f['error']}" for f in (result.get("failed") or [])[:10]
        ]
        summary += "\n\n失败明细：\n" + "\n".join(lines)
        if result["failed_count"] > 10:
            summary += f"\n  …… 还有 {result['failed_count'] - 10} 条"

    seq += 1
    yield _sse(
        SSEEvent.ACTION_FINISHED,
        {
            "action_id": action_id,
            "succeeded": result.get("succeeded_count", 0),
            "failed": result.get("failed_count", 0),
            "total": result.get("total", 0),
            "duration_ms": result.get("duration_ms"),
        },
        seq,
    )

    seq += 1
    yield _sse(SSEEvent.TOKEN, {"delta": summary}, seq)

    await AgentService.add_message(
        db, conversation_id=conversation_id, role="assistant", content=summary, run_id=run_id
    )
    await AgentService.finish_run(
        db,
        run_id,
        status=(
            AgentRunStatus.SUCCESS
            if result.get("failed_count", 0) == 0
            else AgentRunStatus.FAILED
        ),
        answer=summary,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )

    seq += 1
    yield _sse(
        SSEEvent.DONE,
        {
            "run_id": run_id,
            "status": "success" if result.get("failed_count", 0) == 0 else "failed",
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
        seq,
    )


# ══════════════════════════════════════════════════════════════
# 对外入口（带心跳包装）
# ══════════════════════════════════════════════════════════════


def run_chat_stream(
    db: AsyncSession,
    user: User,
    message: str,
    conversation_id: int | None = None,
) -> AsyncGenerator[str, None]:
    """对话入口（已包装心跳）。"""
    return _with_heartbeat(stream_chat(db, user, message, conversation_id))


def run_approve_stream(
    db: AsyncSession,
    user: User,
    action_id: str,
    *,
    approve: bool,
    edited_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> AsyncGenerator[str, None]:
    """确认 / 拒绝入口（已包装心跳）。"""
    return _with_heartbeat(
        stream_approve(
            db,
            user,
            action_id,
            approve=approve,
            edited_payload=edited_payload,
            note=note,
        )
    )
