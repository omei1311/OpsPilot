"""用户模型。

对应数据库里的 users 表。

回顾上一讲的映射关系：
    class User  ←→  users 表
    user 对象    ←→  表里的一行
    user.username ←→  username 这一列
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPKMixin, TimestampMixin


class UserRole(StrEnum):
    """用户角色。

    用 StrEnum 而不是普通 Enum：成员值就是字符串，
    存进数据库、转成 JSON 都不需要额外转换。

    继承 StrEnum 的好处是 `UserRole.ADMIN == "admin"` 直接成立。
    """

    ADMIN = "admin"        # 管理员：能管用户、能看全部数据
    OPERATOR = "operator"  # 操作员：能创建/分派工单
    USER = "user"          # 普通用户：只能看自己的工单


class User(Base, BigIntPKMixin, TimestampMixin):
    """用户表。"""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
        comment="登录名",
    )
    email: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        index=True,
        nullable=False,
        comment="邮箱",
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="bcrypt 哈希后的密码，绝不存明文",
    )
    # 用 String 而不是数据库的 ENUM 类型：
    # MySQL 的 ENUM 改取值要 ALTER TABLE，加一个角色就得写迁移。
    # 用 String + 应用层校验，灵活得多，代价是数据库不帮你挡住非法值。
    role: Mapped[str] = mapped_column(
        String(20),
        default=UserRole.USER,
        server_default=UserRole.USER,
        nullable=False,
        comment="角色: admin / operator / user",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
        comment="是否启用，false 表示禁止登录",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role}>"
