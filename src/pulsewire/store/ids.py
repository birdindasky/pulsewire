"""确定性 ID 生成(来自入库事实,绝不让模型生成)。

- item_id    = sha256(规范化URL + 内容指纹)[:32]
- cluster_id = 由簇内首条派生(clt_ + item_id[:16]),跨天稳定
- source_id  = item_id:fact_type:field[:序号]  ——数字回源对账用的来源指针

已锁规范见 docs/ARCHITECTURE.md。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 跟踪参数:规范化时剥掉,避免同一文章因 utm 等参数被当成不同条目
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_")
_TRACKING_KEYS = {
    "fbclid", "gclid", "gbraid", "wbraid", "dclid", "msclkid",
    "ref", "ref_src", "ref_url", "source", "spm", "scm",
    "mc_cid", "mc_eid", "igshid", "yclid", "_hsenc", "_hsmi",
}
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_WS_RE = re.compile(r"\s+")


def normalize_url(url: str) -> str:
    """规范化 URL:小写 scheme/host、去默认端口、剥跟踪参数、排序 query、去 fragment、去尾斜杠。"""
    url = (url or "").strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port and not (
        scheme in _DEFAULT_PORTS and str(parts.port) == _DEFAULT_PORTS[scheme]
    ):
        netloc = f"{netloc}:{parts.port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS
        and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def content_fingerprint(title: str, content: str = "") -> str:
    """内容指纹:标题+正文归一化(折叠空白、小写、去首尾)后取 sha256。"""
    text = f"{title or ''}\n{content or ''}"
    text = _WS_RE.sub(" ", text).strip().lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_item_id(url: str, title: str, content: str = "") -> str:
    """item_id = sha256(规范化URL + 内容指纹)[:32]。确定性、来自入库事实。"""
    norm = normalize_url(url)
    fp = content_fingerprint(title, content)
    digest = hashlib.sha256(f"{norm}\x1f{fp}".encode("utf-8")).hexdigest()
    return digest[:32]


def make_cluster_id(first_item_id: str) -> str:
    """cluster_id 由簇内首条派生,跨天稳定。"""
    return f"clt_{first_item_id[:16]}"


def make_source_id(item_id: str, fact_type: str, field: str, seq: int | None = None) -> str:
    """source_id = item_id:fact_type:field[:序号](数字回源对账的来源指针)。"""
    parts = [item_id, fact_type, field]
    if seq is not None:
        parts.append(str(seq))
    return ":".join(parts)
