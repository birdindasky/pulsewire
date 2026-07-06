"""交付层单一日期事实源(f04/f05)—— 治"拿昨天冒充今天"和"跨午夜顶掉次日"。

- _logical_date:交付日期/收据/幂等键锚到 run 逻辑日,而非交付那一刻钟点(f05)。
- _fresh_image:只推本 run 刚渲的图,某域 render 失败留的旧图不上车(f04 通道三)。
- get_summaries/get_digest 的 run_id 过滤:某域今日 summarize 失败时不拿旧稿冒充今天(f04 通道一)。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pulsewire.config import get_settings
from pulsewire.deliver.engine import _fresh_image, _logical_date

TZ = ZoneInfo("America/Los_Angeles")


# ---------------- f05:逻辑日期从 run_id 推 ---------------- #
def test_logical_date_from_run_id():
    assert _logical_date("daily_20260703", TZ) == "2026-07-03"
    assert _logical_date("event_20261225", TZ) == "2026-12-25"


def test_logical_date_fallback_to_today_when_unparseable():
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    assert _logical_date(None, TZ) == today
    assert _logical_date("weird-id", TZ) == today
    assert _logical_date("daily_2026", TZ) == today  # 尾巴非 8 位数字 → 退回今天


# ---------------- f04 通道三:图新鲜窗 ---------------- #
def test_fresh_image_recent(tmp_path):
    p = tmp_path / "d.png"
    p.write_bytes(b"x")
    assert _fresh_image(p) is True


def test_fresh_image_stale_not_pushed(tmp_path):
    p = tmp_path / "old.png"
    p.write_bytes(b"x")
    old = time.time() - 24 * 3600  # 昨天的旧图
    os.utime(p, (old, old))
    assert _fresh_image(p) is False


def test_fresh_image_missing(tmp_path):
    assert _fresh_image(tmp_path / "nope.png") is False


# ---------------- f04 通道一:get_summaries/get_digest 按 run_id 过滤 ---------------- #
@pytest.mark.asyncio
async def test_summaries_digest_run_id_filter():
    from sqlalchemy import delete
    from sqlalchemy.exc import InterfaceError, OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from pulsewire.store import (
        create_run,
        get_digest,
        get_summaries,
        upsert_digest,
        upsert_item,
        upsert_summary,
    )
    from pulsewire.store.tables import Digest, Item, Run, Summary

    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    try:
        conn = await engine.connect()
        await conn.close()
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        await engine.dispose()
        pytest.skip("数据库不可用,跳过")
    sm = async_sessionmaker(engine, expire_on_commit=False)

    ik = "int_f04test"
    run_a, run_b = "daily_f04a", "daily_f04b"
    iid = None
    try:
        async with sm() as s:
            async with s.begin():
                await create_run(s, trigger_type="daily", run_id=run_a)  # summaries.run_id FK → runs
                iid = await upsert_item(
                    s, source="f04t", url="https://f04.example/1", title="标题",
                    content="正文", published_at=datetime.now(timezone.utc))
                await upsert_summary(
                    s, interest_key=ik, item_id=iid, cluster_id=None, headline="H",
                    tldr_raw="t", tldr_rendered="t", insight_raw="i", insight_rendered="i",
                    status="ok", used_source_ids=[], unresolved=[], suspect=[],
                    backend="api", model="m", run_id=run_a)
                await upsert_digest(
                    s, interest_key=ik, digest="概述A", backend="api", model="m", run_id=run_a)

        async with sm() as s:
            # run_a(本次 run):拿得到
            assert len(await get_summaries(s, interest_key=ik, run_id=run_a)) == 1
            assert (await get_digest(s, interest_key=ik, run_id=run_a)) is not None
            # run_b(今日跑,库里只有昨天 run_a 的稿):过滤成空/None → 绝不拿旧稿冒充今天
            assert await get_summaries(s, interest_key=ik, run_id=run_b) == []
            assert (await get_digest(s, interest_key=ik, run_id=run_b)) is None
            # 不传 run_id:全取(在追归线 / 历史召回 back-compat 不受影响)
            assert len(await get_summaries(s, interest_key=ik)) == 1
            assert (await get_digest(s, interest_key=ik)) is not None
    finally:
        async with sm() as s:
            async with s.begin():
                await s.execute(delete(Summary).where(Summary.interest_key == ik))
                await s.execute(delete(Digest).where(Digest.interest_key == ik))
                if iid:
                    await s.execute(delete(Item).where(Item.item_id == iid))
                await s.execute(delete(Run).where(Run.run_id == run_a))
        await engine.dispose()
