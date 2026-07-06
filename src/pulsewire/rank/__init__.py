"""rank — 兴趣(自然语言)→标签→分类:embedding 召回粗筛 → LLM 精排
+ 新鲜度门 + 各类/老项限额。[阶段 4]

- engine : 召回 → 规则粗排 → 精排 → 新鲜度门 → 限额,落库 rankings。
- rerank : LLM 精排后端(DeepSeek / litellm)。
"""

from __future__ import annotations

from .engine import (
    Candidate,
    apply_quotas,
    filter_candidates_by_domain,
    interest_key,
    passes_freshness,
    rule_score,
    run_rank,
)

__all__ = [
    "Candidate",
    "apply_quotas",
    "filter_candidates_by_domain",
    "interest_key",
    "passes_freshness",
    "rule_score",
    "run_rank",
]
