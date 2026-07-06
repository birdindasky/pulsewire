"""fetch_and_store 集成测试:跑 file:// fixture 落库(需数据库,无库自动跳过)。

只用 file 源,不依赖网络;验证并发抓取→落库链路 + published_at 兜底。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pulsewire.config import get_settings
from pulsewire.config.models import Source, SourceType
from pulsewire.fetch import fetch_and_store
from pulsewire.store.tables import Item

FILE_SOURCE = Source(
    id="test-file-fixture",
    type=SourceType.file,
    url="file://./tests/fixtures/fetch_only_feed.xml",  # 测试专用,避免与 local-fixture 撞 item_id
    category="tech",
    region="global",
    lang="en",
)


@pytest.mark.asyncio
async def test_fetch_file_source_stores_item():
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过 fetch 集成测试:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        summary = await fetch_and_store([FILE_SOURCE], settings, sessionmaker=sm)
        assert summary["sources_ok"] == 1
        assert summary["sources_failed"] == 0
        assert summary["items"] == 1

        async with sm() as session:
            rows = (
                await session.execute(select(Item).where(Item.source == FILE_SOURCE.id))
            ).scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.title == "Fetch integration test only headline"
            assert row.published_at is not None  # fixture 自带发布时间
            assert row.category == "tech"
    finally:
        # 清理:删掉本测试写入的行,不留脏数据
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == FILE_SOURCE.id))
        await engine.dispose()


NO_DATE_SOURCE = Source(
    id="test-no-date-fixture",
    type=SourceType.file,
    url="file://./tests/fixtures/no_date_feed.xml",
    category="tech",
    region="global",
    lang="en",
)


@pytest.mark.asyncio
async def test_fetch_published_at_fallback_to_fetch_time():
    """条目无发布时间时,published_at 应兜底为抓取时间(非空)。"""
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        summary = await fetch_and_store([NO_DATE_SOURCE], settings, sessionmaker=sm)
        assert summary["items"] == 1
        async with sm() as session:
            row = (
                await session.execute(select(Item).where(Item.source == NO_DATE_SOURCE.id))
            ).scalar_one()
            assert row.published_at is not None  # 兜底:无 pubDate 也有发布时间
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == NO_DATE_SOURCE.id))
        await engine.dispose()


# ----- 2026-07 P1:trust_published_at=false → published_at 存 NULL(治假日期源) ----- #
UNTRUSTED_DATE_SOURCE = Source(
    id="test-untrusted-date-fixture",
    type=SourceType.file,
    url="file://./tests/fixtures/fetch_only_feed.xml",
    category="tech",
    region="global",
    lang="en",
    trust_published_at=False,
)


@pytest.mark.asyncio
async def test_fetch_untrusted_date_source_stores_null_published_at():
    """trust_published_at=false:即使 feed 带日期也存 NULL——该源日期不可信(date_suspect=1.0),
    NULL 让下游按"无日期"走(进不了新鲜窗),堵死旧闻冒充今天。"""
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        summary = await fetch_and_store([UNTRUSTED_DATE_SOURCE], settings, sessionmaker=sm)
        assert summary["items"] == 1
        async with sm() as session:
            row = (
                await session.execute(
                    select(Item).where(Item.source == UNTRUSTED_DATE_SOURCE.id)
                )
            ).scalar_one()
            assert row.published_at is None  # feed 有 pubDate 也不信,存 NULL
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(
                    delete(Item).where(Item.source == UNTRUSTED_DATE_SOURCE.id)
                )
        await engine.dispose()


# ----- 2026-07 P1:url/title 排除过滤(HF 社区洪水 / codex alpha 刷屏) ----- #
def test_apply_exclude_filters_url_and_title():
    from pulsewire.fetch.pipeline import apply_exclude_filters
    from pulsewire.sources.base import RawItem

    src = Source(
        id="test-filter", type=SourceType.rss, url="https://x.example/feed",
        url_exclude_patterns=[r"^https://example\.com/blog/[^/]+/[^/]+$"],
        title_exclude_patterns=[r"(?i)\b(alpha|beta|rc|nightly)\b"],
    )
    items = [
        RawItem(url="https://example.com/blog/official-post", title="Official launch"),
        RawItem(url="https://example.com/blog/some-user/community-post", title="Community post"),
        RawItem(url="https://example.com/releases/a35", title="0.143.0-alpha.35"),
        RawItem(url="https://example.com/blog/research-post", title="Research is fine"),
    ]
    kept = apply_exclude_filters(src, items)
    assert [i.title for i in kept] == ["Official launch", "Research is fine"]


def test_apply_exclude_filters_noop_without_patterns():
    from pulsewire.fetch.pipeline import apply_exclude_filters
    from pulsewire.sources.base import RawItem

    src = Source(id="test-nofilter", type=SourceType.rss, url="https://x.example/feed")
    items = [RawItem(url="https://example.com/a", title="0.1.0-alpha keeps without config")]
    assert apply_exclude_filters(src, items) == items


@pytest.mark.asyncio
async def test_fetch_exclude_filters_end_to_end():
    """走完整 fetch_and_store:命中 url/title 排除的条目不落库。"""
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    src = Source(
        id="test-exclude-filter-fixture",
        type=SourceType.file,
        url="file://./tests/fixtures/exclude_filter_feed.xml",
        category="tech", region="global", lang="en",
        url_exclude_patterns=[r"^https://example\.com/blog/[^/]+/[^/]+$"],
        title_exclude_patterns=[r"(?i)\b(alpha|beta|rc|nightly)\b"],
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        summary = await fetch_and_store([src], settings, sessionmaker=sm)
        assert summary["items"] == 1  # 3 条里踢 2 条(社区 url + alpha 标题)
        async with sm() as session:
            rows = (
                await session.execute(select(Item).where(Item.source == src.id))
            ).scalars().all()
            assert [r.title for r in rows] == ["Official launch post keep me"]
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == src.id))
        await engine.dispose()


# ----- 2026-07 P1:upsert facts 按键合并(重抓不再抹掉 enrich 写的 fulltext) ----- #
@pytest.mark.asyncio
async def test_upsert_refetch_preserves_enriched_facts():
    from pulsewire.store import upsert_item

    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    url = "https://example.com/facts-merge-test"
    title = "facts merge test headline"
    try:
        async with sm() as session:
            async with session.begin():
                # 首抓:feed 自带 facts(如 hn 数值)
                iid = await upsert_item(
                    session, source="test-facts-merge", url=url, title=title,
                    content="body", facts={"hn": {"points": 1}},
                )
                # enrich 写入 fulltext(模拟 update_item_facts 后的状态:直接再 upsert 带 fulltext)
                await upsert_item(
                    session, source="test-facts-merge", url=url, title=title,
                    content="body", facts={"fulltext": {"text": "FULL", "chars": 4}},
                )
                # 隔天重抓:feed 只带新鲜 hn 数值,不带 fulltext → fulltext 必须还在,hn 取新值
                await upsert_item(
                    session, source="test-facts-merge", url=url, title=title,
                    content="body", facts={"hn": {"points": 99}},
                )
        async with sm() as session:
            row = (
                await session.execute(select(Item).where(Item.item_id == iid))
            ).scalar_one()
            assert row.facts["fulltext"]["text"] == "FULL"  # 重抓没抹掉富化成果
            assert row.facts["hn"]["points"] == 99  # 新鲜数值同键覆盖
        async with sm() as session:
            async with session.begin():
                # 重抓 facts=None → 保留已有 facts(不整块置空)
                await upsert_item(
                    session, source="test-facts-merge", url=url, title=title,
                    content="body", facts=None,
                )
        async with sm() as session:
            row = (
                await session.execute(select(Item).where(Item.item_id == iid))
            ).scalar_one()
            assert row.facts["fulltext"]["text"] == "FULL"
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == "test-facts-merge"))
        await engine.dispose()
