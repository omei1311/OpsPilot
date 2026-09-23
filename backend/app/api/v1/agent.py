"""Agent 接口（含 SSE 流式端点）。

三个关键点：

  ① SSE 用 POST 而不是 GET，用 fetch 而不是 EventSource
     原因见下面的注释 —— EventSource 不支持自定义请求头，没法带 JWT。

  ② SSE 路由要加特殊响应头
     `X-Accel-Buffering: no` 告诉 nginx 不要缓冲。
     不加的话，nginx 会把整个响应攒完再发 —— 前端表现为"一直转圈，
     最后一次性全部出现"，完全失去流式的意义。

  ③ 生成器要能感知客户端断开
     用户关掉页面后，后端还在跑 Agent 就是纯浪费钱。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse

from app.agent.runner import run_approve_stream, run_chat_stream
from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import ServiceUnavailableError
from app.schemas.agent import (
    ApproveRequest,
    ChatRequest,
    ConversationOut,
    MessageOut,
    PendingActionDetail,
    PendingActionOut,
    RejectRequest,
    RunOut,
    RunStepOut,
)
from app.schemas.common import ErrorResponse
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agent", tags=["Agent 助手"])

# SSE 响应的通用头
SSE_HEADERS = {
    # ★ 关键：禁止中间层缓冲
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}


def _require_llm() -> None:
    """检查大模型是否配置好。

    没配就直接返回 503 + 明确提示，而不是让请求打出去等一个 401。
    """
    if not settings.llm_configured:
        raise ServiceUnavailableError(
            "还没有配置大模型 API Key，Agent 功能不可用。"
            "请在 backend/.env 里设置 LLM_API_KEY 后重启后端。"
        )


# ══════════════════════════════════════════════════════════════
# 会话管理
# ══════════════════════════════════════════════════════════════


@router.get("/conversations", response_model=list[ConversationOut], summary="会话列表")
async def list_conversations(db: DbSession, user: CurrentUser) -> list[ConversationOut]:
    """当前用户的会话列表。

    注意只能看到自己的 —— 查询条件里强制带了 user_id。
    """
    convs = await AgentService.list_conversations(db, user)
    return [ConversationOut.model_validate(c) for c in convs]


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
    summary="会话消息历史",
)
async def list_messages(
    conversation_id: int, db: DbSession, user: CurrentUser
) -> list[MessageOut]:
    """会话的消息历史，用于前端刷新后恢复对话。"""
    # 先取会话（内部含越权检查），再列消息
    conv = await AgentService.get_or_create_conversation(db, user, conversation_id)
    msgs = await AgentService.list_messages(db, conv.id)
    return [MessageOut.model_validate(m) for m in msgs]


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话",
)
async def delete_conversation(
    conversation_id: int, db: DbSession, user: CurrentUser
) -> None:
    """删除会话。

    只删会话本身，历史消息保留（外键关联）。
    真实项目里应该级联删除或软删除，这里简化处理。
    """
    conv = await AgentService.get_or_create_conversation(db, user, conversation_id)
    await db.delete(conv)
    await db.commit()


# ══════════════════════════════════════════════════════════════
# ★ SSE 主入口
# ══════════════════════════════════════════════════════════════


@router.post(
    "/chat",
    summary="对话（SSE 流式）",
    description=(
        "发送一条消息，以 Server-Sent Events 流式返回 Agent 的执行过程。\n\n"
        "**为什么是 POST 而不是 GET？**\n"
        "SSE 的 GET 语义要求把消息放在 URL 查询串里，"
        "中文和长文本会超长且需要编码。而且浏览器的 EventSource "
        "不支持自定义请求头，没法带 Authorization —— 用 POST + fetch 更合适。\n\n"
        "**事件类型见 app/agent/events.py 的 SSEEvent 枚举。**"
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "未配置大模型 API Key",
        }
    },
)
async def chat(payload: ChatRequest, db: DbSession, user: CurrentUser) -> StreamingResponse:
    """和 Agent 对话。

    返回的是一个 SSE 流，不是普通 JSON。前端需要用 fetch +
    ReadableStream 手动解析（见 frontend/src/api/sse.ts）。
    """
    _require_llm()

    return StreamingResponse(
        run_chat_stream(db, user, payload.message, payload.conversation_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/actions/{action_id}/approve",
    summary="确认执行（SSE 流式）",
    description="用户确认后真正执行批量操作，逐条流式返回执行进度。",
)
async def approve_action(
    action_id: str,
    db: DbSession,
    user: CurrentUser,
    payload: ApproveRequest | None = None,
) -> StreamingResponse:
    """确认并执行待确认的批量操作。

    为什么确认也要走 SSE？
    因为批量执行 17 条工单需要好几秒。返回普通 JSON 的话，
    用户只能盯着转圈等全部执行完。走 SSE 可以逐条反馈进度，
    某一条失败时也能立刻看到。
    """
    return StreamingResponse(
        run_approve_stream(
            db,
            user,
            action_id,
            approve=True,
            edited_payload=payload.edited_payload if payload else None,
            note=payload.note if payload else None,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/actions/{action_id}/reject",
    summary="拒绝执行（SSE 流式）",
    description="用户拒绝后终止本次操作，不做任何数据修改。",
)
async def reject_action(
    action_id: str,
    db: DbSession,
    user: CurrentUser,
    payload: RejectRequest | None = None,
) -> StreamingResponse:
    """拒绝待确认的批量操作。"""
    return StreamingResponse(
        run_approve_stream(
            db,
            user,
            action_id,
            approve=False,
            note=payload.note if payload else None,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ══════════════════════════════════════════════════════════════
# 待确认操作
# ══════════════════════════════════════════════════════════════


@router.get(
    "/actions/pending",
    response_model=list[PendingActionOut],
    summary="我的待确认列表",
)
async def list_pending_actions(
    db: DbSession, user: CurrentUser
) -> list[PendingActionOut]:
    """当前用户所有待确认的操作。前端用它显示红点提醒。"""
    actions = await AgentService.list_pending_actions(db, user)
    return [PendingActionOut.model_validate(a) for a in actions]


@router.get(
    "/actions/{action_id}",
    response_model=PendingActionDetail,
    summary="待确认详情",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_pending_action(
    action_id: str, db: DbSession, user: CurrentUser
) -> PendingActionDetail:
    """待确认操作的完整信息，包含逐条预览和过期时间。"""
    action = await AgentService.get_pending_action(db, action_id, user)
    return PendingActionDetail.model_validate(action)


# ══════════════════════════════════════════════════════════════
# 运行记录
# ══════════════════════════════════════════════════════════════


@router.get(
    "/runs/{run_id}",
    response_model=RunOut,
    summary="运行详情",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_run(
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    with_steps: Annotated[bool, Query(description="是否带执行明细")] = True,
) -> RunOut:
    """查询一次运行的详情。

    用途：用户刷新页面后，用 run_id 把执行过程完整回放出来。
    这就是 agent_run_steps 单独存表的价值 —— 过程可回溯。
    """
    run = await AgentService.get_run(db, run_id, user)

    out = RunOut.model_validate(run)
    if with_steps:
        steps = await AgentService.list_steps(db, run_id)
        out.steps = [RunStepOut.model_validate(s) for s in steps]
    return out
