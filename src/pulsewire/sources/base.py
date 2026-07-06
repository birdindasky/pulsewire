"""适配器基础:统一抓取产物 RawItem 与适配器签名。

适配器 = `async def collect(source, client) -> list[RawItem]`:
给定一个注册表里的 Source 和共享 FetchClient,产出已解析的条目。
解析逻辑各自拆出 `_parse(text)` 纯函数,便于无网络单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient


@dataclass(slots=True)
class RawItem:
    """适配器产出的一条原始条目(尚未落库)。"""

    url: str
    title: str
    content: str | None = None
    published_at: datetime | None = None  # 缺失由 fetch 流水线兜底为抓取时间
    facts: dict | None = None  # 源自带的数字(HN points / GitHub stars 等),阶段 4 富化再挂 source_id


Adapter = Callable[["Source", "FetchClient"], Awaitable[list[RawItem]]]
