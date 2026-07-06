"""SSRF 防护:抓取前校验 URL。

挡:非 http/https scheme、缺主机名、目标(或解析后)落在内网/环回/链路本地/保留段。
file:// 由 sources/file.py 单独做"项目根内"越权校验,不走这里。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}


class SSRFError(ValueError):
    """URL 被 SSRF 策略拒绝。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_http_url_allowed(url: str, *, resolve: bool = True) -> None:
    """放行返回 None;拒绝抛 SSRFError(失败要冒泡,不静默)。

    resolve=False 只校验 scheme/IP 字面量(单测用,不触发 DNS)。
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise SSRFError(f"scheme 不允许:{parts.scheme!r}(仅 http/https)")
    host = parts.hostname
    if not host:
        raise SSRFError(f"URL 缺少主机名:{url!r}")

    # 主机本身就是 IP 字面量
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_blocked_ip(ip):
            raise SSRFError(f"目标 IP 属内网/保留段:{host}")
        return

    if not resolve:
        return

    # 主机名 → 解析全部 A/AAAA,任一落内网即拒(挡 DNS rebinding 到内网)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFError(f"无法解析主机 {host}:{exc}") from exc
    for info in infos:
        addr = info[4][0].split("%")[0]  # 去掉 IPv6 scope id
        resolved = ipaddress.ip_address(addr)
        if _is_blocked_ip(resolved):
            raise SSRFError(f"主机 {host} 解析到内网/保留地址 {addr}")
