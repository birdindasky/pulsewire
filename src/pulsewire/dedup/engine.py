"""三级去重 + 跨源同事件合并(归簇)。

- 一级(URL):入库时 item_id=规范化URL+指纹,已挡完全同一条(`store.upsert_item`)。
- 二级(指纹):标题+正文完全相同(不同 URL)→ 合到同簇。
- 三级(语义):embedding 余弦近邻(pgvector),≥阈值且在窗口内 → 合到同簇(跨源/跨语言)。

按发布时间升序处理:首条派生 cluster_id、跨天稳定;后到的并入最近邻所属簇,否则自立新簇。
失败要冒泡:算不出向量直接报错,不静默退化成"不去重"。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.store import (
    assign_cluster,
    create_cluster,
    find_fingerprint_cluster,
    find_similar_cluster,
    get_unclustered_items,
    refresh_cluster,
    upsert_embedding,
)
from pulsewire.store.ids import make_cluster_id

from .embedding import get_embedder

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pulsewire.config import Settings
    from pulsewire.store.tables import Item

log = get_logger()


def _text(item: Item) -> str:
    """用于算向量的文本:**只用标题**。

    实测:不同源对同一事件的正文摘要不对称(一个源有 description、一个没有),
    若把正文拼进来会让"同一条新闻"的向量发散、与"不同新闻"重叠,无法用阈值分开。
    只用标题最稳:跨源同标题→近乎相同向量,干净地高于无关条目。
    """
    return item.title


async def run_dedup(
    session: AsyncSession,
    settings: Settings,
    *,
    since: datetime | None = None,
    embedder=None,
) -> dict:
    """对未归簇条目做三级去重并归簇,返回汇总 dict。"""
    emb_cfg = settings.dedup.embedding
    items = await get_unclustered_items(session, since=since)
    if not items:
        return {"processed": 0, "new_clusters": 0, "merged": 0, "by_fingerprint": 0, "by_embedding": 0}

    # 算并存所有目标条目的向量(批量,一次模型调用)
    vectors: dict[str, list[float]] = {}
    if emb_cfg.enabled:
        embedder = embedder or get_embedder(settings)
        # 挪出事件循环线程(f03):嵌入是同步 CPU/GPU 活,MLX/Metal 万一卡死若跑在事件循环上
        # 会把整个 async 流水线冻死、协作式看门狗也永远触发不了。与 events/engine.py 侧一致。
        computed = await asyncio.to_thread(embedder.embed, [_text(it) for it in items])
        for it, vec in zip(items, computed):
            vectors[it.item_id] = vec
            await upsert_embedding(session, item_id=it.item_id, vector=vec, model=embedder.model_name)
        await session.flush()

    window_start = datetime.now(timezone.utc) - timedelta(hours=emb_cfg.recency_window_hours)

    new_clusters = merged = by_fingerprint = by_embedding = 0
    for it in items:
        cluster_id: str | None = None

        # 二级:内容指纹完全相同
        cluster_id = await find_fingerprint_cluster(
            session, fingerprint=it.content_fingerprint, exclude_item_id=it.item_id
        )
        if cluster_id is not None:
            by_fingerprint += 1

        # 三级:语义近邻
        if cluster_id is None and emb_cfg.enabled:
            hit = await find_similar_cluster(
                session,
                vector=vectors[it.item_id],
                threshold=emb_cfg.similarity_threshold,
                since=window_start,
                exclude_item_id=it.item_id,
            )
            if hit is not None:
                cluster_id, sim = hit
                by_embedding += 1
                log.info("dedup.merge", item=it.item_id, cluster=cluster_id, sim=round(sim, 3))

        if cluster_id is None:
            cluster_id = make_cluster_id(it.item_id)
            await create_cluster(
                session,
                cluster_id=cluster_id,
                first_item_id=it.item_id,
                title=it.title,
                seen_at=it.published_at,
            )
            new_clusters += 1
        else:
            merged += 1

        await assign_cluster(session, item_id=it.item_id, cluster_id=cluster_id)
        await refresh_cluster(session, cluster_id)
        # flush 让后续条目的最近邻查询能看到刚归簇的这条
        await session.flush()

    return {
        "processed": len(items),
        "new_clusters": new_clusters,
        "merged": merged,
        "by_fingerprint": by_fingerprint,
        "by_embedding": by_embedding,
    }
