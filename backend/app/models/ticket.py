"""工单核心模型。

四张表：
    departments       部门（场景 4「分配给技术部门」需要它）
    tickets           工单主表
    ticket_comments   评论
    ticket_events     操作记录（只增不改，审计用）

对应关系回顾：
    class Ticket ←→ tickets 表
    ticket 对象  ←→ 表里一行
    ticket.title ←→ title 列
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPKMixin, TimestampMixin


# ══════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════


class TicketCategory(StrEnum):
    """工单分类。"""

    ACCOUNT = "account"      # 账号问题
    PAYMENT = "payment"      # 支付问题
    TECHNICAL = "technical"  # 技术故障
    LOGISTICS = "logistics"  # 物流
    OPERATION = "operation"  # 运营
    OTHER = "other"          # 其他


class TicketPriority(StrEnum):
    """优先级。值越大越紧急。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(StrEnum):
    """工单状态。"""

    PENDING = "pending"        # 待处理（刚创建）
    PROCESSING = "processing"  # 处理中（已分派）
    WAITING = "waiting"        # 等待中（挂起，等用户反馈）
    RESOLVED = "resolved"      # 已解决（待确认）
    CLOSED = "closed"          # 已关闭（终态）


class TicketEventType(StrEnum):
    """操作记录类型。"""

    CREATED = "created"
    UPDATED = "updated"
    ASSIGNED = "assigned"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    COMMENTED = "commented"


# ══════════════════════════════════════════════════════════════
# 部门
# ══════════════════════════════════════════════════════════════


class Department(Base, BigIntPKMixin, TimestampMixin):
    """部门。

    保持扁平（不做树形）—— 企业里"技术部/客服部/运维部"这种一层结构
    就够用了，做递归查询是过度设计。
    """

    __tablename__ = "departments"

    name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, comment="部门名称"
    )
    code: Mapped[str] = mapped_column(
        String(30), unique=True, nullable=False, comment="部门编码，如 tech"
    )

    def __repr__(self) -> str:
        return f"<Department id={self.id} name={self.name!r}>"


# ══════════════════════════════════════════════════════════════
# 工单主表
# ══════════════════════════════════════════════════════════════


def generate_temp_ticket_no() -> str:
    """生成一个临时的工单号占位符。

    ⚠️ 为什么需要这个，而不是用空字符串 "" 占位？

    工单号是由自增 id 算出来的（OPS-202609-000123），
    但 id 要 INSERT 之后才知道。所以只能"先插进去，拿到 id 再回填"。

    如果占位符用 ""：
      · 批量插入时，第二行就撞唯一索引 → Duplicate entry ''
      · 并发创建时，两个事务同时插 "" → 后提交的那个直接失败

    用随机字符串占位就不会有任何碰撞，回填后临时值全部消失。
    """
    import uuid

    return f"TMP-{uuid.uuid4().hex[:24]}"


class Ticket(Base, BigIntPKMixin, TimestampMixin):
    """工单。"""

    __tablename__ = "tickets"

    ticket_no: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False,
        comment="工单编号，如 OPS-202609-000123",
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="标题")
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="详细描述"
    )

    # 用 String 存枚举值而不是数据库 ENUM 类型：
    # MySQL 的 ENUM 加一个取值要 ALTER TABLE，改起来麻烦。
    # 用 String + 应用层校验，灵活得多。
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TicketCategory.OTHER,
        comment="分类: account/payment/technical/logistics/operation/other",
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TicketPriority.MEDIUM, index=True,
        comment="优先级: low/medium/high/urgent",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TicketStatus.PENDING, index=True,
        comment="状态: pending/processing/waiting/resolved/closed",
    )

    # ── 人员与部门 ─────────────────────────────────────────
    creator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False, index=True,
        comment="创建人",
    )
    assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True, index=True,
        comment="负责人，null 表示还没分派",
    )
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id"), nullable=True, index=True,
        comment="负责部门",
    )

    # ── SLA ───────────────────────────────────────────────
    sla_deadline: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True,
        comment="SLA 截止时间(UTC)，由创建时的优先级决定",
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="解决时间(UTC)"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="关闭时间(UTC)"
    )

    # ── 关系 ──────────────────────────────────────────────
    #
    # ★ lazy="raise" 是刻意设的，这是异步 SQLAlchemy 的一个关键防御。
    #
    # 默认的懒加载会在【访问属性时】偷偷发一条 SQL：
    #     ticket.creator        ← 这里会查一次 users 表
    # 在异步里，这个"偷偷发的 SQL"发生在没有 await 的地方，
    # greenlet 无法挂起 → 直接抛 MissingGreenlet 异常。
    #
    # 设成 "raise" 后，只要你忘了预加载就立刻报错并告诉你：
    # "你不许懒加载我，请用 selectinload 显式加载"。
    # 把运行时玄学问题变成了启动就暴露的明确错误。
    creator: Mapped["User"] = relationship(  # noqa: F821
        "User", foreign_keys=[creator_id], lazy="raise"
    )
    assignee: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[assignee_id], lazy="raise"
    )
    department: Mapped["Department | None"] = relationship(
        "Department", lazy="raise"
    )

    # ── 索引 ──────────────────────────────────────────────
    # 工单列表页最高频的查询是「按状态 + 优先级筛选，按创建时间排序」，
    # 所以建这个复合索引。单列索引用处不大 —— 状态只有 5 个取值，
    # 区分度太低，MySQL 优化器往往干脆不用。
    __table_args__ = (
        Index("ix_tickets_status_priority", "status", "priority"),
        Index("ix_tickets_assignee_status", "assignee_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_no} {self.status} {self.priority}>"


# ══════════════════════════════════════════════════════════════
# 评论
# ══════════════════════════════════════════════════════════════


class TicketComment(Base, BigIntPKMixin, TimestampMixin):
    """工单评论。"""

    __tablename__ = "ticket_comments"

    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False,
        comment="评论人",
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="评论内容")

    # 评论列表永远是「某张工单下的评论，按时间正序」
    __table_args__ = (Index("ix_comments_ticket_created", "ticket_id", "created_at"),)

    author: Mapped["User"] = relationship("User", lazy="raise")  # noqa: F821

    def __repr__(self) -> str:
        return f"<TicketComment ticket={self.ticket_id} user={self.user_id}>"


# ══════════════════════════════════════════════════════════════
# 操作记录
# ══════════════════════════════════════════════════════════════


class TicketEvent(Base, BigIntPKMixin, TimestampMixin):
    """工单操作记录（审计日志）。

    为什么单独一张表而不是塞进 tickets 的 JSON 字段？

      1. 它是【只增不改】的流水，和工单本身的生命周期不同
      2. 列表页要分页展示，塞 JSON 里没法分页
      3. 以后要统计"平均几个操作解决一个工单"这类指标，
         独立表可以直接聚合查询，JSON 得全表扫

    重要约束：这张表没有 update / delete 接口。
    操作记录一旦写入就不能被普通编辑接口篡改 —— 否则审计就失去意义了。
    """

    __tablename__ = "ticket_events"

    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id"), nullable=False, index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True,
        comment="操作人，null 表示系统自动",
    )
    event_type: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="created/updated/assigned/status_changed/priority_changed/commented",
    )
    # event_data 存这次操作的具体内容，比如：
    #   状态变更 → {"from": "pending", "to": "processing"}
    #   分派     → {"assignee_id": 5, "assignee_name": "李四"}
    # 用 JSON 存：不同事件类型的字段差别很大，建字段会变成一堆 NULL 列。
    # 这里不需要对它做条件查询，所以 JSON 是合适的（不需要索引）。
    event_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="事件详情(JSON)"
    )

    __table_args__ = (Index("ix_events_ticket_created", "ticket_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<TicketEvent ticket={self.ticket_id} type={self.event_type}>"
