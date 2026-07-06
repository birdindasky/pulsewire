"""enrich 测试:派生事实带正确 value + source_id;run_enrich 写回 facts.enriched(需库)。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pulsewire.config import get_settings
from pulsewire.enrich import extract_facts, run_enrich
from pulsewire.store import upsert_item
from pulsewire.store.ids import make_source_id
from pulsewire.store.tables import Item

_SRC = "enrich-test-src"


def test_extract_facts_hn():
    item_id = "a" * 32
    facts = {"hn": {"points": 901, "num_comments": 876, "object_id": "48434312"}}
    out = extract_facts(item_id, facts)
    by_kind = {f["kind"]: f for f in out}
    assert by_kind["hn.points"]["value"] == 901
    assert by_kind["hn.points"]["source_id"] == make_source_id(item_id, "hn", "points")
    assert by_kind["hn.comments"]["value"] == 876
    assert by_kind["hn.comments"]["source_id"] == make_source_id(item_id, "hn", "num_comments")


def test_extract_facts_github():
    item_id = "b" * 32
    facts = {"github": {"stars": 221751, "forks": 50731}}
    out = extract_facts(item_id, facts)
    by_kind = {f["kind"]: f for f in out}
    assert by_kind["github.stars"]["value"] == 221751
    assert by_kind["github.stars"]["source_id"] == make_source_id(item_id, "github", "stars")
    assert by_kind["github.forks"]["value"] == 50731


def test_extract_facts_empty_and_partial():
    assert extract_facts("c" * 32, None) == []
    assert extract_facts("c" * 32, {}) == []
    # 只有 points、没有评论数 → 只出一条
    out = extract_facts("c" * 32, {"hn": {"points": 5}})
    assert len(out) == 1 and out[0]["kind"] == "hn.points"


def test_source_id_never_from_model():
    """source_id 完全由 item_id 派生,跑两次完全一致(确定性,不让模型编)。"""
    item_id = "d" * 32
    facts = {"hn": {"points": 10, "num_comments": 2}}
    assert extract_facts(item_id, facts) == extract_facts(item_id, facts)


@pytest.mark.asyncio
async def test_run_enrich_writes_back_facts():
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    item_id = None
    try:
        async with sm() as session:
            async with session.begin():
                item_id = await upsert_item(
                    session, source=_SRC, url="https://enrich.example/x",
                    title="Enrich test item", published_at=now,
                    facts={"hn": {"points": 42, "num_comments": 7}},
                )

        summary = await run_enrich(settings, fulltext=False, sessionmaker=sm)
        assert summary["items_enriched"] >= 1

        async with sm() as session:
            facts = (
                await session.execute(select(Item.facts).where(Item.item_id == item_id))
            ).scalar_one()
            enriched = {f["kind"]: f for f in facts["enriched"]}
            assert enriched["hn.points"]["value"] == 42
            assert enriched["hn.points"]["source_id"] == make_source_id(item_id, "hn", "points")
            # 原始事实没被丢
            assert facts["hn"]["points"] == 42
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == _SRC))
        await engine.dispose()


# ----- 2026-07 P1:按源全文富化(enrich: ["fulltext"] 的源,瘦正文条目回源抓全文) ----- #
_FT_SRC = "enrich-fulltext-test-src"


def _ft_source(**kw):
    from pulsewire.config.models import Source, SourceType

    defaults = dict(
        id=_FT_SRC, type=SourceType.rss, url="https://ft-test.example/feed",
        enrich=["fulltext"], enabled=True,
    )
    defaults.update(kw)
    return Source(**defaults)


@pytest.mark.asyncio
async def test_run_enrich_flagged_source_fulltext(monkeypatch):
    """flagged 源的瘦正文条目 → facts.fulltext 落库;厚正文/已有全文/未 flag 的都不抓;
    单条失败(返回 None)只跳过不炸;每 run 硬顶生效。"""
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    fetched_urls: list[str] = []

    async def _fake_fulltext(item, settings, *, client=None):
        fetched_urls.append(item.url)
        if "fail" in item.url:
            return None  # 模拟抓取失败:log+skip,不拖垮整批
        return {"text": "REAL FULL BODY " * 20, "chars": 300, "source_id": f"{item.item_id}:fulltext:text"}

    import pulsewire.enrich.engine as engine_mod
    monkeypatch.setattr(engine_mod, "fetch_fulltext", _fake_fulltext)

    ids = {}
    try:
        async with sm() as session:
            async with session.begin():
                ids["thin"] = await upsert_item(
                    session, source=_FT_SRC, url="https://ft-test.example/thin",
                    title="thin item", content="tiny", published_at=now)
                ids["fail"] = await upsert_item(
                    session, source=_FT_SRC, url="https://ft-test.example/fail",
                    title="fail item", content="tiny2", published_at=now)
                ids["thick"] = await upsert_item(
                    session, source=_FT_SRC, url="https://ft-test.example/thick",
                    title="thick item", content="x" * 5000, published_at=now)
                ids["done"] = await upsert_item(
                    session, source=_FT_SRC, url="https://ft-test.example/done",
                    title="already enriched", content="tiny3", published_at=now,
                    facts={"fulltext": {"text": "OLD", "chars": 3}})
                ids["other"] = await upsert_item(
                    session, source="enrich-ft-unflagged", url="https://ft-test.example/other",
                    title="unflagged source item", content="tiny4", published_at=now)

        summary = await run_enrich(
            settings, fulltext=False, sessionmaker=sm,
            sources=[_ft_source()],  # 只 flag 一个源;unflagged 源不在表里等价未 flag
        )
        # thin+fail 是候选;thick(正文厚)/done(已有全文)/other(未 flag)都不是
        assert summary["fulltext_sources"] == 1
        assert summary["fulltext_candidates"] == 2
        assert summary["fulltext_ok"] == 1
        assert sorted(fetched_urls) == [
            "https://ft-test.example/fail", "https://ft-test.example/thin",
        ]

        async with sm() as session:
            facts_thin = (await session.execute(
                select(Item.facts).where(Item.item_id == ids["thin"]))).scalar_one()
            assert facts_thin["fulltext"]["text"].startswith("REAL FULL BODY")
            facts_fail = (await session.execute(
                select(Item.facts).where(Item.item_id == ids["fail"]))).scalar_one()
            assert not (facts_fail or {}).get("fulltext")  # 失败条目没写假数据
            facts_done = (await session.execute(
                select(Item.facts).where(Item.item_id == ids["done"]))).scalar_one()
            assert facts_done["fulltext"]["text"] == "OLD"  # 已有全文不重抓不覆盖
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(
                    Item.source.in_([_FT_SRC, "enrich-ft-unflagged"])))
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_enrich_fulltext_cap_bounds_attempts(monkeypatch):
    """每 run 硬顶:候选超过 fulltext_max_per_run 时只尝试顶内条数(计尝试数,防失控)。"""
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError) as exc:
        await engine.dispose()
        pytest.skip(f"数据库不可用,跳过:{exc}")

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    calls: list[str] = []

    async def _fake_fulltext(item, settings, *, client=None):
        calls.append(item.url)
        return {"text": "BODY " * 30, "chars": 150, "source_id": f"{item.item_id}:fulltext:text"}

    import pulsewire.enrich.engine as engine_mod
    monkeypatch.setattr(engine_mod, "fetch_fulltext", _fake_fulltext)
    monkeypatch.setattr(settings.enrich, "fulltext_max_per_run", 3)

    try:
        async with sm() as session:
            async with session.begin():
                for i in range(6):
                    await upsert_item(
                        session, source=_FT_SRC, url=f"https://ft-test.example/cap{i}",
                        title=f"cap item {i}", content="tiny", published_at=now)
        summary = await run_enrich(settings, fulltext=False, sessionmaker=sm, sources=[_ft_source()])
        assert summary["fulltext_candidates"] == 3  # 硬顶截断
        assert len(calls) == 3
    finally:
        async with sm() as session:
            async with session.begin():
                await session.execute(delete(Item).where(Item.source == _FT_SRC))
        await engine.dispose()
