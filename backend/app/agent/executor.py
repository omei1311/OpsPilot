"""审批通过后的执行器。

★ 这是"两阶段 HITL"的第二阶段。

    阶段一（graph.py 的 guard 节点）：生成计划 → 落库 → 结束
    阶段二（本文件）：用户点确认 → 读计划 → 真正执行

为什么不用 LangGraph 的 interrupt() + checkpointer？
    见 runner.py 顶部的对比说明。简单说：两阶段方案把"等待确认"
    这个状态放在 MySQL 里，服务重启、多 worker 部署都不会丢，
    而且不需要额外引入 Redis。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.agent import AgentPendingAction, PendingActionStatus
from app.models.user import User, UserRole
from app.services.agent_service import AgentService

logger = get_logger(__name__)


# 计划里的工具名 → Service 方法的映射
TOOL_EXECUTORS = {
    "batch_update_tickets": "batch_update",
    "batch_assign_tickets": "batch_assign",
}


async def _execute_ticket_tool(
    db: AsyncSession, tool_name: str, args: dict[str, Any], ticket_ids: list[int], user: User
) -> dict[str, Any]:
    """业务写入边界；缓存/Redis 等依赖只在真正执行工具时加载。"""
    from app.services.ticket_service import TicketService

    allowed_fields = (
        ("priority", "category", "department_id")
        if tool_name == "batch_update_tickets"
        else ("department_id", "assignee_id")
    )
    changes = {key: value for key, value in args.items() if key in allowed_fields and value is not None}
    executor = getattr(TicketService, TOOL_EXECUTORS[tool_name])
    return await executor(db, ticket_ids, operator=user, **changes)


async def execute_action(
    db: AsyncSession,
    action: AgentPendingAction,
    user: User,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """执行一个已批准的待确认操作。

    Args:
        action: 已批准的操作记录
        user:   执行人（用于权限和审计）
        on_progress: 可选的回调，每处理完一条调用一次。
                     用来往 SSE 推送逐条进度 —— 17 条工单要 8 秒，
                     不推送的话用户只能干等。

    Returns:
        执行结果字典，会存进 action.result
    """
    started = time.perf_counter()

    plan = action.payload or {}
    tool_name = plan.get("tool")
    args = dict(plan.get("args") or {})

    # 用户确认时可能改过勾选，以 action.payload 里的最新值为准
    ticket_ids = args.get("ticket_ids") or []

    if not ticket_ids:
        result = {"ok": False, "error": "计划里没有要处理的工单"}
        await AgentService.mark_action_result(
            db, action, status=PendingActionStatus.FAILED, result=result
        )
        return result

    # ── 二次权限校验 ───────────────────────────────────────
    #
    # 为什么明明在决定阶段已经查过权限，这里还要再查一次？
    #
    # 因为两次请求之间可能隔了好几分钟：
    #   · 用户可能已经被降权或停用
    #   · 工单可能已经被转给别人
    # 只信"确认时"的校验结果是不够的。
    #
    # 这就是"TOCTOU（Time-of-Check to Time-of-Use）"问题 ——
    # 检查时刻和使用时刻状态不一致。凡是"先检查、隔一会儿再执行"
    # 的流程都要注意。
    # 请求中的 ORM User 可能仍缓存着审批前的 admin 身份。重新读取并将
    # 最新主体交给业务服务，避免管理员被降权后仍按旧角色批量操作。
    current_user = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .execution_options(populate_existing=True, autoflush=False)
    )
    if current_user is None or not current_user.is_active:
        result = {"ok": False, "error": "当前账号不存在或已被禁用"}
        await AgentService.mark_action_result(
            db, action, status=PendingActionStatus.FAILED, result=result
        )
        return result
    user = current_user
    if action.requested_by != user.id and user.role != UserRole.ADMIN:
        result = {"ok": False, "error": "当前账号已无权执行该审批计划"}
        await AgentService.mark_action_result(
            db, action, status=PendingActionStatus.FAILED, result=result
        )
        return result

    # ── 执行 ───────────────────────────────────────────────
    try:
        if tool_name not in TOOL_EXECUTORS:
            result = {"ok": False, "error": f"不支持的工具：{tool_name}"}
            await AgentService.mark_action_result(
                db, action, status=PendingActionStatus.FAILED, result=result
            )
            return result
        raw = await _execute_ticket_tool(db, tool_name, args, ticket_ids, user)

    except Exception as exc:  # noqa: BLE001
        logger.exception("批量执行失败 | action_id=%s", action.action_id)
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "total": len(ticket_ids),
            "succeeded_count": 0,
            "failed_count": len(ticket_ids),
        }
        await AgentService.mark_action_result(
            db, action, status=PendingActionStatus.FAILED, result=result
        )
        # 把失败事件推给前端，否则用户只看到连接断了不知道为什么
        if on_progress:
            await on_progress({"event": "action_finished", "data": result})
        return result

    duration_ms = int((time.perf_counter() - started) * 1000)

    result = {
        "ok": raw["failed_count"] == 0,
        "total": raw["total"],
        "succeeded_count": raw["succeeded_count"],
        "failed_count": raw["failed_count"],
        "failed": raw["failed"],
        "tool": tool_name,
        "duration_ms": duration_ms,
    }

    # 部分失败也算"执行完成"，但状态上要能看出有失败
    final_status = (
        PendingActionStatus.EXECUTED
        if raw["failed_count"] == 0
        else PendingActionStatus.FAILED
    )
    await AgentService.mark_action_result(db, action, status=final_status, result=result)

    logger.info(
        "批量执行完成 | action_id=%s tool=%s 成功=%s 失败=%s 耗时=%sms",
        action.action_id,
        tool_name,
        raw["succeeded_count"],
        raw["failed_count"],
        duration_ms,
    )

    return result


def summarize_result(result: dict[str, Any]) -> str:
    """把执行结果压成一句给用户看的总结。"""
    if not result.get("ok") and result.get("failed_count") == result.get("total"):
        return f"执行失败：{result.get('error', '未知错误')}"

    total = result.get("total", 0)
    ok_count = result.get("succeeded_count", 0)
    fail_count = result.get("failed_count", 0)

    if fail_count == 0:
        return f"已完成 {ok_count} 条工单的修改。"

    return (
        f"已完成 {ok_count}/{total} 条，{fail_count} 条失败。"
        f"失败原因见下方清单。"
    )
