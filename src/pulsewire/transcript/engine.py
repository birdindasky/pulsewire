"""transcript 引擎:精排后对入选条目抓网页正文/逐字稿,写回 facts.fulltext。

只对 rankings(已限到 final_limit)里的条目抓 → 网络与 token 都有界。
幂等:已有 facts.fulltext 的跳过;单条失败 best-effort 不拖垮整批(失败要记日志,不静默)。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

from pulsewire.enrich import fetch_fulltext
from pulsewire.obs import get_logger
from pulsewire.store import (
    get_items_by_ids,
    get_rankings,
    get_sessionmaker,
    update_item_facts,
)

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


async def run_transcript(
    settings: Settings,
    *,
    interest_key: str,
    run_id: str | None = None,
    sessionmaker=None,
) -> dict:
    """对某兴趣的精排入选条目抓正文/逐字稿,写回 facts.fulltext。返回汇总 dict。"""
    if not settings.run.transcript:
        return {"interest_key": interest_key, "skipped": True, "reason": "run.transcript=false"}

    sm = sessionmaker or get_sessionmaker()

    # 1) 取精排入选条目,快照 (item_id, url, 是否已有 fulltext)——不在事务里跑网络
    async with sm() as session:
        rankings = await get_rankings(session, interest_key=interest_key)
        item_ids = [r.item_id for r in rankings]
        if not item_ids:
            return {"interest_key": interest_key, "ranked": 0, "fetched": 0, "note": "无精排结果"}
        items = await get_items_by_ids(session, item_ids)
        targets = [
            SimpleNamespace(item_id=it.item_id, url=it.url)
            for it in items
            if not (it.facts or {}).get("fulltext")
        ]
        skipped_existing = len(items) - len(targets)

    # 2) 并发抓正文(best-effort;数量 = 入选条目,有界)
    results = await asyncio.gather(*(fetch_fulltext(t, settings) for t in targets))

    # 3) 写回 facts.fulltext(一次事务)
    fetched = 0
    async with sm() as session:
        async with session.begin():
            fresh = await get_items_by_ids(session, [t.item_id for t in targets])
            fresh_by_id = {it.item_id: it for it in fresh}
            for t, ft in zip(targets, results):
                if ft is None:
                    continue
                it = fresh_by_id.get(t.item_id)
                if it is None:
                    continue
                new_facts = dict(it.facts or {})
                new_facts["fulltext"] = ft
                await update_item_facts(session, item_id=t.item_id, facts=new_facts)
                fetched += 1

    log.info(
        "transcript.done", interest_key=interest_key, ranked=len(item_ids),
        targeted=len(targets), fetched=fetched, skipped_existing=skipped_existing,
    )
    return {
        "interest_key": interest_key,
        "ranked": len(item_ids),
        "targeted": len(targets),
        "fetched": fetched,
        "skipped_existing": skipped_existing,
    }
