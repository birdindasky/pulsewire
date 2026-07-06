"""存储层(PostgreSQL + pgvector,SQLAlchemy async)。

- base    : 引擎 / 会话 / 声明基类
- tables  : ORM 表(数据契约)
- ids     : 确定性 ID 生成(item_id / cluster_id / source_id)
- repo    : 仓储读写(含投递幂等)
表结构变更走 Alembic(migrations/)。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from pulsewire.config import get_settings

from .base import Base, get_engine, get_sessionmaker
from .ids import make_cluster_id, make_item_id, make_source_id, normalize_url
from .repo import (
    add_item_timeline,
    assign_cluster,
    count_items,
    create_cluster,
    create_run,
    find_fingerprint_cluster,
    find_similar_cluster,
    finish_run,
    get_run,
    set_run_stage,
    get_digest,
    get_cached_judgments,
    upsert_judgments,
    has_delivery,
    get_item,
    get_fulltext_candidates,
    get_items,
    get_embeddings_by_ids,
    get_items_by_ids,
    get_latest_timeline_stars,
    get_rankings,
    get_recent_embeddings,
    get_source_item_stats,
    delete_orphan_items,
    get_summaries,
    get_unclustered_items,
    get_recent_items_by_sources,
    prune_rankings,
    prune_summaries,
    recall_by_vector,
    record_delivery,
    refresh_cluster,
    update_item_facts,
    get_active_threads,
    get_active_thread_cluster_map,
    linked_cluster_ids,
    create_thread,
    link_cluster_to_thread,
    touch_thread,
    mark_dormant_threads,
    get_threads_for_display,
    clear_threads,
    upsert_digest,
    upsert_embedding,
    upsert_item,
    upsert_ranking,
    upsert_summary,
)


async def ping_database() -> dict[str, object]:
    """连接数据库并返回基本信息;连不上会抛异常(失败要冒泡,不静默吞)。"""
    engine = create_async_engine(get_settings().database.async_dsn, echo=False)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar_one()
            pgvector = (
                await conn.execute(
                    text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
                )
            ).first()
        return {
            "connected": True,
            "server_version": str(version).split(",")[0],
            "pgvector_available": pgvector is not None,
        }
    finally:
        await engine.dispose()


__all__ = [
    "Base",
    "add_item_timeline",
    "get_engine",
    "get_sessionmaker",
    "ping_database",
    "make_item_id",
    "make_cluster_id",
    "make_source_id",
    "normalize_url",
    "upsert_item",
    "get_item",
    "create_run",
    "finish_run",
    "get_run",
    "set_run_stage",
    "record_delivery",
    "has_delivery",
    "count_items",
    "get_fulltext_candidates",
    "get_items",
    "get_items_by_ids",
    "get_latest_timeline_stars",
    "get_active_threads",
    "get_active_thread_cluster_map",
    "linked_cluster_ids",
    "create_thread",
    "link_cluster_to_thread",
    "touch_thread",
    "mark_dormant_threads",
    "get_threads_for_display",
    "clear_threads",
    "get_rankings",
    "get_summaries",
    "get_digest",
    "get_cached_judgments",
    "upsert_judgments",
    "update_item_facts",
    "get_unclustered_items",
    "get_recent_embeddings",
    "get_embeddings_by_ids",
    "get_source_item_stats",
    "delete_orphan_items",
    "get_recent_items_by_sources",
    "upsert_embedding",
    "recall_by_vector",
    "upsert_ranking",
    "prune_rankings",
    "prune_summaries",
    "upsert_summary",
    "upsert_digest",
    "find_fingerprint_cluster",
    "find_similar_cluster",
    "create_cluster",
    "assign_cluster",
    "refresh_cluster",
]
