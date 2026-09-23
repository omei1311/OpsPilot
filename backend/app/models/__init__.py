"""ORM 模型包。

⚠️ 重要约定：每新增一个模型文件，**必须**在这里 import 一次。

原因：SQLAlchemy 的 Base.metadata 只有在模型类被 Python 真正导入执行后
才会注册表定义。Alembic autogenerate 是拿 Base.metadata 和数据库现状做
diff 的 —— 漏 import 的表现是「明明写了模型，autogenerate 却生成空迁移」。
"""

from app.db.base import Base, BigIntPKMixin, TimestampMixin
from app.models.agent import (
    AgentConversation,
    AgentMessage,
    AgentPendingAction,
    AgentRun,
    AgentRunStatus,
    AgentRunStep,
    AgentStepType,
    PendingActionStatus,
)
from app.models.sla import SlaPolicy
from app.models.ticket import (
    Department,
    Ticket,
    TicketCategory,
    TicketComment,
    TicketEvent,
    TicketEventType,
    TicketPriority,
    TicketStatus,
)
from app.models.user import User, UserRole

__all__ = [
    # 基类与 Mixin
    "Base",
    "BigIntPKMixin",
    "TimestampMixin",
    # 用户
    "User",
    "UserRole",
    # 工单
    "Department",
    "Ticket",
    "TicketCategory",
    "TicketPriority",
    "TicketStatus",
    "TicketComment",
    "TicketEvent",
    "TicketEventType",
    # SLA
    "SlaPolicy",
    # Agent
    "AgentConversation",
    "AgentMessage",
    "AgentRun",
    "AgentRunStatus",
    "AgentRunStep",
    "AgentStepType",
    "AgentPendingAction",
    "PendingActionStatus",
]
