"""run_dedup 集成测试:跨源同事件合并、不同事件分簇、source_count 正确。

需数据库 + 本地 embedding 模型;任一不可用则跳过。测试自清理。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pulsewire.config import get_settings
from pulsewire.dedup import get_embedder, run_dedup
from pulsewire.store import upsert_item
from pulsewire.store.tables import Cluster, Item

_SOURCES = ("dedup-test-a", "dedup-test-b", "dedup-test-c")


def test_mlx_provider_on_non_apple_gives_actionable_error(monkeypatch):
    """非 Apple Silicon(mlx_embeddings 装不到)选 provider=mlx 时,报一句能照做的话、
    指向 provider=local 跨平台退路,而不是首次 embed 时抛看不懂的 ModuleNotFoundError。
    模拟"没装 mlx":让 find_spec 对 mlx_embeddings 返回 None(此测试在任何平台都跑)。"""
    import importlib.util as _ilu

    from pulsewire.dedup import embedding as _emb

    settings = get_settings()
    monkeypatch.setattr(settings.dedup.embedding, "provider", "mlx")
    real_find_spec = _ilu.find_spec
    monkeypatch.setattr(
        _emb.importlib.util, "find_spec",
        lambda name, *a, **k: None if name == "mlx_embeddings" else real_find_spec(name, *a, **k),
    )
    with pytest.raises(RuntimeError, match="provider 改为 local"):
        _emb.get_embedder(settings)


@pytest.mark.asyncio
async def test_cross_source_same_event_merges():
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")
    try:
        embedder = get_embedder(settings)
        embedder.embed(["warmup"])  # 触发模型加载;失败则跳过
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"本地 embedding 模型不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    ids = []
    try:
        # 3 条:A/B 跨源跨语言同事件,C 不同事件
        async with sm() as session:
            async with session.begin():
                ids.append(
                    await upsert_item(
                        session, source=_SOURCES[0], url="https://a.example/openai-1",
                        title="OpenAI 发布 GPT-5.5,推理能力大幅提升",
                        published_at=now - timedelta(minutes=30),
                    )
                )
                ids.append(
                    await upsert_item(
                        session, source=_SOURCES[1], url="https://b.example/openai-2",
                        title="OpenAI unveils GPT-5.5 with major reasoning gains",
                        published_at=now - timedelta(minutes=20),
                    )
                )
                ids.append(
                    await upsert_item(
                        session, source=_SOURCES[2], url="https://c.example/apple-1",
                        title="苹果发布搭载 M5 芯片的新款 MacBook Pro",
                        published_at=now - timedelta(minutes=10),
                    )
                )

        # 跑去重
        async with sm() as session:
            async with session.begin():
                summary = await run_dedup(session, settings)
        assert summary["processed"] >= 3

        # 校验:A/B 同簇、C 独立;A/B 簇 source_count==2
        async with sm() as session:
            rows = (
                await session.execute(
                    select(Item.item_id, Item.cluster_id, Item.source).where(
                        Item.source.in_(_SOURCES)
                    )
                )
            ).all()
            by_src = {r.source: r for r in rows}
            assert by_src[_SOURCES[0]].cluster_id == by_src[_SOURCES[1]].cluster_id  # A==B
            assert by_src[_SOURCES[2]].cluster_id != by_src[_SOURCES[0]].cluster_id  # C 独立

            ab_cluster = by_src[_SOURCES[0]].cluster_id
            sc = (
                await session.execute(
                    select(Cluster.source_count).where(Cluster.cluster_id == ab_cluster)
                )
            ).scalar_one()
            assert sc == 2  # 两个不同源 → 大事判定可用
    finally:
        async with sm() as session:
            async with session.begin():
                # 先删 item(级联删 embedding),再删本测试建的簇
                await session.execute(delete(Item).where(Item.source.in_(_SOURCES)))
                if ids:
                    await session.execute(
                        delete(Cluster).where(Cluster.first_item_id.in_(ids))
                    )
        await engine.dispose()
