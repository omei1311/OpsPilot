"""健康检查接口。

分两个端点，语义不同（这是 K8s 里 liveness / readiness 的经典区分）：

    GET /api/v1/health      存活检查 —— 进程还在跑吗？
                            不碰任何外部依赖，永远快速返回 200。
                            挂了就说明进程死了，应该重启。

    GET /api/v1/health/db   依赖检查 —— 依赖就绪了吗？
                            真的去查一次数据库，连不上返回 503。
                            失败说明「后端活着但数据库有问题」，
                            重启后端没用，得去查数据库。

把两者分开的原因：如果合并成一个，数据库一抖动就会让编排系统
误判进程已死并疯狂重启，反而放大故障。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, status

from app.core.config import settings
from app.core.deps import DbSession
from app.schemas.common import DbHealthResponse, ErrorResponse, HealthResponse
from app.services.system_service import SystemService

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get(
    "",
    response_model=HealthResponse,
    summary="存活检查",
    description="检查应用进程是否存活。不访问数据库等外部依赖，用于负载均衡和容器存活探针。",
)
async def health_check() -> HealthResponse:
    """进程存活即返回 200。

    注意：这里【故意不注入 DbSession】。
    一旦注入，FastAPI 会在进入函数前先去连接池借连接 ——
    数据库一挂，连这个「进程还活着」的信号都发不出来了。
    """
    return HealthResponse(
        status="ok",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.APP_ENV,
        time=datetime.now(timezone.utc),
    )


@router.get(
    "/db",
    response_model=DbHealthResponse,
    summary="数据库连接检查",
    description="实际执行一次 SELECT 查询，验证数据库连通性，并返回连接池状态。",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "数据库不可用",
        }
    },
)
async def health_check_db(db: DbSession) -> DbHealthResponse:
    """探测数据库，附带连接池水位。

    业务逻辑全在 SystemService 里，这里只负责「把结果返回出去」。
    连不上时会抛 DatabaseUnavailableError，由全局处理器统一转成 503。
    """
    result = await SystemService.check_database(db)
    # 连接池状态用于排查连接泄漏：checked_out 长期只增不减就是没归还
    result.pool = SystemService.get_pool_status()
    return result


@router.get(
    "/redis",
    summary="Redis 连接检查",
    description="验证 Redis（JWT 黑名单存储）连通性。",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Redis 不可用",
        }
    },
)
async def health_check_redis() -> dict:
    """探测 Redis。

    注意 Redis 挂掉**不会**影响登录 —— 黑名单查询走 fail-open 降级。
    但健康检查要如实反映，不能藏着。
    """
    return await SystemService.check_redis()
