"""只读工具。

全部无副作用，可以直接执行，不需要人工确认。

设计铁律（整个 tools/ 目录都遵守）：
    ① 不写一行 SQL，全部通过 Service 层
    ② 身份来自 ToolContext，不接受 LLM 传 user_id
    ③ 出错返回 {"ok": false, "error": "..."} 而不是抛异常
    ④ 返回值精简 —— 这些内容会进 LLM 的上下文，字段越多越烧 token
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.context import ToolContext
from app.core.logging import get_logger
from app.schemas.ticket import TicketListQuery
from app.services.dashboard_service import DashboardService
from app.services.ticket_service import TicketService

logger = get_logger(__name__)


def _ok(**kwargs: Any) -> dict[str, Any]:
    """成功返回的统一外壳。"""
    return {"ok": True, **kwargs}


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    """失败返回的统一外壳。

    ⚠️ 工具出错时【不能抛异常】，要返回结构化的错误信息。

    原因：返回的错误会作为 ToolMessage 回灌给 LLM，模型看到
    "工单不存在" 就能自己调整策略（换个条件再查，或者告诉用户）。
    而如果抛异常，整个图就中断了，模型根本没有挽救的机会。

    这也是为什么错误信息要写成人话 —— 它是给模型看的提示，不是给日志看的。
    """
    return {"ok": False, "error": message, **extra}


# ══════════════════════════════════════════════════════════════
# 1. 查工单列表
# ══════════════════════════════════════════════════════════════


@tool
async def list_tickets(
    status: list[str] | None = None,
    priority: list[str] | None = None,
    category: list[str] | None = None,
    keyword: str | None = None,
    assignee_id: int | None = None,
    department_id: int | None = None,
    unhandled_hours: int | None = None,
    limit: int = 20,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """查询工单列表。当用户想了解"有哪些工单"时使用。

    这是最常用的工具，支持组合筛选条件。

    Args:
        status: 状态筛选。可选值：pending(待处理)、processing(处理中)、
            waiting(等待中)、resolved(已解决)、closed(已关闭)。
            可以传多个，如 ["pending", "processing"]。
        priority: 优先级筛选。可选值：low、medium、high、urgent。
        category: 分类筛选。可选值：account、payment、technical、
            logistics、operation、other。
        keyword: 在标题和描述里做模糊搜索。
        assignee_id: 按负责人筛选。
        department_id: 按负责部门筛选。
        unhandled_hours: ★ 只返回"超过 N 小时仍未处理"的工单。
            用户说"超过 24 小时没处理的工单"时，用 unhandled_hours=24。
            注意：这个参数会自动限定状态为 pending（待处理），
            不需要再另外传 status。
        limit: 最多返回多少条，默认 20，最大 100。
            ★ 如果要基于结果做【批量操作】，必须保证拿到全部数据：
              把 limit 设大（如 100），并检查返回的 truncated 字段。
              truncated 为 true 说明还有工单没返回，此时【不能】直接做批量操作，
              否则会漏掉一部分 —— 用户说的是"所有"，漏了却不会有任何提示。

    Returns:
        {"ok": true, "total": 总数, "count": 本次返回条数,
         "truncated": 是否还有更多, "items": [工单摘要...]}

        每个 item 里都有 id 字段 —— 批量操作要用的是它，不是 ticket_no。
    """
    ctx = ToolContext.from_config(config)

    # 把 limit 夹在合理区间，防止模型传个 10000 把上下文撑爆。
    # 上限设成 100 而不是 50：场景 4 需要一次拿到【全部】符合条件的工单，
    # 上限太小会导致"用户说要全部，实际只处理了前 N 条"的静默错误。
    limit = max(1, min(limit, 100))

    query = TicketListQuery(
        status=status,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        keyword=keyword,
        assignee_id=assignee_id,
        department_id=department_id,
        unhandled_hours=unhandled_hours,
        page=1,
        page_size=limit,
    )

    # 数据权限由 Service 强制生效 —— engineer 调这个工具也只能看到自己的工单
    page = await TicketService.list_tickets(ctx.db, query, user=ctx.user)

    truncated = page.total > len(page.items)

    return _ok(
        total=page.total,
        count=len(page.items),
        # ★ truncated 这个字段非常重要，提示词里会告诉模型如何处理它
        truncated=truncated,
        items=[
            {
                # ★★★ id 必须有！★★★
                #
                # 批量工具（batch_update_tickets / batch_assign_tickets）
                # 要的是 ticket_ids（主键 ID），不是工单号。
                #
                # 如果这里不返回 id，模型就只能【自己编】——
                # 而编出来的 ID 如果恰好对应真实存在的工单，
                # 就会改错数据，而且用户从数字上完全看不出来。
                # 这是比"报错"危险得多的静默错误。
                #
                # 这个 bug 是 test_agent.py 跑场景 4 时抓出来的：
                # 计划显示"将 20 条工单改为高优先级"，但工具返回的
                # 数据里根本没有 id 字段。
                "id": t.id,
                "ticket_no": t.ticket_no,
                "title": t.title,
                "status": t.status.value if hasattr(t.status, "value") else t.status,
                "priority": t.priority.value if hasattr(t.priority, "value") else t.priority,
                "assignee": t.assignee_name,
                "sla_status": t.sla_status,
                # 去掉了 category 和 created_at：
                # 批量场景用不到，但每条能省约 40 字符 ——
                # 100 条就是 4000 字符，直接影响能不能一次拿全。
            }
            for t in page.items
        ],
        hint=(
            f"还有 {page.total - len(page.items)} 条未返回，"
            f"请用更大的 limit 重新查询以获得完整列表"
            if truncated
            else None
        ),
    )


# ══════════════════════════════════════════════════════════════
# 2. 查工单详情
# ══════════════════════════════════════════════════════════════


@tool
async def get_ticket(
    ticket_no: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """按工单号查询单张工单的详细信息。

    用户提到具体工单号（如 OPS-202609-000123）时使用。

    Args:
        ticket_no: 工单号，形如 OPS-202609-000123。

    Returns:
        {"ok": true, "ticket": {...}} 或 {"ok": false, "error": "..."}
    """
    ctx = ToolContext.from_config(config)

    # 工单号 → 主键 id 的转换交给 Service，工具层不写 SQL
    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)

    if ticket_id is None:
        # 返回人话错误，模型可以据此告诉用户"没找到这个工单"
        return _fail(f"找不到工单号 {ticket_no}")

    try:
        detail = await TicketService.get_detail(ctx.db, ticket_id, user=ctx.user)
    except Exception as exc:  # noqa: BLE001
        # Service 层会因权限不足抛 NotFoundError（故意不区分"不存在"和"无权限"）
        logger.info("get_ticket 失败 | %s | %s", ticket_no, exc)
        return _fail(f"无法查看工单 {ticket_no}（可能不存在或你没有权限）")

    return _ok(
        ticket={
            "ticket_no": detail.ticket_no,
            "title": detail.title,
            "description": detail.description,
            "status": detail.status.value if hasattr(detail.status, "value") else detail.status,
            "priority": detail.priority.value if hasattr(detail.priority, "value") else detail.priority,
            "category": detail.category.value if hasattr(detail.category, "value") else detail.category,
            "creator": detail.creator_name,
            "assignee": detail.assignee_name,
            "department": detail.department_name,
            "sla_deadline": detail.sla_deadline.isoformat() if detail.sla_deadline else None,
            "sla_status": detail.sla_status,
            "sla_remaining_minutes": detail.sla_remaining_minutes,
            "created_at": detail.created_at.isoformat() if detail.created_at else None,
            "comment_count": len(detail.comments),
            "last_comments": [
                {"author": c.author_name, "content": c.content[:200]}
                for c in detail.comments[-3:]  # 只带最近 3 条，控制 token
            ],
        }
    )


# ══════════════════════════════════════════════════════════════
# 3. 统计
# ══════════════════════════════════════════════════════════════


@tool
async def get_ticket_statistics(
    days: int | None = None,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """获取工单的数量统计。用户问"有多少工单""分布情况"时使用。

    也适合用来做"最近 N 天的情况"这类分析。

    Args:
        days: 只统计最近 N 天的工单。不传则统计全部。

    Returns:
        {"ok": true, "total": 总数, "by_status": {...}, "by_priority": {...},
         "by_category": {...}, "overdue": 超时数, "unassigned": 未分派数}
    """
    ctx = ToolContext.from_config(config)

    stats = await TicketService.get_statistics(ctx.db, user=ctx.user, days=days)

    return _ok(
        total=stats.total,
        by_status=stats.by_status,
        by_priority=stats.by_priority,
        by_category=stats.by_category,
        overdue=stats.overdue,
        unassigned=stats.unassigned,
        days=days,
    )


# ══════════════════════════════════════════════════════════════
# 4. SLA 风险分析
# ══════════════════════════════════════════════════════════════


@tool
async def analyze_sla_risk(
    days: int = 7,
    limit: int = 10,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """分析 SLA 风险。用户问"SLA 风险""哪些工单快超时了"时使用。

    返回已超时和即将超时的工单清单，按紧急程度排序。

    Args:
        days: 分析最近 N 天创建的工单，默认 7 天。
        limit: 最多返回多少条最紧急的工单，默认 10。

    Returns:
        {"ok": true, "total_overdue": 已超时数, "total_at_risk": 即将超时数,
         "items": [最紧急的工单...]}
    """
    ctx = ToolContext.from_config(config)
    limit = max(1, min(limit, 30))

    # 复用 Dashboard 的 SLA 风险逻辑 —— 和前端看板用的是同一份实现，
    # 保证"AI 说的"和"页面上显示的"永远一致
    risk = await DashboardService.get_sla_risk(ctx.db, ctx.user, limit=limit)

    return _ok(
        total_overdue=risk.total_overdue,
        total_at_risk=risk.total_at_risk,
        items=[
            {
                "ticket_no": t.ticket_no,
                "title": t.title,
                "status": t.status.value if hasattr(t.status, "value") else t.status,
                "priority": t.priority.value if hasattr(t.priority, "value") else t.priority,
                "assignee": t.assignee_name,
                "sla_status": t.sla_status,
                "sla_deadline": t.sla_deadline.isoformat() if t.sla_deadline else None,
            }
            for t in risk.items
        ],
    )


# ══════════════════════════════════════════════════════════════
# 5. 工单总结材料
# ══════════════════════════════════════════════════════════════


@tool
async def summarize_ticket(
    ticket_no: str,
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """获取一张工单的完整处理脉络，用于生成总结。

    当用户说"总结一下这个工单""这个工单是怎么回事"时使用。
    返回工单信息 + 全部评论 + 完整操作记录的紧凑时间线。

    ⚠️ 注意：这个工具【不生成总结文字】，只负责把材料收集齐。
    总结由你（模型）根据返回的内容来写。

    为什么不在这里调模型做总结？
      工具里再调一次模型会让调用链变成"模型→工具→模型"，
      耗时翻倍、成本翻倍，而且第二个模型的输出你没法控制。
      正确做法是工具只取数据，推理和表达统一在你这一层完成。

    Args:
        ticket_no: 工单号。

    Returns:
        {"ok": true, "ticket": {...}, "timeline": [...]}
    """
    ctx = ToolContext.from_config(config)

    ticket_id = await TicketService.get_id_by_no(ctx.db, ticket_no)
    if ticket_id is None:
        return _fail(f"找不到工单号 {ticket_no}")

    try:
        detail = await TicketService.get_detail(ctx.db, ticket_id, user=ctx.user)
    except Exception:  # noqa: BLE001
        return _fail(f"无法查看工单 {ticket_no}（可能不存在或你没有权限）")

    # 把评论和操作记录合并成一条时间线，按时间排序。
    # 合成一条线比给模型两个独立列表更容易理解"事情的先后顺序"。
    timeline: list[dict[str, Any]] = []
    for e in detail.events:
        timeline.append(
            {
                "time": e.created_at.isoformat() if e.created_at else None,
                "who": e.actor_name or "系统",
                "what": e.event_type.value if hasattr(e.event_type, "value") else e.event_type,
                "detail": e.event_data,
            }
        )
    for cm in detail.comments:
        timeline.append(
            {
                "time": cm.created_at.isoformat() if cm.created_at else None,
                "who": cm.author_name or "未知",
                "what": "comment",
                "detail": {"content": cm.content},
            }
        )
    timeline.sort(key=lambda x: x["time"] or "")

    return _ok(
        ticket={
            "ticket_no": detail.ticket_no,
            "title": detail.title,
            "description": detail.description,
            "status": detail.status.value if hasattr(detail.status, "value") else detail.status,
            "priority": detail.priority.value if hasattr(detail.priority, "value") else detail.priority,
            "creator": detail.creator_name,
            "assignee": detail.assignee_name,
            "created_at": detail.created_at.isoformat() if detail.created_at else None,
            "resolved_at": detail.resolved_at.isoformat() if detail.resolved_at else None,
            "sla_status": detail.sla_status,
        },
        timeline=timeline,
    )


# ══════════════════════════════════════════════════════════════
# 6. 部门列表（元数据）
# ══════════════════════════════════════════════════════════════


@tool
async def list_departments(
    config: RunnableConfig = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """获取所有部门的列表（含 id 和名称）。

    ★ 这个工具是场景 4 的必要前提。

    用户说"分配给技术部门"时，你拿到的是【中文名称】，
    但 assign_ticket 需要的是【department_id】。
    所以必须先用这个工具把名称映射成 id。

    同理，如果用户说"分配给张三"，也需要先知道张三的 id。

    Returns:
        {"ok": true, "departments": [{"id": 1, "name": "技术部", "code": "tech"}, ...]}
    """
    ctx = ToolContext.from_config(config)

    departments = await TicketService.list_departments(ctx.db)

    return _ok(
        departments=[{"id": d.id, "name": d.name, "code": d.code} for d in departments]
    )
