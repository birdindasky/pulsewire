"""SQLAlchemy async 引擎 / 会话 / 声明基类。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from pulsewire.config import get_settings

# Jina embeddings v3 维度(语义去重向量)
EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        db = get_settings().database
        _engine = create_async_engine(
            db.async_dsn,
            echo=False,
            pool_pre_ping=True,
            # 建连超时:机器刚睡醒/postgres 没起时快速失败,不无限挂死整跑(2026-06-15 二⑧)
            connect_args={"timeout": db.connect_timeout},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker
