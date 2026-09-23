"""工单相关的出入参模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.ticket import (
    TicketCategory,
    TicketEventType,
    TicketPriority,
    TicketStatus,
)
from app.schemas.common import UTCDatetime

# ══════════════════════════════════════════════════════════════
# 入参
# ══════════════════════════════════════════════════════════════


class TicketCreate(BaseModel):
    """创建工单。"""

    title: str = Field(min_length=2, max_length=200, description="标题")
    description: str | None = Field(
        default=None, max_length=5000, description="详细描述"
    )
    category: TicketCategory = Field(
        default=TicketCategory.OTHER, description="分类"
    )
    priority: TicketPriority = Field(
        default=TicketPriority.MEDIUM, description="优先级"
    )
    # 创建时可以不指定负责人，先落到"待处理"池子里
    assignee_id: int | None = Field(default=None, description="负责人 ID")
    department_id: int | None = Field(default=None, description="负责部门 ID")


class TicketUpdate(BaseModel):
    """修改工单。

    所有字段都是可选的 —— 前端只传要改的字段。
    没传的字段（None）不参与更新，这是 PATCH 的语义。

    ⚠️ 注意这里【故意不包含 status 和 assignee_id】。
    它们有各自专门的接口（.../status 和 .../assign），因为改动它们
    需要额外的业务校验（状态机、权限），不能和普通字段编辑混在一起。
    """

    title: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    category: TicketCategory | None = None
    priority: TicketPriority | None = None
    department_id: int | None = None


class TicketStatusUpdate(BaseModel):
    """修改状态。"""

    status: TicketStatus = Field(description="目标状态")
    reason: str | None = Field(default=None, max_length=500, description="变更原因")


class TicketAssign(BaseModel):
    """分派。

    assignee_id 和 department_id 至少传一个：
      只传部门  → 落到部门池子，等主管再指派具体人
      只传个人  → 直接指派给这个人
      两个都传  → 指派给人，同时记录所属部门
    """

    assignee_id: int | None = Field(default=None, description="负责人 ID")
    department_id: int | None = Field(default=None, description="负责部门 ID")

    # ⚠️ 这里必须用 model_validator，不能用 field_validator。
    #
    # Pydantic v2 的 field_validator 只在【该字段出现在输入里】时才执行。
    # 如果写成 @field_validator("department_id")，那么调用方传 {}
    # （两个字段都没传）时，这个校验函数根本不会被触发，
    # 于是空请求会被放行 —— 这就是我们踩到的坑。
    #
    # model_validator(mode="after") 是"整个对象构造完之后"再校验，
    # 不管字段传没传都会执行。
    @model_validator(mode="after")
    def _at_least_one_target(self) -> TicketAssign:
        if self.assignee_id is None and self.department_id is None:
            raise ValueError("assignee_id 和 department_id 至少要传一个")
        return self


class TicketCommentCreate(BaseModel):
    """发表评论。"""

    content: str = Field(min_length=1, max_length=5000, description="评论内容")


class TicketListQuery(BaseModel):
    """工单列表的筛选条件。

    这个模型不直接用于接口签名（那里会用 Query 参数），
    而是 Service 层的入参 —— 把"筛选条件"打包成一个对象传递，
    比在函数签名里列 8 个参数清楚得多。
    """

    status: list[TicketStatus] | None = None
    priority: list[TicketPriority] | None = None
    category: list[TicketCategory] | None = None
    assignee_id: int | None = None
    department_id: int | None = None
    creator_id: int | None = None
    keyword: str | None = None
    # "超过 N 小时未处理" —— 场景 4 的核心筛选条件
    unhandled_hours: int | None = None
    page: int = 1
    page_size: int = 20


# ══════════════════════════════════════════════════════════════
# 出参
# ══════════════════════════════════════════════════════════════


class TicketOut(BaseModel):
    """工单完整信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_no: str
    title: str
    description: str | None

    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus

    creator_id: int
    assignee_id: int | None
    department_id: int | None

    # 关联对象的名称，由 Service 层查出来后填进来。
    # 前端列表页直接显示名字，不用再为每一行发一次请求查用户
    # （那就是经典的 N+1 问题）。
    creator_name: str | None = None
    assignee_name: str | None = None
    department_name: str | None = None

    sla_deadline: UTCDatetime | None = None
    # SLA 状态由服务端计算好，前端只负责展示颜色。
    # 让前端算的话，客户端时钟不准就会算错。
    sla_status: Literal["normal", "at_risk", "overdue", "done"] | None = None
    sla_remaining_minutes: int | None = Field(
        default=None, description="距截止还剩多少分钟，负数表示已超时"
    )

    created_at: UTCDatetime
    updated_at: UTCDatetime
    resolved_at: UTCDatetime | None = None


class TicketBrief(BaseModel):
    """工单摘要 —— 列表和 Agent 工具返回用。

    只带关键字段，不带 description 这种长文本。
    Agent 的工具返回值尤其需要精简：工具结果要进 LLM 上下文，
    字段越多 token 消耗越大。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_no: str
    title: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    assignee_name: str | None = None
    sla_deadline: UTCDatetime | None = None
    sla_status: Literal["normal", "at_risk", "overdue", "done"] | None = None
    created_at: UTCDatetime


class TicketCommentOut(BaseModel):
    """评论。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    user_id: int
    author_name: str | None = None
    content: str
    created_at: UTCDatetime


class TicketEventOut(BaseModel):
    """操作记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    user_id: int | None
    actor_name: str | None = None
    event_type: TicketEventType
    event_data: dict[str, Any] | None
    created_at: UTCDatetime


class TicketDetailOut(TicketOut):
    """工单详情 = 工单信息 + 评论 + 操作记录。

    为什么详情要一次带回来，而不是分三个接口？
      详情页三个都要展示，分三次请求会有明显的加载卡顿，
      而且后端本来就能在 3 次查询内全部拿到（不是 N+1）。
    """

    comments: list[TicketCommentOut] = Field(default_factory=list)
    events: list[TicketEventOut] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════
# 统计
# ══════════════════════════════════════════════════════════════


class TicketStats(BaseModel):
    """工单统计 —— Dashboard 和 Agent 的 get_ticket_statistics 工具共用。"""

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    overdue: int = Field(default=0, description="已超时数量")
    unassigned: int = Field(default=0, description="未分派数量")
