"""HackerNews 适配器(Algolia 公共 API:一次拿到 points / 评论数)。

源 url 用 Algolia 检索端点(如 front_page),按"取标题/外链/时间 + points/评论数"实现。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .base import RawItem

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient

_HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"


def _parse(payload: str) -> list[RawItem]:
    data = json.loads(payload)
    items: list[RawItem] = []
    for hit in data.get("hits", []):
        object_id = hit.get("objectID")
        title = (hit.get("title") or hit.get("story_title") or "").strip()
        if not title:
            continue
        # Ask/Show HN 等无外链 → 指向 HN 讨论页
        url = hit.get("url") or hit.get("story_url") or _HN_ITEM_URL.format(object_id)
        ts = hit.get("created_at_i")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        facts = {
            "hn": {
                "points": hit.get("points"),
                "num_comments": hit.get("num_comments"),
                "object_id": object_id,
            }
        }
        items.append(RawItem(url=url, title=title, published_at=published, facts=facts))
    return items


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    resp = await client.get(source.url)
    if resp.not_modified:
        return []
    return _parse(resp.text)
