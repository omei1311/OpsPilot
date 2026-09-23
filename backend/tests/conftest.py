"""pytest 公共夹具。

本阶段的测试分两类：
  · 不依赖数据库的（健康检查、错误格式）—— 任何环境都能跑
  · 依赖数据库的 —— 连不上时自动 skip，不阻塞 CI

用 TestClient 而不是 AsyncClient：FastAPI 的 TestClient 内部起了一个
portal 线程跑事件循环，能直接驱动 async 端点，写起来是同步风格，
对本阶段的接口测试足够用。真正需要测并发行为时再换 httpx.AsyncClient。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    """带 lifespan 的测试客户端。

    用 `with` 进入是为了触发 lifespan —— 否则 startup / shutdown 逻辑
    （日志初始化、启动探测、连接池释放）都不会执行。
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def db_available() -> bool:
    """探测数据库是否可用，供需要真库的测试做 skip 判断。

    刻意用一次性的独立引擎，而不是复用 app.db.session 里的全局 engine：
    全局 engine 的连接池会绑定到首次使用它的事件循环，被 pytest 的
    另一个循环借用会报 "attached to a different loop"。
    """

    async def _probe() -> bool:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 — 探测失败即视为不可用
            return False
        finally:
            await engine.dispose()

    return asyncio.run(_probe())


@pytest.fixture(scope="session")
def require_db(db_available: bool) -> None:
    """需要数据库的测试挂上它，连不上就跳过。"""
    if not db_available:
        pytest.skip(
            f"数据库不可用（{settings.MYSQL_HOST}:{settings.MYSQL_PORT}），跳过集成测试"
        )
