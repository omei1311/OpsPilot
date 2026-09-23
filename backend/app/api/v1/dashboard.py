"""Dashboard 接口。

数据权限和工单列表完全一致 —— 因为都走 TicketService._apply_scope()，
普通用户看 Dashboard 时统计的也只是他自己的工单。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, DbSession
from app.schemas.dashboard import (
    DashboardOverview,
    DistributionResponse,
    SlaRiskResponse,
    TrendResponse,
    WorkloadItem,
)
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["数据看板"])


@router.get("/overview", response_model=DashboardOverview, summary="概览卡片")
async def get_overview(db: DbSession, user: CurrentUser) -> DashboardOverview:
    """顶部统计卡片：总数 / 今日新增 / 待处理 / 处理中 / 已解决 / 超时 等。"""
    return await DashboardService.get_overview(db, user)


@router.get("/trend", response_model=TrendResponse, summary="工单趋势")
async def get_trend(
    db: DbSession,
    user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=90, description="统计最近 N 天")] = 7,
) -> TrendResponse:
    """折线图：每天的新增 vs 解决。"""
    return await DashboardService.get_trend(db, user, days=days)


@router.get(
    "/distribution",
    response_model=DistributionResponse,
    summary="分布统计",
)
async def get_distribution(
    db: DbSession, user: CurrentUser
) -> DistributionResponse:
    """饼图：按状态 / 优先级 / 分类的分布。"""
    return await DashboardService.get_distribution(db, user)


@router.get("/sla-risk", response_model=SlaRiskResponse, summary="SLA 风险清单")
async def get_sla_risk(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50, description="返回多少条")] = 10,
) -> SlaRiskResponse:
    """最紧急的 N 条工单（按 SLA 截止时间升序）。

    这个接口同时会被 Agent 的 analyze_sla_risk 工具复用 ——
    又一处"人工入口和 AI 入口共享同一套逻辑"。
    """
    return await DashboardService.get_sla_risk(db, user, limit=limit)


@router.get("/workload", response_model=list[WorkloadItem], summary="处理人工作量")
async def get_workload(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[WorkloadItem]:
    """柱状图：各处理人在办的工单数。"""
    return await DashboardService.get_workload(db, user, limit=limit)
