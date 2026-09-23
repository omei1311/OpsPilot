"""统一异常体系与全局异常处理。

设计目标：**任何**从接口返回的错误，响应体格式完全一致。

    {
      "code":    "TICKET_NOT_FOUND",     # 机器可读，前端据此做分支
      "message": "工单不存在",            # 人可读，可直接展示给用户
      "detail":  {"ticket_no": "OPS-..."} # 结构化上下文，可为 null
    }

分层约定：
    Service 层只管 raise 业务异常，不关心 HTTP；
    异常到 HTTP 状态码的映射只在本文件里发生。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 异常定义
# ══════════════════════════════════════════════════════════════


class BizError(Exception):
    """所有业务异常的基类。

    子类通过覆盖类属性来声明自己的 code / http_status，
    这样 raise 的时候只传 message 和 detail 就够了。
    """

    code: str = "BIZ_ERROR"
    http_status: int = status.HTTP_400_BAD_REQUEST
    default_message: str = "业务处理失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail
        if code:
            self.code = code
        super().__init__(self.message)


class BadRequestError(BizError):
    """参数或请求体不合法（业务层面，非语法层面）。"""

    code = "BAD_REQUEST"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "请求参数不合法"


class UnauthorizedError(BizError):
    """未登录 / token 失效。"""

    code = "UNAUTHORIZED"
    http_status = status.HTTP_401_UNAUTHORIZED
    default_message = "未认证或登录已过期"


class ForbiddenError(BizError):
    """已登录但权限不足。"""

    code = "FORBIDDEN"
    http_status = status.HTTP_403_FORBIDDEN
    default_message = "没有权限执行该操作"


class NotFoundError(BizError):
    """资源不存在。"""

    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    default_message = "请求的资源不存在"


class ConflictError(BizError):
    """与当前资源状态冲突（如非法状态流转、重复提交）。"""

    code = "CONFLICT"
    http_status = status.HTTP_409_CONFLICT
    default_message = "当前状态不允许该操作"


class ServiceUnavailableError(BizError):
    """依赖的外部组件不可用。"""

    code = "SERVICE_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    default_message = "服务暂时不可用"


class DatabaseUnavailableError(ServiceUnavailableError):
    """数据库连不上 / 查询失败。

    健康检查接口专用。单独成一个类是为了让前端能区分
    「后端挂了」和「后端活着但数据库挂了」。
    """

    code = "DATABASE_UNAVAILABLE"
    default_message = "数据库连接失败"


# ══════════════════════════════════════════════════════════════
# 响应构造
# ══════════════════════════════════════════════════════════════


def build_error_body(
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一的错误响应体。"""
    return {"code": code, "message": message, "detail": detail}


# ══════════════════════════════════════════════════════════════
# 处理器注册
# ══════════════════════════════════════════════════════════════


def register_exception_handlers(app: FastAPI) -> None:
    """把所有异常处理器挂到 FastAPI 实例上。

    FastAPI 会按「最具体的异常类优先」匹配处理器，
    所以 BizError 的处理器不会被 Exception 的处理器抢走。
    """

    @app.exception_handler(BizError)
    async def _handle_biz_error(request: Request, exc: BizError) -> JSONResponse:
        """业务异常：预期内的错误，用 WARNING 级别记录，不打堆栈。"""
        logger.warning(
            "业务异常 | %s %s | code=%s | message=%s | detail=%s",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=build_error_body(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Pydantic 入参校验失败（422）。

        FastAPI 原始的 errors() 里可能含 bytes / 异常对象，不能直接 JSON 序列化，
        这里裁剪成前端好用的精简结构。
        """
        errors = [
            {
                "field": ".".join(str(loc) for loc in err.get("loc", [])),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        logger.warning(
            "参数校验失败 | %s %s | %s", request.method, request.url.path, errors
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=build_error_body(
                "VALIDATION_ERROR", "请求参数校验失败", {"errors": errors}
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """接管 FastAPI / Starlette 自己抛的 HTTPException。

        主要是 404（路由不存在）和 405（方法不允许）。
        不接管的话，这些会返回 {"detail": "Not Found"}，格式和我们的不一致。
        """
        code_map = {
            status.HTTP_404_NOT_FOUND: "ROUTE_NOT_FOUND",
            status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        }
        code = code_map.get(exc.status_code, f"HTTP_{exc.status_code}")
        message = str(exc.detail) if exc.detail else "请求失败"

        logger.warning(
            "HTTP 异常 | %s %s | status=%s", request.method, request.url.path, exc.status_code
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(code, message),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """兜底：所有未预期的异常。

        必须打完整堆栈 —— 这是我们自己代码的 bug，需要能定位。
        但对外的 message 必须是模糊的，不能把 SQL、文件路径、堆栈泄露出去。
        """
        logger.exception(
            "未预期异常 | %s %s | %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
            exc,
        )
        detail = None
        if settings.DEBUG:
            # 本地开发时把真实异常类型带出来，省得每次都翻日志
            detail = {"exception": type(exc).__name__, "message": str(exc)}

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=build_error_body("INTERNAL_ERROR", "服务器内部错误", detail),
        )
