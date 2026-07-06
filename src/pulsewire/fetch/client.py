"""共享异步抓取客户端 + 中间件:UA / 每主机限速 / 重试 / ETag 条件请求 / SSRF 防护。

实现"带退避重试、每主机限速、条件请求、SSRF 拦截"的共享抓取中间件。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .ssrf import assert_http_url_allowed

if TYPE_CHECKING:
    from pulsewire.config import Settings

# 这些状态码当作"瞬时故障",触发退避重试
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableHTTP(Exception):
    """可重试的 HTTP 响应(5xx / 429)。"""


@dataclass(slots=True)
class FetchResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str]
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


class FetchClient:
    """对一批源复用一个 httpx 客户端;每主机一个限速器;按配置重试。

    用法:`async with FetchClient(settings) as client: await client.get(url)`
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        f = settings.fetch
        self._retry_max = f.retry_max
        self._rate = f.rate_limit_per_host
        self._slow_hosts = f.slow_hosts
        self._client = client or httpx.AsyncClient(
            timeout=f.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": f.user_agent,
                # 反爬第二道(2026-07 P1):部分 CDN(腾讯云 NWS 等)对"只有 UA 没有浏览器味
                # 请求头"的客户端仍拦(qbitai 403 实锤是裸 UA;带头后 200)。跟浏览器对齐的
                # Accept/Accept-Language 对正常源无副作用,对挑剔源是通行证。
                "Accept": "application/rss+xml,application/atom+xml,application/xml;q=0.9,text/html;q=0.8,*/*;q=0.7",
                "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6,zh;q=0.5",
            },
        )
        self._owns_client = client is None
        self._limiters: dict[str, AsyncLimiter] = {}
        # ETag/Last-Modified 缓存(进程内):同一 URL 再抓走条件请求,命中 304 跳过
        self._cache: dict[str, tuple[str | None, str | None]] = {}

    async def __aenter__(self) -> FetchClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _host_rate(self, host: str) -> float:
        """该主机限速(次/秒):slow_hosts 后缀匹配命中用慢速,否则全局 rate_limit_per_host。"""
        for suffix, rate in self._slow_hosts.items():
            if host == suffix or host.endswith("." + suffix):
                return rate
        return self._rate

    def _limiter(self, host: str) -> AsyncLimiter:
        lim = self._limiters.get(host)
        if lim is None:
            rate = self._host_rate(host)
            # AsyncLimiter 桶容量须 ≥1:慢速(rate<1)等价表达为「1 个 / (1/rate) 秒」(如 0.5 → 每 2 秒 1 个);
            # 否则容量 0.5<1 永远 acquire 不到 1 个 token(报 Can't acquire more than the maximum capacity)。
            lim = AsyncLimiter(rate, 1) if rate >= 1 else AsyncLimiter(1, 1.0 / rate)
            self._limiters[host] = lim
        return lim

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_conditional: bool = True,
    ) -> FetchResponse:
        # SSRF:抓取前拦截内网/非法 scheme(getaddrinfo 是阻塞调用,丢线程池)
        await asyncio.to_thread(assert_http_url_allowed, url)

        host = httpx.URL(url).host or ""
        req_headers = dict(headers or {})
        if use_conditional and url in self._cache:
            etag, last_modified = self._cache[url]
            if etag:
                req_headers["If-None-Match"] = etag
            if last_modified:
                req_headers["If-Modified-Since"] = last_modified

        async def _do() -> httpx.Response:
            async with self._limiter(host):
                resp = await self._client.get(url, headers=req_headers)
            if resp.status_code in _RETRYABLE_STATUS:
                raise RetryableHTTP(f"{resp.status_code} {url}")
            return resp

        resp: httpx.Response | None = None
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._retry_max + 1),
            wait=wait_exponential(multiplier=0.5, max=8),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.TimeoutException, RetryableHTTP)
            ),
            reraise=True,  # 重试耗尽 → 原始异常冒泡,不静默吞
        ):
            with attempt:
                resp = await _do()
        assert resp is not None

        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        if resp.status_code == 304:
            return FetchResponse(
                url=url,
                status=304,
                text="",
                headers=dict(resp.headers),
                not_modified=True,
                etag=etag,
                last_modified=last_modified,
            )
        if etag or last_modified:
            self._cache[url] = (etag, last_modified)
        resp.raise_for_status()  # 非 2xx(且不可重试,如 4xx)→ 冒泡
        return FetchResponse(
            url=url,
            status=resp.status_code,
            text=resp.text,
            headers=dict(resp.headers),
            etag=etag,
            last_modified=last_modified,
        )
