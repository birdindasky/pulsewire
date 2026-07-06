"""HuggingFace 每日精选论文适配器(/api/daily_papers)。

上游:https://huggingface.co/api/daily_papers?limit=50(官方公开 API,2026-07-05 实探字段)。
响应是数组,元素形如 {"paper": {...}, "publishedAt": <精选页时间>, "title": ..., ...};
一手字段全在 paper 里:paper.id / title / summary / publishedAt(论文发布时间)/ upvotes。

🔴 日期铁律:published_at 只用 **paper.publishedAt**(一手论文时间)。顶层 publishedAt 是
上榜/精选页时间(实测比 paper.publishedAt 还早晚不一),拿它冒充发布时间=freshness 造假
(2026-06-25 GitHub 榜教训),不用;paper.publishedAt 缺失/认不出 → **整条丢弃**
(考官 2026-07-05 FAIL 复现:None 交给流水线会被 trust_published_at 默认路径兜底成
抓取时间=满格新鲜度进榜;trust_published_at:false 也不是药——它把真日期一并置空。
与 ossinsight._repo_to_item 同纪律:拿不到一手时间就不发条目)。

upvotes 不设门槛(2026-07-05 决定):榜单本身已是 HF 编辑每日精选;票数随时间累积,
当天新上榜论文票数天然低(实测 50 条分布 1~76、中位 ~6,最新一批普遍 <5),
设槛会系统性砍掉最新鲜的论文——恰是日报要的。原始票数存 facts.hf 备查/备扩。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from .base import RawItem

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient

_PAPER_URL = "https://huggingface.co/papers/{}"


def _parse_dt(value: str | None) -> datetime | None:
    """ISO8601(带 Z)→ aware datetime;认不出 → None,绝不编时间。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse(payload: str) -> list[RawItem]:
    """纯函数解析(罐装 JSON 可无网络单测)。

    - 顶层不是数组 → ValueError(fail-loud,由 fetch 流水线按单源失败记录,不拖垮整批);
    - 单条缺 paper.id / paper.title → 跳过该条,不整批报废。
    """
    data = json.loads(payload)  # 畸形 JSON → JSONDecodeError 冒泡,单源 fail-loud
    if not isinstance(data, list):
        raise ValueError(f"daily_papers 响应非数组(拿到 {type(data).__name__}),API 结构变了?")
    items: list[RawItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        paper = entry.get("paper") or {}
        if not isinstance(paper, dict):
            continue
        paper_id = str(paper.get("id") or "").strip()
        title = str(paper.get("title") or "").strip()
        if not paper_id or not title:
            continue
        published = _parse_dt(paper.get("publishedAt"))
        if published is None:
            continue  # 🔴 无一手论文时间整条丢弃:None 会被流水线兜底成抓取时间=freshness 造假
        summary = str(paper.get("summary") or "").strip() or None
        items.append(
            RawItem(
                url=_PAPER_URL.format(paper_id),
                title=title,
                content=summary,  # 摘要当正文,给判官/summarize 用
                published_at=published,  # 一手论文时间(paper.publishedAt)
                facts={"hf": {"upvotes": paper.get("upvotes"), "paper_id": paper_id}},
            )
        )
    return items


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    resp = await client.get(source.url)
    if resp.not_modified:
        return []
    return _parse(resp.text)
