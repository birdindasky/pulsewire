"""file:// 适配器:读本地 feed(离线测试 / 本地巡检)。

SSRF 越权防护:只允许读取项目根目录内的文件(resolve 后做包含判断),
挡 file:///etc/passwd 与 ../ 穿越。内容按 RSS/Atom 解析。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pulsewire.config import PROJECT_ROOT

from .base import RawItem
from .rss import _parse

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient


class FilePathError(ValueError):
    """file:// 路径非法或越权。"""


def resolve_file_url(url: str) -> Path:
    """把 file:// URL 解析成项目根内的真实路径;越权/不存在则抛错。

    支持:file:///abs、file://localhost/abs、file://./rel、file://rel(相对项目根)。
    """
    if not url.startswith("file://"):
        raise FilePathError(f"file 适配器只接受 file:// :{url!r}")
    rest = url[len("file://") :]
    if rest.startswith("localhost/"):
        rest = rest[len("localhost") :]

    if rest.startswith("/"):
        path = Path(rest)  # 绝对路径
    else:
        rest = rest[2:] if rest.startswith("./") else rest
        path = PROJECT_ROOT / rest  # 相对项目根

    resolved = path.resolve()
    root = PROJECT_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise FilePathError(f"file:// 越权:{resolved} 不在项目根 {root} 内")
    if not resolved.is_file():
        raise FilePathError(f"file:// 文件不存在:{resolved}")
    return resolved


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    path = resolve_file_url(source.url)
    return _parse(path.read_text(encoding="utf-8"))
