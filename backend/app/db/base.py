"""ORM 基类与公共 Mixin。

所有业务模型都继承 Base。Mixin 负责把每张表都要有的字段抽出来，
避免在 9 个模型文件里重复写 id / created_at / updated_at。

用法::

    from app.db.base import Base, BigIntPKMixin, TimestampMixin

    class Ticket(Base, BigIntPKMixin, TimestampMixin):
        __tablename__ = "tickets"
        title: Mapped[str] = mapped_column(String(200), nullable=False)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 2.x 声明式基类。

    继承 DeclarativeBase 而不是老式的 declarative_base()：
    前者是 2.0 的推荐写法，类型检查器（mypy / pyright）能正确推导
    Mapped[...] 注解，IDE 补全和下划线提示都更准。

    Base.metadata 是所有表定义的汇总，Alembic autogenerate 依赖它。
    """

    def __repr__(self) -> str:
        """默认的 __repr__ 只打印类名和主键，日志里够用且不会泄漏敏感字段。"""
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class BigIntPKMixin:
    """自增 BIGINT 主键。

    用 BIGINT 而非 INT：工单、日志这类表增长快，INT 上限约 21 亿，
    对日志表来说不是遥不可及的数字，一开始就用 BIGINT 省得以后改。
    """

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="主键",
    )


class TimestampMixin:
    """创建 / 更新时间。

    - server_default=func.now()：由【数据库】填值，不依赖应用服务器时钟
    - onupdate=func.now()：SQLAlchemy 在 UPDATE 时自动带上，业务代码不用管
    - 两个字段都跟随时区无关的 DATETIME（库连接固定 +00:00，存的都是 UTC）
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="创建时间(UTC)",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间(UTC)",
    )
