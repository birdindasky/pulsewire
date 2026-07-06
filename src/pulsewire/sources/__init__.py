"""sources — 信源注册表 + 适配器(rss / hackernews / github / hf_papers / ossinsight / file)。

[阶段 2] 源适配器层:按各源行为规格实现。
对外:`get_adapter(source_type)` 取对应适配器协程;`RawItem` 为统一产物。
"""

from __future__ import annotations

from pulsewire.config.models import SourceType

from . import file_src, github, hackernews, hf_papers, ossinsight, rss
from .base import Adapter, RawItem

ADAPTERS: dict[SourceType, Adapter] = {
    SourceType.rss: rss.collect,
    SourceType.hackernews: hackernews.collect,
    SourceType.github: github.collect,
    SourceType.hf_papers: hf_papers.collect,
    SourceType.ossinsight: ossinsight.collect,
    SourceType.file: file_src.collect,
}


def get_adapter(source_type: SourceType) -> Adapter:
    """取源类型对应的适配器;未实现(reddit/youtube/html 留待后续)抛 NotImplementedError。"""
    try:
        return ADAPTERS[source_type]
    except KeyError as exc:
        raise NotImplementedError(f"阶段 2 未实现的源类型:{source_type}") from exc


__all__ = ["ADAPTERS", "Adapter", "RawItem", "get_adapter"]
