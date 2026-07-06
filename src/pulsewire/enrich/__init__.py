"""enrich — 并发富化(按源路由):HN points/评论、GitHub stars/forks、正文(trafilatura)。

每个富化结果挂 value + source_id(来自入库事实,**不让模型编**)。[阶段 4]
- engine : 从入库事实派生带 source_id 的结构化事实 + 可选正文全文。
"""

from __future__ import annotations

from .engine import extract_facts, fetch_fulltext, run_enrich

__all__ = ["extract_facts", "fetch_fulltext", "run_enrich"]
