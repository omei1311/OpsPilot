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

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
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


def _approved_payload(
    payload: dict[str, Any], edited_payload: dict[str, Any] | None
) -> tuple[dict[str, Any], int]:
    """确认时仅允许缩小已持久化的目标集合，不能修改工具或业务参数。"""
    args = payload.get("args") if isinstance(payload, dict) else None
    original_ids = args.get("ticket_ids") if isinstance(args, dict) else None
    if (
        not isinstance(original_ids, list)
        or not original_ids
        or any(type(ticket_id) is not int or ticket_id <= 0 for ticket_id in original_ids)
        or len(set(original_ids)) != len(original_ids)
    ):
        raise ConflictError("原执行计划的工单范围无效，请重新生成计划")

    selected_ids = original_ids
    if edited_payload is not None:
        if not isinstance(edited_payload, dict) or set(edited_payload) != {"ticket_ids"}:
            raise BadRequestError("确认时仅允许修改 ticket_ids 勾选范围")
        selected_ids = edited_payload["ticket_ids"]
        if (
            not isinstance(selected_ids, list)
            or not selected_ids
            or any(type(ticket_id) is not int or ticket_id <= 0 for ticket_id in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)
        ):
            raise BadRequestError("请选择不重复的正整数工单 ID，空选请拒绝本次操作")
        if not set(selected_ids).issubset(original_ids):
            raise BadRequestError("只能取消原计划中的工单，不能添加新的执行目标")

    if len(selected_ids) > settings.AGENT_BATCH_LIMIT:
        raise BadRequestError(f"单次最多处理 {settings.AGENT_BATCH_LIMIT} 条工单")

    return {**payload, "args": {**args, "ticket_ids": list(selected_ids)}}, len(selected_ids)


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
        """由数据库原子抢占审批决定；只有获胜的请求可以进入执行流程。

        Python 内判断状态不能保护同时读到 pending 的两个请求。这里用
        UPDATE 的状态、有效期和权限条件决定唯一获胜者；失败请求回滚，
        不能覆盖已批准、拒绝或过期的决定。不自动重试已批准的执行，
        也不把这一保证扩大为进程崩溃后的 exactly-once 业务执行。
        """
        action_id, user_id = action.action_id, user.id
        # 原对象可能在请求排队期间过时。重新读取持久化的计划与用户权限；
        # populate_existing 避免 identity map 返回之前缓存的属性。
        action = await db.scalar(
            select(AgentPendingAction)
            .where(AgentPendingAction.action_id == action_id)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if action is None:
            raise NotFoundError("待确认操作不存在")
        current_user = await db.scalar(
            select(User)
            .where(User.id == user_id)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if current_user is None or not current_user.is_active:
            raise ForbiddenError("当前账号不可执行审批")
        if action.requested_by != user_id and current_user.role != UserRole.ADMIN:
            raise ForbiddenError("无权操作该待确认项")
        if action.status != PendingActionStatus.PENDING:
            raise ConflictError(
                f"该操作已经是「{action.status}」状态，不能重复处理",
                detail={"action_id": action.action_id, "status": action.status},
            )

        # 将权限放进 UPDATE 条件，覆盖用户在上面读取之后被停用/降权的情况。
        authorized = exists(
            select(User.id).where(
                User.id == user_id,
                User.is_active.is_(True),
                or_(User.role == UserRole.ADMIN, AgentPendingAction.requested_by == user_id),
            )
        )
        now = _now()
        expired = action.expires_at <= now
        values: dict[str, Any]
        if expired:
            values = {"status": PendingActionStatus.EXPIRED}
        else:
            values = {
                "status": PendingActionStatus.APPROVED if approve else PendingActionStatus.REJECTED,
                "decided_by": user_id,
                "decided_at": now,
                "decision_note": note,
            }
            if approve:
                values["payload"], values["affected_count"] = _approved_payload(
                    action.payload, edited_payload
                )

        stmt = (
            update(AgentPendingAction)
            .where(
                AgentPendingAction.action_id == action_id,
                AgentPendingAction.status == PendingActionStatus.PENDING,
                authorized,
                AgentPendingAction.expires_at <= now
                if expired
                else AgentPendingAction.expires_at > now,
            )
            .values(**values)
            .execution_options(synchronize_session=False, autoflush=False)
        )
        result = await db.execute(stmt)
        if result.rowcount != 1:
            await db.rollback()
            raise ConflictError(
                "该操作的状态、有效期或审批权限已变化，请刷新后重试",
                detail={"action_id": action_id},
            )
        await db.commit()
        await db.refresh(action)
        if expired:
            raise ConflictError("该操作已超时失效，请重新发起", detail={"action_id": action_id})

        logger.info(
            "审批决定 | action_id=%s approve=%s by=%s affected=%s",
            action.action_id,
            approve,
            user_id,
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
        stmt = (
            update(AgentPendingAction)
            .where(AgentPendingAction.status == PendingActionStatus.PENDING)
            .where(AgentPendingAction.expires_at <= _now())
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
