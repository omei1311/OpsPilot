"""异步数据库引擎与 Session 工厂。

这是整个数据层的入口。三个概念要分清楚：

    Engine        进程级单例。持有【连接池】，不持有连接本身。
    Session       请求级。一次工作单元（unit of work），做增删改查。
    Connection    真正的一条 TCP 连接，由 Session 在需要时从池里借。

关系：Engine → (池) → Connection ← Session 借用 → 归还

⚠️ 关键约束：AsyncSession **不是并发安全的**。
   一个 Session 同一时刻只能服务一个协程。
   所以本项目的用法是「一个 HTTP 请求 = 一个 Session」，
   由 app/core/deps.py 里的依赖注入负责创建和关闭。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _build_engine_kwargs() -> dict:
    """按数据库类型组装引擎参数。

    连接池参数不是所有驱动都支持 —— SQLite 用的是 NullPool，
    传 pool_size 会直接报错。测试环境可能要跑 SQLite，所以在这里分支。
    """
    kwargs: dict = {
        "echo": settings.DB_ECHO,
        # 借出连接前先发一个轻量探测包。代价是每次多一个往返，
        # 换来的是不会拿到被 MySQL 单方面关闭的死连接。
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
    }

    if settings.is_sqlite:
        return kwargs

    kwargs.update(
        # 池里常驻的连接数
        pool_size=settings.DB_POOL_SIZE,
        # 池满之后最多再临时开多少条（峰值容量 = pool_size + max_overflow）
        max_overflow=settings.DB_MAX_OVERFLOW,
        # 连接存活超过这个秒数就回收重建。
        # 必须小于 MySQL 的 wait_timeout（默认 28800s，但我们设的 3600 更保险）
        pool_recycle=settings.DB_POOL_RECYCLE,
    )
    return kwargs


# ── 进程级单例 ──────────────────────────────────────────────
# 放在模块顶层，import 时就创建。
# 注意：create_async_engine 只是构造对象，**不会真的去连数据库**，
# 连接是在第一次执行 SQL 时才建立的（懒连接）。
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    **_build_engine_kwargs(),
)


# ── Session 工厂 ────────────────────────────────────────────
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # ⚠️ 异步场景下必须设为 False。
    # 默认 True 时，commit() 会把实例上的属性全部标记为「过期」，
    # 之后读 obj.title 会触发一次隐式的 refresh 查询 —— 在异步里
    # 这种隐式 IO 会直接抛 MissingGreenlet 错误。设 False 后
    # commit 完对象仍可直接读，这是异步 SQLAlchemy 最常见的一个坑。
    expire_on_commit=False,
    # 查询前不自动 flush。默认 True 会在每次查询前把挂起的变更刷库，
    # 容易产生意料之外的 SQL。我们让 Service 层显式控制 flush 时机。
    autoflush=False,
)


# ── 依赖注入用 ──────────────────────────────────────────────


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """提供一个请求级 Session。

    这是 FastAPI 依赖，通过 app/core/deps.py 的 DbSession 类型别名使用。

    关于事务边界的一个刻意决策：
      这里【不自动 commit】。事务边界属于 Service 层，原因是
        · 只读请求不该产生 commit
        · 多步写入需要在同一个事务里，由依赖统一 commit 会切碎事务
      所以约定是：Service 里显式 await db.commit()。
      这里只负责「出异常就回滚」和「无论如何都关闭」。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            # 出任何异常都回滚，避免把半成品事务留给下一个请求
            # （连接归还池里时若事务未结束，会污染后续使用者）
            await session.rollback()
            raise
        # 没有 commit —— 由 Service 层决定何时提交


# ── 生命周期钩子 ────────────────────────────────────────────


async def dispose_engine() -> None:
    """关闭连接池。

    应用退出时调用（main.py 的 lifespan 里）。
    不调用的话，进程会吊着一批 MySQL 连接直到超时，
    在 --reload 反复重启的开发场景下很快就把连接数用满。
    """
    await engine.dispose()
