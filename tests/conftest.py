"""pytest 共享夹具。需要数据库的测试在连不上时自动跳过。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pulsewire.config import get_settings


@pytest.fixture(autouse=True)
def _isolate_delivery_receipts(tmp_path, monkeypatch):
    """所有测试的交付收据(二⑥ sentinel)写到临时目录,不污染真实 deploy/state/。

    run_deliver 成功推飞书会 write_receipt 到真实磁盘;测试若不隔离,会留下带测试假日期的收据,
    误导真实哨兵。autouse 把 _STATE_DIR 指到 per-test tmp;需要自定收据目录的测试可再覆盖。
    """
    from pulsewire.obs import sentinel

    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "pw_state", raising=False)


@pytest_asyncio.fixture
async def db_session():
    """事务内的会话,测试结束回滚——不留脏数据。

    每个测试用独立 engine(NullPool),避免 pytest-asyncio 每测试换事件循环时
    复用连接池导致的 'attached to a different loop'。
    """
    engine = create_async_engine(get_settings().database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过仓储测试:{exc}")
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def clean_github_candidates(db_session):
    """github_board 集成测试隔离:事务内清掉库里已有的 github 候选(有 facts→github→stars 的 item)。

    `_select_trending` 候选池按涨速取 top-N,生产/开发库里 200 个真实高星 repo 会占满候选池,
    把测试自插的中等星 repo 挤出评选、或被同名 token 折叠 → 断言随真实数据漂移而时灵时挂
    (2026-06-20 开发库灌 200 真实 repo 后暴露,6 个集成测试挂)。在事务内 DELETE,让测试只看见
    自己插的 repo;db_session 是回滚事务,DELETE 随 fixture 结束 rollback,**绝不动真实数据**。
    """
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM items WHERE facts->'github'->>'stars' IS NOT NULL")
    )
    return db_session
