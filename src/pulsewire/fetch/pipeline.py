"""并发抓取 → 落库。

行为规格:
- 按 fetch.concurrency 限并发抓取所有启用源(网络阶段)。
- 单源失败:记录 + 标记,不静默吞、不拖垮其它源;全部失败才整体冒泡(不产空数据)。
- 落库:写一个事务,published_at 缺失回退抓取时间。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.sources import get_adapter
from pulsewire.store import get_sessionmaker, upsert_item

from .client import FetchClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    from pulsewire.config import Settings, Source
    from pulsewire.sources import RawItem

log = get_logger()


@dataclass(slots=True)
class SourceResult:
    source_id: str
    ok: bool
    items: int = 0
    error: str | None = None


def apply_exclude_filters(source: Source, items: list[RawItem]) -> list[RawItem]:
    """按源配置的 url/title 排除正则丢条目(2026-07 P1:HF 社区洪水 / codex alpha 刷屏)。

    在"取条上限"截断**之前**调用,免得垃圾条目占掉最新 N 条的坑。
    正则编译失败 = 配置写错,直接抛(单源 fail-loud,由上层按单源失败记录,不拖垮整批)。
    """
    if not source.url_exclude_patterns and not source.title_exclude_patterns:
        return items
    url_res = [re.compile(p) for p in source.url_exclude_patterns]
    title_res = [re.compile(p) for p in source.title_exclude_patterns]
    kept: list[RawItem] = []
    dropped = 0
    for it in items:
        if any(r.search(it.url or "") for r in url_res) or any(
            r.search(it.title or "") for r in title_res
        ):
            dropped += 1
            continue
        kept.append(it)
    if dropped:
        log.info("fetch.source.filtered", source=source.id, dropped=dropped, kept=len(kept))
    return kept


async def fetch_and_store(
    sources: list[Source],
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """抓取所有启用源并落库,返回汇总 dict。"""
    enabled = [s for s in sources if s.enabled]
    sem = asyncio.Semaphore(settings.fetch.concurrency)

    async def _collect(source: Source, client: FetchClient) -> tuple[Source, list[RawItem], Exception | None]:
        try:
            async with sem:
                items = await get_adapter(source.type)(source, client)
            # 条目级排除过滤(url/title 正则)在截断前应用,垃圾不占最新 N 条的坑
            items = apply_exclude_filters(source, items)
            # 取条上限:feed 通常最新在前,截前 N 条,兜住吐全量历史的源
            cap = source.max_items or settings.fetch.max_items_per_source
            if cap and len(items) > cap:
                items = items[:cap]
            return source, items, None
        except Exception as exc:  # 单源失败:冒泡到日志,但不拖垮整批
            log.error(
                "fetch.source.failed",
                source=source.id,
                type=source.type.value,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return source, [], exc

    # 网络阶段:并发抓取
    async with FetchClient(settings) as client:
        collected = await asyncio.gather(*(_collect(s, client) for s in enabled))

    # 落库阶段:单事务写入(published_at 兜底=抓取时间)
    now = datetime.now(timezone.utc)
    sm = sessionmaker or get_sessionmaker()
    results: list[SourceResult] = []
    async with sm() as session:
        async with session.begin():
            for source, items, error in collected:
                if error is not None:
                    results.append(SourceResult(source.id, ok=False, error=str(error)))
                    continue
                for it in items:
                    # 日期不可信源(trust_published_at=false):published_at 一律存 NULL——
                    # feed 日期是抓取时间冒充/缺失,回落"抓取时间"就是旧闻装新(date_suspect=1.0)。
                    # NULL 下游按"无日期"走:进不了新鲜窗(passes_freshness_window(None)=False),
                    # 仍可当互证成员。可信源维持原兜底(缺日期=抓取时间)。
                    published = (
                        (it.published_at or now) if source.trust_published_at else None
                    )
                    await upsert_item(
                        session,
                        source=source.id,
                        url=it.url,
                        title=it.title,
                        content=it.content,
                        published_at=published,
                        category=source.category,
                        region=source.region,
                        lang=source.lang,
                        facts=it.facts,
                    )
                log.info("fetch.source.ok", source=source.id, items=len(items))
                results.append(SourceResult(source.id, ok=True, items=len(items)))

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    summary = {
        "sources_total": len(enabled),
        "sources_ok": len(ok),
        "sources_failed": len(failed),
        "items": sum(r.items for r in ok),
        "failed_ids": [r.source_id for r in failed],
    }
    # 全部源失败 → 整体冒泡,绝不静默产出空数据(旧版踩过当天 0 产出)
    if enabled and not ok:
        raise RuntimeError(f"全部源抓取失败,不产空数据:{summary}")
    return summary
