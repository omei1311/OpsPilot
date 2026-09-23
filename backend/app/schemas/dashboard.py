"""Dashboard 统计数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.ticket import TicketBrief


class DashboardOverview(BaseModel):
    """顶部概览卡片。"""

    total: int = Field(0, description="工单总数")
    today_created: int = Field(0, description="今日新增")
    today_resolved: int = Field(0, description="今日解决")
    pending: int = Field(0, description="待处理")
    processing: int = Field(0, description="处理中")
    resolved: int = Field(0, description="已解决")
    closed: int = Field(0, description="已关闭")
    overdue: int = Field(0, description="SLA 已超时")
    at_risk: int = Field(0, description="SLA 即将超时")
    unassigned: int = Field(0, description="未分派")


class TrendPoint(BaseModel):
    """趋势图上的一个点（一天）。"""

    date: str = Field(description="日期 YYYY-MM-DD")
    created: int = Field(0, description="当天新增")
    resolved: int = Field(0, description="当天解决")


class TrendResponse(BaseModel):
    """趋势折线图数据。"""

    days: int
    points: list[TrendPoint] = Field(default_factory=list)


class NameCount(BaseModel):
    """通用的「名称 + 数量」项，饼图/柱状图用。"""

    name: str
    value: int


class DistributionResponse(BaseModel):
    """分布统计（饼图用）。

    给的是数组而不是 dict，因为前端画图需要有序的列表，
    dict 的顺序在 JSON 里不保证。
    """

    by_status: list[NameCount] = Field(default_factory=list)
    by_priority: list[NameCount] = Field(default_factory=list)
    by_category: list[NameCount] = Field(default_factory=list)


class SlaRiskResponse(BaseModel):
    """SLA 风险清单。"""

    total_overdue: int = Field(0, description="超时总数")
    total_at_risk: int = Field(0, description="即将超时总数")
    items: list[TicketBrief] = Field(
        default_factory=list, description="最紧急的 N 条，按剩余时间升序"
    )


class WorkloadItem(BaseModel):
    """处理人工作量。"""

    user_id: int
    username: str
    processing: int = Field(0, description="处理中")
    resolved: int = Field(0, description="已解决")
    total: int = Field(0, description="总计")
