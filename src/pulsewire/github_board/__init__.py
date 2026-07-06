"""github_board — GitHub 开源热榜:独立选品(按 stars,非语义排序)→ 复用 summarize → 出榜。

主新闻日报按"重要性"排,会把开源项目挤掉;热榜单独成板:取带 stars 的 AI repo、去重、
按 stars 取 top N,复用 summarize/verify(数字回源)产出"是什么 + 多少 stars",出一张速读榜 PNG + App tab。
真·增长速度排序需跨天 star 增量(item_timeline),v2。
"""

from __future__ import annotations

from .engine import GH_INTEREST, GH_INTEREST_KEY, run_github_board

__all__ = ["GH_INTEREST", "GH_INTEREST_KEY", "run_github_board"]
