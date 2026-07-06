"""GitHub 适配器(Search Repositories API:stars / forks)。

源 url 用 GitHub 检索端点;有 github_token(env)则带上提额度。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pulsewire.config import get_settings

from .base import RawItem

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient

# 滚动新鲜窗占位:URL 里写 created:>{created_since:60d},抓取时换成"今天−60天"的日期。
# 让 GitHub Search 的 created:> 是滚动窗(每天往前推),而非 sources.yaml 里写死的静态日期。
_SINCE_RE = re.compile(r"\{created_since:(\d+)d\}")


def _apply_rolling_window(url: str) -> str:
    def _repl(m: re.Match) -> str:
        days = int(m.group(1))
        return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    return _SINCE_RE.sub(_repl, url)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse(payload: str) -> list[RawItem]:
    data = json.loads(payload)
    items: list[RawItem] = []
    for repo in data.get("items", []):
        full_name = repo.get("full_name") or repo.get("name")
        html_url = repo.get("html_url")
        if not full_name or not html_url:
            continue
        published = _parse_dt(repo.get("pushed_at")) or _parse_dt(repo.get("created_at"))
        facts = {
            "github": {
                "stars": repo.get("stargazers_count"),
                "forks": repo.get("forks_count"),
                # created_at = 仓库创建日期(非 pushed_at);热榜按"星/天龄"算涨速代理需要它,
                # 老巨仓一次 commit 把 pushed_at 装新也骗不了年龄。
                "created_at": repo.get("created_at"),
            }
        }
        items.append(
            RawItem(
                url=html_url,
                title=full_name,
                content=repo.get("description"),
                published_at=published,
                facts=facts,
            )
        )
    return items


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    headers = {"Accept": "application/vnd.github+json"}
    token = get_settings().resolve_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.get(_apply_rolling_window(source.url), headers=headers)
    if resp.not_modified:
        return []
    return _parse(resp.text)
