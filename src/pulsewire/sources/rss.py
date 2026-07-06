"""RSS / Atom 适配器(feedparser 解析)。

仅按"取 link/title/摘要/发布时间"的行为规格实现。

2026-07 源升级 P1 三刀(exam_final 64 FIX 落地,详见 docs 判决书):
- 正文取 content:encoded 与 summary 里**更长**者:一批源(Substack 系/one-useful-thing/
  hard-fork/fox 等)全文明明在 feed 的 content:encoded 里,旧版只读 summary → body_rate 0。
- link 兜底 guid/enclosure:megaphone 系播客(no-priors/training-data)条目无 <link>,
  旧版整条丢弃 → 新集永远进不来(training-data 从未入库、no-priors 冻在 06-09 实锤)。
- 日期字符串兜底解析:Fierce 系 feed 用非 RFC822 日期『Jun 24, 2026 3:33pm』,feedparser
  解析不了 → 流水线回落抓取时间 = 旧闻冒充今天(date_suspect_rate=1.0 实锤)。
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import feedparser

from .base import RawItem

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient

# feedparser 失败后的兜底日期格式(逐个尝试;无时区按 UTC 记——Fierce 实际是美东,
# 按 UTC 会比真实时间早约 4-5 小时 = 只会更"旧"不会更"新",不产生新鲜度造假)
_FALLBACK_DATE_FORMATS = (
    "%b %d, %Y %I:%M%p",  # Fierce 系:'Jun 24, 2026 3:33pm'
    "%b %d, %Y",          # 同族纯日期变体
)


def _published(entry: dict) -> datetime | None:
    """从 entry 取发布时间(published 优先,退而求 updated),转 UTC。

    feedparser 解析不动的非标准格式(如 Fierce 'Jun 24, 2026 3:33pm')再按
    _FALLBACK_DATE_FORMATS 兜底;全失败返回 None(交上游按"无日期"处理,别编时间)。
    """
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    for key in ("published", "updated"):
        raw = (entry.get(key) or "").strip()
        if not raw:
            continue
        for fmt in _FALLBACK_DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _visible_len(s: str) -> int:
    """剥标签后的可见文本长度(考官加固:纯 <img src=base64…> 的 raw 很长但可见为零,
    按 raw 长度取长者会让图片标记赢过真文字摘要 → 空壳)。标签整体剥掉,空白折叠后计长。"""
    return len(re.sub(r"\s+", "", _TAG_RE.sub(" ", s)))


def _content(entry: dict) -> str | None:
    """条目正文:summary/description 与 content:encoded(feedparser 映射到 entry.content)
    取**可见文本最长**者——一批源把全文放 content:encoded、summary 只是一行摘要。"""
    candidates: list[str] = []
    summary = entry.get("summary") or entry.get("description")
    if summary:
        candidates.append(summary)
    for c in entry.get("content") or []:
        value = c.get("value") if isinstance(c, dict) else getattr(c, "value", None)
        if value:
            candidates.append(value)
    if not candidates:
        return None
    return max(candidates, key=_visible_len)


def _link(entry: dict) -> str:
    """条目链接:link → URL 形 guid → enclosure href 逐级兜底。

    megaphone 系播客 feed 条目没有 <link>(guid 是裸 UUID、音频在 enclosure),
    旧版直接丢弃整条 → 新集永远进不了库。enclosure URL 每集唯一,可当条目 URL 用。
    """
    link = (entry.get("link") or "").strip()
    if link:
        return link
    guid = (entry.get("id") or "").strip()
    if guid.startswith(("http://", "https://")):
        return guid
    for lnk in entry.get("links") or []:
        if isinstance(lnk, dict) and lnk.get("rel") == "enclosure" and lnk.get("href"):
            return str(lnk["href"]).strip()
    return ""


def _looks_like_html(text: str) -> bool:
    """响应像 HTML 页面而非 feed(codex②):Accept 含 text/html 后,拦截页/同意墙可能 200 返回 HTML,
    feedparser 解析出 0 条会被当"正常空 feed"静默吞掉 → 源无声死亡。嗅前 512 字符。"""
    head = text[:512].lstrip().lower()
    if head.startswith(("<?xml", "<rss", "<feed", "<rdf")):
        return False  # 合法 feed 头直接短路(考官 LOW:防空 feed 的 CDATA 里藏 <html 误报)
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head[:256]


def _parse(text: str) -> list[RawItem]:
    feed = feedparser.parse(text)
    items: list[RawItem] = []
    for entry in feed.entries:
        link = _link(entry)
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue  # 没链接(含全部兜底)或没标题的条目无法去重/落库,跳过
        items.append(
            RawItem(url=link, title=title, content=_content(entry), published_at=_published(entry))
        )
    return items


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    headers = {"User-Agent": source.user_agent} if source.user_agent else None
    resp = await client.get(source.url, headers=headers)
    if resp.not_modified:
        return []
    items = _parse(resp.text)
    if not items and _looks_like_html(resp.text):
        # 0 条 + 响应是 HTML = 八成被拦截页/同意墙顶了,按源失败冒泡(进 failed_ids 可观测),
        # 绝不静默记成"正常抓到 0 条"。真空 feed(合法 XML 0 条)不受影响。
        raise ValueError(f"feed 返回 HTML 而非 XML(疑似拦截页):{source.url}")
    return items
