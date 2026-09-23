"""SLA 策略。

SLA = Service Level Agreement，服务级别协议。
说白了就是「这个优先级的工单，多久之内必须响应/解决」。

现实企业里 SLA 会分工作日历、分客户等级，非常复杂。
我们只做最核心的一层：**按优先级定一个时限**。
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPKMixin, TimestampMixin


class SlaPolicy(Base, BigIntPKMixin, TimestampMixin):
    """每个优先级对应一条策略。"""

    __tablename__ = "sla_policies"

    priority: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, comment="优先级: low/medium/high/urgent"
    )
    response_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="响应时限(分钟)"
    )
    resolution_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="解决时限(分钟)"
    )
    is_active: Mapped[bool] = mapped_column(
        default=True, nullable=False, comment="是否启用"
    )

    def __repr__(self) -> str:
        return (
            f"<SlaPolicy {self.priority} "
            f"响应{self.response_minutes}min 解决{self.resolution_minutes}min>"
        )
