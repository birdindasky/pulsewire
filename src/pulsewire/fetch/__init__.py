"""fetch — 并发抓取:共享 httpx client + 中间件(重试/限速/ETag/SSRF防护/UA)+ 落库。

[阶段 2]
- client   : FetchClient(UA / 每主机限速 / 退避重试 / ETag 条件请求 / SSRF 拦截)
- ssrf     : 抓取前 URL 校验
- pipeline : 并发抓取所有启用源 → upsert_item(published_at 兜底)
"""

from __future__ import annotations

from .client import FetchClient, FetchResponse
from .pipeline import SourceResult, fetch_and_store
from .ssrf import SSRFError, assert_http_url_allowed

__all__ = [
    "FetchClient",
    "FetchResponse",
    "SourceResult",
    "fetch_and_store",
    "SSRFError",
    "assert_http_url_allowed",
]
