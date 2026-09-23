"""工单接口。

风格和 auth.py 完全一致 —— 声明路由、声明依赖、转手给 Service。
这个文件里没有一行 SQL，也没有一条业务规则。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession
from app.models.ticket import TicketCategory, TicketPriority, TicketStatus
from app.schemas.common import ErrorResponse, PageResult
from app.schemas.ticket import (
    TicketAssign,
    TicketBrief,
    TicketCommentCreate,
    TicketCommentOut,
    TicketCreate,
    TicketDetailOut,
    TicketListQuery,
    TicketOut,
    TicketStats,
    TicketStatusUpdate,
    TicketUpdate,
)
from app.services.ticket_service import TicketService

router = APIRouter(prefix="/tickets", tags=["工单"])

# 常见错误响应，抽出来复用，避免每处都写一遍
NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "工单不存在"}}
FORBIDDEN = {status.HTTP_403_FORBIDDEN: {"model": ErrorResponse, "description": "无权限"}}


# ══════════════════════════════════════════════════════════════
# 创建
# ══════════════════════════════════════════════════════════════


@router.post(
    "",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建工单",
)
async def create_ticket(
    data: TicketCreate,
    db: DbSession,
    user: CurrentUser,
) -> TicketOut:
    """创建工单。

    工单号自动生成（OPS-年月-6位序号），SLA 截止时间按优先级自动计算。
    创建人固定取当前登录用户 —— **不接受前端传 creator_id**，
    否则任何人都能伪造成别人提的工单。
    """
    return await TicketService.create(db, data, creator=user)


# ══════════════════════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=PageResult[TicketBrief],
    summary="工单列表",
)
async def list_tickets(
    db: DbSession,
    user: CurrentUser,
    # ── 筛选条件 ───────────────────────────────────────────
    #
    # 这些参数会成为 URL 上的查询串：
    #   /api/v1/tickets?status=pending&priority=high&page=1
    #
    # Query(...) 用来声明校验规则和文档说明。
    # 用 list[...] 类型时 FastAPI 会自动支持重复传参：
    #   ?status=pending&status=processing  →  ["pending", "processing"]
    status_: Annotated[list[TicketStatus] | None, Query(alias="status")] = None,
    priority: Annotated[list[TicketPriority] | None, Query()] = None,
    category: Annotated[list[TicketCategory] | None, Query()] = None,
    assignee_id: Annotated[int | None, Query()] = None,
    department_id: Annotated[int | None, Query()] = None,
    creator_id: Annotated[int | None, Query()] = None,
    keyword: Annotated[str | None, Query(max_length=100, description="标题/描述模糊搜索")] = None,
    unhandled_hours: Annotated[
        int | None,
        Query(ge=1, le=720, description="超过 N 小时未处理（场景 4 用）"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
) -> PageResult[TicketBrief]:
    """查询工单列表。

    数据权限在 Service 层强制生效：
      admin / operator → 看全部
      user             → 只看自己创建或被分派的
    """
    query = TicketListQuery(
        status=status_,
        priority=priority,
        category=category,
        assignee_id=assignee_id,
        department_id=department_id,
        creator_id=creator_id,
        keyword=keyword,
        unhandled_hours=unhandled_hours,
        page=page,
        page_size=page_size,
    )
    return await TicketService.list_tickets(db, query, user=user)


@router.get(
    "/statistics",
    response_model=TicketStats,
    summary="工单统计",
)
async def get_statistics(
    db: DbSession,
    user: CurrentUser,
    days: Annotated[int | None, Query(ge=1, le=365, description="只统计最近 N 天")] = None,
) -> TicketStats:
    """工单统计（Dashboard 用，Agent 的统计工具也复用它）。

    ⚠️ 注意这条路由必须放在 /{ticket_id} 【前面】。

    FastAPI 是按注册顺序匹配的。如果 /{ticket_id} 在前面，
    访问 /tickets/statistics 时会被它先匹配到，
    然后试图把 "statistics" 转成 int 而报 422。
    这是路由顺序的经典坑。
    """
    return await TicketService.get_statistics(db, user=user, days=days)


@router.get(
    "/{ticket_id}",
    response_model=TicketDetailOut,
    summary="工单详情",
    responses={**NOT_FOUND},
)
async def get_ticket(
    ticket_id: int,
    db: DbSession,
    user: CurrentUser,
) -> TicketDetailOut:
    """工单详情（含评论和操作记录）。"""
    return await TicketService.get_detail(db, ticket_id, user=user)


# ══════════════════════════════════════════════════════════════
# 修改
# ══════════════════════════════════════════════════════════════


@router.put(
    "/{ticket_id}",
    response_model=TicketOut,
    summary="修改工单",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def update_ticket(
    ticket_id: int,
    data: TicketUpdate,
    db: DbSession,
    user: CurrentUser,
) -> TicketOut:
    """修改标题、描述、分类、优先级、部门。

    改优先级会自动重算 SLA 截止时间，并写一条 priority_changed 记录。
    """
    return await TicketService.update(db, ticket_id, data, operator=user)


@router.patch(
    "/{ticket_id}/status",
    response_model=TicketOut,
    summary="修改状态",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def change_status(
    ticket_id: int,
    data: TicketStatusUpdate,
    db: DbSession,
    user: CurrentUser,
) -> TicketOut:
    """修改工单状态。

    会走状态机校验，非法流转返回 409 并告诉你允许流转到哪些状态。
    """
    return await TicketService.change_status(db, ticket_id, data, operator=user)


@router.patch(
    "/{ticket_id}/assign",
    response_model=TicketOut,
    summary="分派工单",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def assign_ticket(
    ticket_id: int,
    data: TicketAssign,
    db: DbSession,
    user: CurrentUser,
) -> TicketOut:
    """分派 / 转派工单给某个人或某个部门。

    分派后工单会自动从 pending 进入 processing。
    """
    return await TicketService.assign(db, ticket_id, data, operator=user)


@router.delete(
    "/{ticket_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除工单",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def delete_ticket(
    ticket_id: int,
    db: DbSession,
    user: CurrentUser,
) -> None:
    """删除工单。仅管理员可用，且工单下不能有评论。"""
    await TicketService.delete(db, ticket_id, operator=user)


# ══════════════════════════════════════════════════════════════
# 评论
# ══════════════════════════════════════════════════════════════


@router.post(
    "/{ticket_id}/comments",
    response_model=TicketCommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="添加评论",
    responses={**NOT_FOUND, **FORBIDDEN},
)
async def add_comment(
    ticket_id: int,
    data: TicketCommentCreate,
    db: DbSession,
    user: CurrentUser,
) -> TicketCommentOut:
    """给工单添加评论。"""
    return await TicketService.add_comment(db, ticket_id, data.content, author=user)
