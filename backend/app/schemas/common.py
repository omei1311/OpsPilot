"""通用响应模型。

这里放「跨模块复用」的 Schema。具体业务的 Schema
（TicketCreate / UserOut ...）放在各自的模块文件里。

约定：
  · 出参模型以 Out / Response 结尾
  · 入参模型以 Create / Update / Query 结尾
  · 全部继承 BaseModel，并开启 from_attributes 以便直接从 ORM 对象转换
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")


def _to_utc_iso(value: datetime) -> str:
    """把 datetime 转成带 Z 后缀的 UTC ISO 8601 字符串。

    为什么需要这个：
    数据库里存的是 UTC 的【朴素时间】(naive datetime，不带时区信息)。
    Pydantic 默认会序列化成 "2026-09-21T04:37:32" —— 没有时区标记。
    前端 `new Date("2026-09-21T04:37:32")` 会按【浏览器本地时区】解析，
    于是北京时间(UTC+8)下会凭空多出 8 小时。

    加上 Z 后缀后，前端就知道这是 UTC，浏览器会自动转成本地时间显示。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 所有对外返回的时间字段都用这个类型标注。
# 用 Annotated + PlainSerializer，写一次到处生效，不用每个字段配一遍。
UTCDatetime = Annotated[datetime, PlainSerializer(_to_utc_iso, return_type=str)]


class ORMModel(BaseModel):
    """可以直接从 ORM 实例构造的模型基类。

    model_config 里的 from_attributes=True 等价于 Pydantic v1 的 orm_mode，
    有了它才能写 `TicketOut.model_validate(ticket_orm_obj)`。
    """

    model_config = ConfigDict(from_attributes=True)


# ══════════════════════════════════════════════════════════════
# 分页
# ══════════════════════════════════════════════════════════════


class PageResult(BaseModel, Generic[T]):
    """统一分页响应。

    所有列表接口都返回这个结构，前端只需要写一套分页组件。

    泛型用法::

        class TicketListOut(PageResult[TicketOut]): ...
    """

    items: list[T] = Field(default_factory=list, description="当前页数据")
    total: int = Field(description="符合条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")

    @property
    def pages(self) -> int:
        """总页数。"""
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


# ══════════════════════════════════════════════════════════════
# 错误响应
# ══════════════════════════════════════════════════════════════


class ErrorResponse(BaseModel):
    """错误响应体。

    这个类【只用于 OpenAPI 文档展示】—— 实际错误响应由
    app/core/exceptions.py 里的处理器直接构造 dict 返回，
    不走 Pydantic 序列化，省一点开销。
    """

    code: str = Field(description="机器可读的错误码", examples=["TICKET_NOT_FOUND"])
    message: str = Field(description="人可读的错误描述", examples=["工单不存在"])
    detail: dict[str, Any] | None = Field(
        default=None, description="结构化上下文，便于前端定位问题"
    )


# ══════════════════════════════════════════════════════════════
# 健康检查
# ══════════════════════════════════════════════════════════════


class HealthResponse(BaseModel):
    """存活检查响应（不涉及任何外部依赖）。"""

    status: Literal["ok"] = "ok"
    app: str = Field(description="应用名")
    version: str = Field(description="应用版本")
    env: str = Field(description="运行环境 dev/test/prod")
    time: datetime = Field(description="服务器当前时间(UTC)")


class DbPoolStatus(BaseModel):
    """连接池实时状态。排查「连接泄漏」时非常有用。"""

    size: int = Field(description="池容量")
    checked_in: int = Field(description="空闲连接数")
    checked_out: int = Field(description="借出中的连接数")
    overflow: int = Field(description="超出池容量临时创建的连接数")


class DbHealthResponse(BaseModel):
    """数据库连接检查响应。"""

    status: Literal["ok"] = "ok"
    database: str = Field(description="数据库类型", examples=["mysql"])
    server_version: str = Field(description="数据库服务端版本")
    latency_ms: float = Field(description="执行一次 SELECT 1 的往返耗时(毫秒)")
    pool: DbPoolStatus | None = Field(default=None, description="连接池状态")


__all__ = [
    "ORMModel",
    "UTCDatetime",
    "PageResult",
    "ErrorResponse",
    "HealthResponse",
    "DbHealthResponse",
    "DbPoolStatus",
]
