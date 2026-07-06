"""dedup — 三级去重 + 跨源同事件合并(归簇)。

URL规范化(入库已挡)→ 内容指纹 → embedding 语义近重复(pgvector)。
宁漏勿误合并;阈值用评测集校准。[阶段 3]
- embedding : 向量后端(本地 fastembed / Jina API)
- engine    : 三级去重,归簇到 clusters(source_count 为大事判定)
"""

from __future__ import annotations

from .embedding import Embedder, LocalEmbedder, get_embedder
from .engine import run_dedup

__all__ = ["Embedder", "LocalEmbedder", "get_embedder", "run_dedup"]
