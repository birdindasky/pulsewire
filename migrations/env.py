"""Alembic 异步迁移环境。URL 从 pulsewire 配置注入,密钥不进仓库。"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from pulsewire.config import get_settings
from pulsewire.store.base import Base

# 导入所有表,确保 metadata 完整(autogenerate 用)
from pulsewire.store import tables  # noqa: F401

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database.async_dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
