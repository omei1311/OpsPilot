"""写工具（有副作用）。

和读工具的区别：这些会真的改数据库。

风险分级见 app/agent/policy.py：
    low     直接执行（新增数据，可撤销）
    medium  直接执行（单条修改，影响可控）
    high    必须先人工确认（批量、不可逆）
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.context import ToolContext
from app.core.logging import get_logger
from app.schemas.ticket import (
    TicketAssign,
    TicketCreate,
    TicketStatusUpdate,
    TicketUpdate,
)
from app.services.ticket_service import TicketService

logger = get_logger(__name__)


def _ok(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, **kwargs}


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


# ══════════════════════════════════════════════════════════════
# 1. 创建工单（场景 1）
# ══════════════════════════════════════════════════════════════


@tool
async def create_ticket(
    title: str,
    description: str | None = None,
    category: str = "other",
    priority: str = "medium",
    department_id: int | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """创建一张新工单。用户描述了一个问题并希望"报个故障""建个单子"时使用。

    典型输入："线上支付接口大量出现 502，帮我报个故障。"
    此时你应该提取出：
        title       = "支付接口大量返回 502"
        description = 用户的完整描述
        category    = "payment"
        priority    = "urgent"（"大量""无法支付"说明影响面大）

    Args:
        title: 工单标题，一句话概括问题，2-200 字。
        description: 详细描述，尽量保留用户原话中的关键信息。
        category: 分类。account(账号) / payment(支付) / technical(技术故障) /
            logistics(物流) / operation(运营) / other(其他)。
        priority: 优先级。low / medium / high / urgent。
            出现"大量""全部""无法使用""线上故障"等词时倾向 urgent 或 high。
        department_id: 负责部门 ID。不确定就不传，后续再分派。

    Returns:
        {"ok": true, "ticket_no": "OPS-202609-000123", "id": 123, ...}
    """
    ctx = ToolContext.from_config(config)

    try:
        # 用 Pydantic 模型做二次校验 —— 即使模型传了非法的 category，
        # 也会在这里被拦住并返回明确的错误，而不是写进数据库
        payload = TicketCreate(
            title=title,
            description=description,
            category=category,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            department_id=department_id,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            f"参数不合法：{exc}。"
            f"category 必须是 account/payment/technical/logistics/operation/other 之一，"
            f"priority 必须是 low/medium/high/urgent 之一"
        )

    # ★ 创建人取 ctx.user，不接受模型传的创建人。
    # 否则模型可以把工单伪造成别人提的。
    ticket = await TicketService.create(ctx.db, payload, creator=ctx.user)

    logger.info(
        "Agent 创建工单 | %s | run=%s user=%s",
        ticket.ticket_no,
        ctx.run_id,
        ctx.user.username,
    )

    return _ok(
        ticket_no=ticket.ticket_no,
        id=ticket.id,
        title=ticket.title,
        status=ticket.status.value if hasattr(ticket.status, "value") else ticket.status,
        priority=ticket.priority.value if hasattr(ticket.priority, "value") else ticket.priority,
        sla_deadline=ticket.sla_deadline.isoformat() if ticket.sla_deadline else None,
    )


# ══════════════════════════════════════════════════════════════
# 2. 修改工单
# ══════════════════════════════════════════════════════════════


@tool
async def update_ticket(
    ticket_no: str,
    title: str | None = None,
    description: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """修改单张工单的信息。用户说"把这张工单改成...""调整下优先级"时使用。

    ⚠️ 只改传了值的字段，没传的保持不变。

    Args:
        ticket_no: 工单号。
        title: 新标题。
        description: 新描述。
        category: 新分类。
        priority: 新优先级。注意：改优先级会【自动重新计算 SLA 截止时间】。

    Returns:
        {"ok": true, "ticket_no": ..., "changed": ["priority"]}
    """
    ctx = ToolContext.from_config(config)

    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)
    if ticket_id is None:
        return _fail(f"找不到工单号 {ticket_no}")

    # exclude_unset 的效果在这里很关键：
    # 只把用户真正提到的字段放进更新数据，没提到的不会被清空
    payload = TicketUpdate(
        title=title,
        description=description,
        category=category,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
    )
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        return _fail("没有指定要修改的内容")

    try:
        updated = await TicketService.update(ctx.db, ticket_id, payload, operator=ctx.user)
    except Exception as exc:  # noqa: BLE001
        logger.info("update_ticket 失败 | %s | %s", ticket_no, exc)
        return _fail(f"修改失败：{exc}")

    return _ok(
        ticket_no=updated.ticket_no,
        changed=list(changes.keys()),
        priority=updated.priority.value if hasattr(updated.priority, "value") else updated.priority,
        sla_deadline=updated.sla_deadline.isoformat() if updated.sla_deadline else None,
    )


# ══════════════════════════════════════════════════════════════
# 3. 分派
# ══════════════════════════════════════════════════════════════


@tool
async def assign_ticket(
    ticket_no: str,
    department_id: int | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """把工单分派给某个部门。用户说"分配给技术部门""转给运维"时使用。

    ★ 用户通常说的是【部门名称】（"技术部"），而这里要的是【部门 ID】。
    所以你需要先用 list_departments 拿到名称到 id 的映射，再调用本工具。

    Args:
        ticket_no: 工单号。
        department_id: 部门 ID（不是名称）。先用 list_departments 查询。

    Returns:
        {"ok": true, "ticket_no": ..., "department": "技术部", "status": "processing"}
    """
    ctx = ToolContext.from_config(config)

    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)
    if ticket_id is None:
        return _fail(f"找不到工单号 {ticket_no}")

    if department_id is None:
        return _fail("必须提供 department_id。可以先用 list_departments 查询部门列表")

    try:
        payload = TicketAssign(department_id=department_id)
        updated = await TicketService.assign(ctx.db, ticket_id, payload, operator=ctx.user)
    except Exception as exc:  # noqa: BLE001
        logger.info("assign_ticket 失败 | %s | %s", ticket_no, exc)
        return _fail(f"分派失败：{exc}")

    return _ok(
        ticket_no=updated.ticket_no,
        department=updated.department_name,
        status=updated.status.value if hasattr(updated.status, "value") else updated.status,
    )


# ══════════════════════════════════════════════════════════════
# 4. 变更状态
# ══════════════════════════════════════════════════════════════


@tool
async def change_ticket_status(
    ticket_no: str,
    status: str,
    reason: str | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """变更工单状态。用户说"把这个工单关掉""标记为已解决"时使用。

    ⚠️ 状态流转有严格限制（状态机），不是任意跳转：
        pending(待处理)    → processing(处理中) / closed(已关闭)
        processing(处理中) → waiting(等待中) / resolved(已解决) / pending
        waiting(等待中)    → processing / closed
        resolved(已解决)   → closed / processing
        closed(已关闭)     → 终态，不能再变

    比如不能把 pending 直接改成 resolved，必须先经过 processing。

    Args:
        ticket_no: 工单号。
        status: 目标状态。
        reason: 变更原因（可选）。

    Returns:
        {"ok": true, ...} 或 {"ok": false, "error": "...", "allowed": [...]}
    """
    ctx = ToolContext.from_config(config)

    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)
    if ticket_id is None:
        return _fail(f"找不到工单号 {ticket_no}")

    try:
        payload = TicketStatusUpdate(status=status, reason=reason)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return _fail(
            f"状态 {status} 不合法。"
            f"可选值：pending / processing / waiting / resolved / closed"
        )

    try:
        updated = await TicketService.change_status(
            ctx.db, ticket_id, payload, operator=ctx.user
        )
    except Exception as exc:  # noqa: BLE001
        # 状态机拒绝时，把"允许流转到哪些状态"一并返回。
        # 这样模型能立刻知道正确的做法，而不是反复试错。
        allowed = TicketService.ALLOWED_TRANSITIONS.get(
            getattr(exc, "detail", {}).get("from", ""), set()
        ) if getattr(exc, "detail", None) else set()
        return _fail(f"状态变更失败：{exc}", allowed=sorted(allowed) if allowed else None)

    return _ok(
        ticket_no=updated.ticket_no,
        status=updated.status.value if hasattr(updated.status, "value") else updated.status,
    )


# ══════════════════════════════════════════════════════════════
# 5. 添加评论
# ══════════════════════════════════════════════════════════════


@tool
async def add_ticket_comment(
    ticket_no: str,
    content: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """给工单添加一条评论。用户说"帮我留言""补充一下说明"时使用。

    Args:
        ticket_no: 工单号。
        content: 评论内容。

    Returns:
        {"ok": true, "comment_id": ...}
    """
    ctx = ToolContext.from_config(config)

    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)
    if ticket_id is None:
        return _fail(f"找不到工单号 {ticket_no}")

    if not content or not content.strip():
        return _fail("评论内容不能为空")

    try:
        comment = await TicketService.add_comment(
            ctx.db, ticket_id, content.strip(), author=ctx.user
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"添加评论失败：{exc}")

    return _ok(comment_id=comment.id, ticket_no=ticket_no)
