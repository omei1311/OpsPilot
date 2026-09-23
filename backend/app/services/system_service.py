"""系统级服务：健康检查、依赖探测。

这个文件是「分层」的一个具体示范：
    Router 拿到请求 → 调本 Service → Service 用 Session 查库
    Router 里看不到任何 SQL，Service 里看不到任何 Request / HTTP 概念。

后面工单、用户、Agent 的 Service 都长这个样子。
"""

from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import DatabaseUnavailableError, ServiceUnavailableError
from app.core.logging import get_logger
from app.schemas.common import DbHealthResponse, DbPoolStatus

logger = get_logger(__name__)


class SystemService:
    """系统状态相关查询。

    方法全部是 staticmethod —— 这个 Service 不需要实例状态，
    也就不需要依赖注入一个实例对象。少一层间接，读起来更直接。
    """

    @staticmethod
    async def check_database(db: AsyncSession) -> DbHealthResponse:
        """检查数据库连通性并返回版本与耗时。

        Raises:
            DatabaseUnavailableError: 连不上或查询失败，由全局异常处理器转成 503。
        """
        started = time.perf_counter()
        try:
            # SELECT 1 是最轻量的连通性探测；顺带取版本号便于确认连的是不是预期实例
            result = await db.execute(text("SELECT VERSION()"))
            server_version = str(result.scalar_one())
        except SQLAlchemyError as exc:
            # 这里必须 catch 住再抛业务异常：
            # 把 SQLAlchemy 的异常泄露出去，全局兜底处理器只会回一个 500，
            # 前端无法区分「数据库挂了」和「代码有 bug」。
            logger.error("数据库连通性检查失败: %s", exc, exc_info=True)
            raise DatabaseUnavailableError(
                "无法连接数据库，请检查 MySQL 是否已启动、连接配置是否正确",
                detail={
                    "host": settings.MYSQL_HOST,
                    "port": settings.MYSQL_PORT,
                    "database": settings.MYSQL_DATABASE,
                    # 只回显异常类型，不回显完整信息（里面可能带连接串）
                    "error": type(exc).__name__,
                },
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        return DbHealthResponse(
            status="ok",
            database=db.bind.dialect.name if db.bind else "unknown",
            server_version=server_version,
            latency_ms=latency_ms,
        )

    @staticmethod
    async def check_redis() -> dict:
        """检查 Redis 连通性。

        Redis 挂了不影响主流程（黑名单走 fail-open 降级），
        但健康检查要能如实反映出来 —— 否则运维不知道有个组件是坏的。
        """
        import time as _time

        from app.core.redis import blacklist_size, get_redis

        started = _time.perf_counter()
        try:
            client = get_redis()
            await client.ping()
            info = await client.info("server")
            latency_ms = round((_time.perf_counter() - started) * 1000, 2)

            return {
                "status": "ok",
                "server_version": info.get("redis_version", "unknown"),
                "latency_ms": latency_ms,
                # 黑名单条数：正常情况下应该很小（每条都有 TTL，会自己消失）
                "blacklist_size": await blacklist_size(),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Redis 连通性检查失败: %s", exc)
            raise ServiceUnavailableError(
                "无法连接 Redis，请检查服务是否已启动",
                detail={"url": settings.REDIS_URL, "error": type(exc).__name__},
            ) from exc

    @staticmethod
    def get_pool_status() -> DbPoolStatus | None:
        """读取连接池当前状态。

        只在非 SQLite 下有效（SQLite 用的 NullPool 没有这些计数）。
        判断「请求是不是忘了 commit 导致连接不归还」时，
        看一眼 checked_out 是不是只增不减就明白了。
        """
        if settings.is_sqlite:
            return None

        # 延迟 import：避免模块级循环依赖
        from app.db.session import engine

        pool = engine.pool
        try:
            size = pool.size()  # type: ignore[attr-defined]
            checked_out = pool.checkedout()  # type: ignore[attr-defined]

            # ⚠️ 不要直接用 pool.overflow()。
            # SQLAlchemy 2.0 里 QueuePool.overflow() 返回的是内部计数器 _overflow，
            # 它从负数开始做偏移记账，实测空池借出 1 条会返回 -9。
            # 直接透出去会让调用方以为连接池出了故障。
            # 「溢出连接数」的正确定义是：借出数超出池容量的部分。
            overflow = max(0, checked_out - size)

            return DbPoolStatus(
                size=size,
                checked_in=pool.checkedin(),  # type: ignore[attr-defined]
                checked_out=checked_out,
                overflow=overflow,
            )
        except (AttributeError, NotImplementedError):
            # 换成 NullPool / StaticPool 时这些方法不存在，静默降级
            return None
