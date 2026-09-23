"""Agent 运行记录服务。

负责所有 Agent 相关的数据库记账：会话、消息、运行、执行明细、待确认操作。

为什么不把这些塞进 runner.py？
    因为 runner 的职责是"编排执行流程"，记账是另一件事。
    混在一起会让 runner 变得又长又难改 —— 而且 Agent 工具
    以后也可能需要查这些数据（比如"上次那个批量操作怎么样了"）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.models.agent import (
    AgentConversation,
    AgentMessage,
    AgentPendingAction,
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    PendingActionStatus,
)
from app.models.user import User, UserRole

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AgentService:
    """Agent 运行记录。"""

    # ══════════════════════════════════════════════════════
    # 会话
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def get_or_create_conversation(
        db: AsyncSession,
        user: User,
        conversation_id: int | None = None,
        *,
        title: str | None = None,
    ) -> AgentConversation:
        """取会话，没有就新建。

        前端第一次发消息时不传 conversation_id，后端自动建一个。
        """
        if conversation_id is not None:
            conv = await db.get(AgentConversation, conversation_id)
            if conv is None:
                raise NotFoundError("会话不存在")
            # ★ 越权检查：不能通过猜 id 读到别人的会话
            if conv.user_id != user.id:
                raise ForbiddenError("无权访问该会话")
            return conv

        conv = AgentConversation(
            user_id=user.id,
            title=(title or "新对话")[:100],
            last_active_at=_now(),
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv

    @staticmethod
    async def list_conversations(
        db: AsyncSession, user: User, *, limit: int = 30
    ) -> list[AgentConversation]:
        """当前用户的会话列表，按最后活跃时间倒序。"""
        stmt = (
            select(AgentConversation)
            .where(AgentConversation.user_id == user.id)
            .order_by(AgentConversation.last_active_at.desc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    async def list_messages(
        db: AsyncSession, conversation_id: int, *, limit: int = 200
    ) -> list[AgentMessage]:
        """会话的消息历史，按时间正序。"""
        stmt = (
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation_id)
            .order_by(AgentMessage.created_at.asc(), AgentMessage.id.asc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    async def add_message(
        db: AsyncSession,
        *,
        conversation_id: int,
        role: str,
        content: str,
        run_id: str | None = None,
        commit: bool = True,
    ) -> AgentMessage:
        """存一条对话消息。"""
        msg = AgentMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            run_id=run_id,
        )
        db.add(msg)

        # 顺手更新会话的活跃时间，让会话列表排序正确
        conv = await db.get(AgentConversation, conversation_id)
        if conv is not None:
            conv.last_active_at = _now()
            # 第一次对话时用用户的话做标题
            if conv.title == "新对话" and role == "user":
                conv.title = content[:50]

        if commit:
            await db.commit()
            await db.refresh(msg)
        return msg

    # ══════════════════════════════════════════════════════
    # 运行
    # ══════════════════════════════════════════════════════

    @staticmethod
    def new_run_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    async def create_run(
        db: AsyncSession,
        *,
        run_id: str,
        conversation_id: int,
        user_id: int,
        user_input: str,
    ) -> AgentRun:
        """创建一次运行记录。"""
        run = AgentRun(
            run_id=run_id,
            conversation_id=conversation_id,
            user_id=user_id,
            user_input=user_input,
            status=AgentRunStatus.RUNNING,
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return run

    @staticmethod
    async def finish_run(
        db: AsyncSession,
        run_id: str,
        *,
        status: str,
        intent: str | None = None,
        answer: str | None = None,
        error: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tool_call_count: int = 0,
        duration_ms: int | None = None,
    ) -> None:
        """更新运行结果。"""
        stmt = select(AgentRun).where(AgentRun.run_id == run_id)
        run = (await db.execute(stmt)).scalars().first()
        if run is None:
            logger.warning("finish_run 找不到记录 | run_id=%s", run_id)
            return

        run.status = status
        if intent is not None:
            run.intent = intent
        if answer is not None:
            run.answer = answer
        if error is not None:
            run.error = error
        run.prompt_tokens = prompt_tokens
        run.completion_tokens = completion_tokens
        run.tool_call_count = tool_call_count
        run.duration_ms = duration_ms

        await db.commit()

    @staticmethod
    async def get_run(db: AsyncSession, run_id: str, user: User) -> AgentRun:
        """按 run_id 查运行记录（含越权检查）。"""
        stmt = select(AgentRun).where(AgentRun.run_id == run_id)
        run = (await db.execute(stmt)).scalars().first()
        if run is None:
            raise NotFoundError("运行记录不存在")
        if run.user_id != user.id and user.role != UserRole.ADMIN:
            raise ForbiddenError("无权查看该运行记录")
        return run

    # ══════════════════════════════════════════════════════
    # 执行明细
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def add_step(
        db: AsyncSession,
        *,
        run_id: str,
        step_type: str,
        node: str | None = None,
        tool_name: str | None = None,
        input_data: dict[str, Any] | None = None,
        output_data: dict[str, Any] | None = None,
        status: str = "ok",
        duration_ms: int | None = None,
        commit: bool = False,
    ) -> None:
        """记一步执行明细。

        序号自动递增：查当前最大序号 + 1。
        单次运行内是串行的，不会有并发问题。
        """
        max_seq = (
            await db.execute(
                select(func.max(AgentRunStep.seq)).where(AgentRunStep.run_id == run_id)
            )
        ).scalar_one_or_none() or 0

        db.add(
            AgentRunStep(
                run_id=run_id,
                seq=max_seq + 1,
                step_type=step_type,
                node=node,
                tool_name=tool_name,
                input_data=input_data,
                output_data=output_data,
                status=status,
                duration_ms=duration_ms,
            )
        )
        if commit:
            await db.commit()

    @staticmethod
    async def list_steps(db: AsyncSession, run_id: str) -> list[AgentRunStep]:
        """某次运行的全部执行明细（按序号排序，用于回放）。"""
        stmt = (
            select(AgentRunStep)
            .where(AgentRunStep.run_id == run_id)
            .order_by(AgentRunStep.seq.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    # ══════════════════════════════════════════════════════
    # 待确认操作（HITL）
    # ══════════════════════════════════════════════════════

    @staticmethod
    async def get_pending_action(
        db: AsyncSession, action_id: str, user: User
    ) -> AgentPendingAction:
        """取待确认操作，并检查归属。"""
        stmt = select(AgentPendingAction).where(
            AgentPendingAction.action_id == action_id
        )
        action = (await db.execute(stmt)).scalars().first()
        if action is None:
            raise NotFoundError("待确认操作不存在")

        # 谁能批：发起人本人，或管理员
        if action.requested_by != user.id and user.role != UserRole.ADMIN:
            raise ForbiddenError("无权操作该待确认项")
        return action

    @staticmethod
    async def list_pending_actions(
        db: AsyncSession, user: User, *, only_pending: bool = True
    ) -> list[AgentPendingAction]:
        """当前用户的待确认列表（前端用来显示红点）。"""
        stmt = select(AgentPendingAction).where(
            AgentPendingAction.requested_by == user.id
        )
        if only_pending:
            stmt = stmt.where(AgentPendingAction.status == PendingActionStatus.PENDING)
        stmt = stmt.order_by(AgentPendingAction.created_at.desc()).limit(50)
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    async def decide_action(
        db: AsyncSession,
        action: AgentPendingAction,
        *,
        approve: bool,
        user: User,
        edited_payload: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> AgentPendingAction:
        """记录审批决定。

        ★ 这里的并发保护很关键。

        用户可能双击"确认"按钮，或者网络重试导致请求发两次。
        如果不做保护，批量操作会被执行两遍 —— 数据就错了。

        保护方式：只有状态是 PENDING 的才能被决定。
        第二次请求进来时状态已经是 APPROVED，直接抛 409。
        这就是"幂等性"的实现方式。
        """
        if action.status != PendingActionStatus.PENDING:
            raise ConflictError(
                f"该操作已经是「{action.status}」状态，不能重复处理",
                detail={"action_id": action.action_id, "status": action.status},
            )

        # 过期检查：避免用户放着不管几小时后突然点确认
        if action.expires_at < _now():
            action.status = PendingActionStatus.EXPIRED
            await db.commit()
            raise ConflictError(
                "该操作已超时失效，请重新发起",
                detail={"action_id": action.action_id},
            )

        action.status = (
            PendingActionStatus.APPROVED if approve else PendingActionStatus.REJECTED
        )
        action.decided_by = user.id
        action.decided_at = _now()
        action.decision_note = note

        # 用户可以在确认时改勾选（比如从 17 条改成 15 条）
        if approve and edited_payload:
            ids = edited_payload.get("ticket_ids")
            if isinstance(ids, list) and ids:
                action.payload = {**action.payload, "args": {**action.payload.get("args", {}), "ticket_ids": ids}}
                action.affected_count = len(ids)

        await db.commit()
        await db.refresh(action)

        logger.info(
            "审批决定 | action_id=%s approve=%s by=%s affected=%s",
            action.action_id,
            approve,
            user.username,
            action.affected_count,
        )
        return action

    @staticmethod
    async def mark_action_result(
        db: AsyncSession,
        action: AgentPendingAction,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """记录执行结果。"""
        action.status = status
        action.executed_at = _now()
        action.result = result
        await db.commit()

    @staticmethod
    async def expire_stale_actions(db: AsyncSession) -> int:
        """把超时未处理的待确认项标记为过期。

        由定时任务调用。不清理的话，这些记录会一直挂在"待确认"状态，
        前端红点永远消不掉。

        Returns:
            本次过期的条数
        """
        from sqlalchemy import update

        stmt = (
            update(AgentPendingAction)
            .where(AgentPendingAction.status == PendingActionStatus.PENDING)
            .where(AgentPendingAction.expires_at < _now())
            .values(status=PendingActionStatus.EXPIRED)
        )
        result = await db.execute(stmt)
        await db.commit()

        count = result.rowcount or 0
        if count:
            logger.info("清理超时待确认项 | %s 条", count)
        return count

    @staticmethod
    def approval_deadline() -> datetime:
        """算出一个新的待确认项的过期时间。"""
        return _now() + timedelta(seconds=settings.AGENT_APPROVAL_TIMEOUT)
