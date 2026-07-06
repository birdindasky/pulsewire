"""transcript — 精排后对入选条目抓正文/网页逐字稿,喂给总结提质。[阶段 7+]

便宜路(v1,无需 ASR / 无新依赖):
- 网页正文/逐字稿:trafilatura 抓 item.url(复用 enrich.fetch_fulltext)。
  覆盖 Lex Fridman / Dwarkesh 等官网带全文字稿的访谈,以及所有博客/资讯/论文条目。
- 只对 **精排入选**的 ~final_limit 条抓(不是召回的全部),控网络与 token。
- 幂等:已存 facts.fulltext 的跳过;失败 best-effort 不拖垮整批。

未覆盖(留 v2):YouTube 自动字幕(需 youtube-transcript-api 依赖)、纯音频播客 ASR 转写。
"""

from __future__ import annotations

from .engine import run_transcript

__all__ = ["run_transcript"]
