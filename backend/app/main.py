"""FastAPI 应用入口。

启动方式::

    uvicorn app.main:app --reload --port 8000

模块级暴露 `app` 对象，这是 uvicorn / gunicorn 约定俗成的查找目标。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.redis import close_redis
from app.db.session import AsyncSessionLocal, dispose_engine

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════


async def _probe_database() -> None:
    """启动时探一次数据库。

    刻意【不因失败而中断启动】。理由：
      · 启动失败 → 进程反复退出重启，日志被刷屏，真正的原因反而被淹没
      · 正常启动 → 应用能响应 /api/v1/health/db，直接告诉你哪里不对

    所以这里只告警。真正的强校验交给 health/db 接口和编排层的健康检查。
    """
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        logger.info(
            "数据库连接正常 | %s:%s/%s",
            settings.MYSQL_HOST,
            settings.MYSQL_PORT,
            settings.MYSQL_DATABASE,
        )
    except Exception as exc:  # noqa: BLE001 — 启动探测，任何异常都只告警
        logger.warning(
            "数据库连接失败（应用仍会启动，请检查 MySQL）| %s: %s",
            type(exc).__name__,
            exc,
        )
        logger.warning(
            "排查提示：docker compose -f docker-compose.dev.yml up -d 启动 MySQL，"
            "或检查 backend/.env 里的 MYSQL_* 配置"
        )


async def _probe_redis() -> None:
    """启动时探一次 Redis。

    和数据库探测一样：失败只告警不中断启动。

    ⚠️ 但 Redis 的失败后果比数据库轻得多 —— 它只存 JWT 黑名单，
    而且代码里做了 fail-open 降级（Redis 挂了当作没有黑名单）。
    所以 Redis 没起来时，系统依然可以正常登录和使用，
    只是"登出后 token 立即失效"这个功能暂时不生效。
    """
    try:
        from app.core.redis import ping

        if await ping():
            logger.info("Redis 连接正常 | %s", settings.REDIS_URL)
        else:
            raise ConnectionError("ping 返回失败")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Redis 连接失败（应用仍会启动，登出黑名单功能降级）| %s: %s",
            type(exc).__name__,
            exc,
        )
        logger.warning(
            "排查提示：docker compose -f docker-compose.dev.yml up -d 启动 Redis"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期：启动前 / 关闭后各执行一次。

    用 lifespan 而不是已废弃的 @app.on_event("startup")。
    好处是启动和关闭逻辑写在同一个函数里，共享局部变量，
    也不再有「事件处理器散落各处」的问题。

    后续阶段会在这里追加：
      · 阶段 3：启动 APScheduler 定时扫描 SLA
      · 阶段 5：初始化 LangGraph 的 Redis Checkpointer
    """
    setup_logging()

    logger.info("=" * 62)
    logger.info("%s v%s 启动中 | 环境=%s", settings.APP_NAME, settings.APP_VERSION, settings.APP_ENV)
    logger.info("API 前缀: %s", settings.API_V1_PREFIX)
    logger.info("接口文档: http://127.0.0.1:%s/docs", settings.PORT)
    logger.info("=" * 62)

    await _probe_database()

    await _probe_redis()

    yield  # ←── 应用在此运行

    logger.info("%s 正在关闭，释放连接池...", settings.APP_NAME)
    await dispose_engine()
    await close_redis()
    logger.info("%s 已关闭", settings.APP_NAME)


# ══════════════════════════════════════════════════════════════
# 应用工厂
# ══════════════════════════════════════════════════════════════


def create_app() -> FastAPI:
    """构造 FastAPI 实例。

    写成工厂函数而不是直接在模块顶层堆代码，是为了让测试能造出
    一份隔离的实例（比如覆盖依赖、改配置），互不干扰。
    """
    app = FastAPI(
        title=f"{settings.APP_NAME} API",
        description=(
            "企业智能工单与运营协同平台 —— 后端接口\n\n"
            "业务模块：用户权限 / 工单中心 / Dashboard / Agent Copilot"
        ),
        version=settings.APP_VERSION,
        lifespan=lifespan,
        # 文档路径跟着 API 前缀走，保持一致
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    )

    # ── 中间件 ─────────────────────────────────────────────
    # 注意中间件注册顺序：后注册的先执行（洋葱模型）。
    # CORS 放这里注册，实际上是最外层，保证预检请求和错误响应也带上 CORS 头。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        # ⚠️ 不能写成 ["*"] 同时开 allow_credentials=True，
        # 浏览器会直接拒绝。必须显式列出来源。
        allow_methods=["*"],
        allow_headers=["*"],
        # 让前端能读到这些响应头（分页总数、追踪 ID 等）
        expose_headers=["X-Request-ID", "X-Total-Count"],
    )

    # ── 异常处理 ───────────────────────────────────────────
    register_exception_handlers(app)

    # ── 路由 ───────────────────────────────────────────────
    # 统一前缀只在这里加一次。子路由内部各自带自己的前缀
    # （health.router 带 /health），拼起来就是 /api/v1/health。
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    # ── 根路径 ─────────────────────────────────────────────
    @app.get("/", tags=["根"], summary="服务信息")
    async def root() -> dict[str, str]:
        """给个入口指引，方便部署后直接访问根路径确认服务活着。"""
        return {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
            "docs": "/docs",
            "health": f"{settings.API_V1_PREFIX}/health",
        }

    return app


app = create_app()
