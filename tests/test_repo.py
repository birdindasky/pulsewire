"""仓储层测试(需要数据库;连不上自动跳过)。"""

from __future__ import annotations

import pytest

from pulsewire.store import repo
from pulsewire.store.ids import make_item_id

pytestmark = pytest.mark.asyncio


async def test_write_and_read_item(db_session):
    item_id = await repo.upsert_item(
        db_session,
        source="hn-frontpage",
        url="https://example.com/story?utm_source=feed",
        title="Round-trip item",
        content="hello",
    )
    assert item_id == make_item_id("https://example.com/story", "Round-trip item", "hello")

    got = await repo.get_item(db_session, item_id)
    assert got is not None
    assert got.title == "Round-trip item"
    assert got.source == "hn-frontpage"
    assert got.normalized_url == "https://example.com/story"


async def test_add_item_timeline_records_star_snapshot(db_session):
    """item_timeline 追加 star 快照(增速排序的数据来源):同 item 多行 = 轨迹,不 upsert。"""
    from sqlalchemy import select

    from pulsewire.store.tables import ItemTimeline

    item_id = await repo.upsert_item(
        db_session, source="ai-trending", url="https://github.com/o/r", title="repo",
        facts={"github": {"stars": 1000}},
    )
    await repo.add_item_timeline(db_session, item_id=item_id, trigger_type="daily", rank=1, stars=1000)
    await repo.add_item_timeline(db_session, item_id=item_id, trigger_type="daily", rank=1, stars=1200)

    rows = (await db_session.execute(
        select(ItemTimeline).where(ItemTimeline.item_id == item_id).order_by(ItemTimeline.stars)
    )).scalars().all()
    assert [r.stars for r in rows] == [1000, 1200]  # 两次快照都在(纯追加)
    assert all(r.observed_at is not None for r in rows)  # observed_at 由库 now() 自动填


async def test_upsert_is_idempotent(db_session):
    args = dict(source="s", url="https://example.com/x", title="T", content="c")
    id1 = await repo.upsert_item(db_session, **args)
    id2 = await repo.upsert_item(db_session, **args)
    assert id1 == id2  # 同 URL+内容 → 同 item_id,不产生重复行


async def test_delivery_idempotency_key_blocks_duplicate(db_session):
    first = await repo.record_delivery(
        db_session, cluster_id="clt_abc", channel="feishu", trigger_type="daily"
    )
    dup = await repo.record_delivery(
        db_session, cluster_id="clt_abc", channel="feishu", trigger_type="daily"
    )
    other_channel = await repo.record_delivery(
        db_session, cluster_id="clt_abc", channel="wechat", trigger_type="daily"
    )
    assert first is True       # 首次登记 → 应投递
    assert dup is False        # 同键重复 → 挡住,不重复推
    assert other_channel is True  # 换渠道 → 允许


async def _put_summary(session, interest_key, item_id):
    await repo.upsert_summary(
        session, interest_key=interest_key, item_id=item_id, cluster_id=None,
        headline="h", tldr_raw="t", tldr_rendered="t", insight_raw="i", insight_rendered="i",
        status="ok", used_source_ids=[], unresolved=[], suspect=[], backend="api", model="m",
    )


async def test_prune_summaries_drops_stale_keeps_produced(db_session):
    """分块失败跳过的条目:本轮没产出 → 旧总结被 prune 删掉,不冒充本轮上线。"""
    ik = "int_prunetest"
    i1 = await repo.upsert_item(db_session, source="s", url="https://e.com/1", title="A", content="a")
    i2 = await repo.upsert_item(db_session, source="s", url="https://e.com/2", title="B", content="b")
    await _put_summary(db_session, ik, i1)
    await _put_summary(db_session, ik, i2)

    # 本轮只产出 i1 → 删掉不在 keep 里的 i2 旧总结
    deleted = await repo.prune_summaries(db_session, interest_key=ik, keep_item_ids=[i1])
    assert deleted == 1
    left = {s.item_id for s in await repo.get_summaries(db_session, interest_key=ik)}
    assert left == {i1}

    # keep 为空(全块失败)→ 不删,交上层冒泡,不静默清空
    deleted0 = await repo.prune_summaries(db_session, interest_key=ik, keep_item_ids=[])
    assert deleted0 == 0
    assert {s.item_id for s in await repo.get_summaries(db_session, interest_key=ik)} == {i1}


async def test_prune_is_soft_delete_and_revivable(db_session):
    """2026-06-15 二⑦:prune 是软删(行还在、可追溯),重新产出同条目 → upsert 复活;
    且重复 prune 不二次计数(只标尚未软删的行)。"""
    from sqlalchemy import func, select

    from pulsewire.store.tables import Summary

    ik = "int_softdel"
    i1 = await repo.upsert_item(db_session, source="s", url="https://e.com/sd1", title="A", content="a")
    i2 = await repo.upsert_item(db_session, source="s", url="https://e.com/sd2", title="B", content="b")
    await _put_summary(db_session, ik, i1)
    await _put_summary(db_session, ik, i2)

    # 软删 i2:get_summaries 看不到,但行还在(pruned_at 非空)
    assert await repo.prune_summaries(db_session, interest_key=ik, keep_item_ids=[i1]) == 1
    assert {s.item_id for s in await repo.get_summaries(db_session, interest_key=ik)} == {i1}
    total = (await db_session.execute(
        select(func.count()).select_from(Summary).where(Summary.interest_key == ik))).scalar()
    assert total == 2  # 物理行未删,可追溯

    # 重复 prune 同一份 → 不二次标记(幂等,只标尚未软删的)
    assert await repo.prune_summaries(db_session, interest_key=ik, keep_item_ids=[i1]) == 0

    # 重新产出 i2 → 复活(pruned_at 置回 NULL),get_summaries 重新可见
    await _put_summary(db_session, ik, i2)
    assert {s.item_id for s in await repo.get_summaries(db_session, interest_key=ik)} == {i1, i2}


async def test_get_embeddings_by_ids_covers_full_set(db_session):
    """2026-06-15 二①:get_embeddings_by_ids 按 id 取向量(不受时间窗限制),覆盖整个候选集。"""
    from pulsewire.store.base import EMBEDDING_DIM

    i1 = await repo.upsert_item(db_session, source="s", url="https://e.com/v1", title="A")
    i2 = await repo.upsert_item(db_session, source="s", url="https://e.com/v2", title="B")
    await repo.upsert_embedding(db_session, item_id=i1, vector=[0.1] * EMBEDDING_DIM, model="test")
    await repo.upsert_embedding(db_session, item_id=i2, vector=[0.2] * EMBEDDING_DIM, model="test")

    got = await repo.get_embeddings_by_ids(db_session, [i1, i2, "nonexistent-id"])
    assert set(got) == {i1, i2}                 # 不存在的 id 不在返回里(调用方按缺失处理)
    assert len(got[i1]) == EMBEDDING_DIM
    assert await repo.get_embeddings_by_ids(db_session, []) == {}  # 空入参 → 空 dict


async def test_get_source_item_stats_groups_by_source(db_session):
    """2026-06-15 三①:源治理体检——按 source 统计条目数,供孤儿源识别。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    await repo.upsert_item(db_session, source="src-a", url="https://e.com/sa1", title="A1", published_at=now)
    await repo.upsert_item(db_session, source="src-a", url="https://e.com/sa2", title="A2", published_at=now)
    await repo.upsert_item(db_session, source="src-b", url="https://e.com/sb1", title="B1", published_at=now)

    stats = {s: (c, last) for s, c, last in await repo.get_source_item_stats(db_session)}
    assert stats["src-a"][0] == 2 and stats["src-b"][0] == 1  # 按源分组计数
    assert stats["src-a"][1] is not None  # 带最近 published_at


async def test_delete_orphan_items_keeps_registered(db_session):
    """2026-06-15 三①清理:删 source 不在注册表的孤儿条目;在册的留;空注册表→不删(安全闸)。"""
    reg = await repo.upsert_item(db_session, source="src-registered", url="https://e.com/reg", title="R")
    orphan = await repo.upsert_item(db_session, source="src-gone", url="https://e.com/orphan", title="O")

    # 空注册表 → 一个不删(防注册表加载异常清空全库)
    assert await repo.delete_orphan_items(db_session, []) == 0
    assert await repo.get_item(db_session, orphan) is not None

    # 注册表只含 src-registered → 删掉 src-gone 的孤儿,留 src-registered
    deleted = await repo.delete_orphan_items(db_session, ["src-registered"])
    assert deleted >= 1
    assert await repo.get_item(db_session, reg) is not None       # 在册留
    assert await repo.get_item(db_session, orphan) is None        # 孤儿删
