"""Alembic 运行环境（异步版）。

Alembic 官方模板默认是同步引擎，本项目用 AsyncEngine，
所以要自己包一层 asyncio.run —— 核心是 connection.run_sync()，
它让同步风格的 migration 代码能跑在异步连接上。

三个关键点：
  1. URL 从 app.core.config.settings 读，不写在 alembic.ini 里，
     保证「应用」和「迁移」用的是同一份配置
  2. import app.models 触发所有模型注册到 Base.metadata，
     否则 autogenerate 会生成空迁移
  3. 用 NullPool：迁移是一次性脚本，用完即走，没必要维持连接池
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

# ⚠️ 这行 import 有副作用，不能删：
# 它会把 app/models/__init__.py 里登记的所有模型类加载进 Python，
# 模型继承 Base 时才会把表注册到 Base.metadata。
# 少了它，autogenerate 会认为「一个表都没有」。
from app.models import Base  # noqa: F401

# Alembic 的 Config 对象，对应 alembic.ini
config = context.config

# 把配置中心里的连接串注入 Alembic。
# 注意这里是「运行时覆盖」——即使别人在 alembic.ini 里填了 url 也会被盖掉，
# 从根上杜绝「应用连 A 库、迁移改 B 库」这种事故。
# % 需要转义，因为 alembic.ini 走 configparser 插值语法
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# 按 alembic.ini 的 [loggers] 段配置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的比对基准
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 文件，不连数据库。

    用法：alembic upgrade head --sql > migrate.sql
    适合交给 DBA 审核后再上生产的场景。
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """真正的迁移逻辑，跑在同步 context 里（由 run_sync 桥接）。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 检测列类型变更（如 String(50) → String(200)）
        compare_type=True,
        # 检测默认值变更
        compare_server_default=True,
        # 每个迁移在独立事务里执行；MySQL 的 DDL 不支持事务回滚，
        # 这个选项确保失败时不会留下「迁移记录已写但表没建」的错位状态
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """用异步引擎连接数据库并执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # 迁移脚本是一次性任务，用 NullPool 不保留连接
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # run_sync 是异步 SQLAlchemy 提供的桥：
        # 把同步的 do_run_migrations 丢进 greenlet 里跑，
        # 内部遇到 IO 时自动让出事件循环
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：直连数据库执行迁移（日常用这个）。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
