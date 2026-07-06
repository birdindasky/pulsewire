"""事件聚类的正文清洗 —— Phase 1a 决定性发现固化(回应 codex M2)。

为什么:很多源的原始 `Item.content` 是 RSS 的 <img>/base64/google-news 跳转 URL = 噪声,
判官读到的是空气 → 同事件 recall 卡死(脏正文 0.849)。换干净正文后未调过的判官直接
precision 1.0/recall 0.868 过线(`calibration/RESULTS.md` 轮5a)。

**本函数即 Phase 1a 验过的那版清洗口径(去 HTML 标签/裸 URL/base64 长串 + 实体反转义 + 折叠空白),
固化、勿改**——改口径等于偏离已验证配置,须重过 Phase 1a 校准门。聚类/判官的正文截断(500 字符)
由调用方施加,不在此函数。
"""
from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_B64 = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")  # base64 长串(图片/跳转编码)
_WS = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)


def clean_text(raw: str | None) -> str:
    """清洗正文供事件聚类 + 判官用;空/None → 空串。口径与 Phase 1a 校准一致。"""
    if not raw:
        return ""
    s = _TAG.sub(" ", raw)
    s = _URL.sub(" ", s)
    s = _B64.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def body_chars_beyond_title(content: str | None, title: str | None) -> int:
    """量「真文章正文」有多少字符 —— 清洗后正文里**不在标题中**的词的字符数。

    为什么不直接量 len(clean_text(content)):**空壳源**(如 google-news 跳转卡)的 content 只是一段
    `<a href=...>标题</a>` 锚点,clean_text 把链接抹掉后**只剩标题本身**(实测 TradingKey 那条 clean=156,
    但全是标题原文,真正文 0 字)——naive len>=80 放它进榜,正是把"10 倍错数字"喂给 summarize 的真因。
    把标题里出现过的词剔掉,剩下的才是真正抓到的正文;空壳剔完≈0,真新闻(哪怕短)仍远超阈值
    (影子跑实测:空壳 ≤2 字,最短真新闻 77 字,间隔极大,阈值不敏感)。content/title 都先走 clean_text。
    """
    body = clean_text(content)
    if not body:
        return 0
    title_words = {w.lower() for w in _WORD.findall(clean_text(title)) if len(w) > 1}
    return sum(len(m.group(0)) for m in _WORD.finditer(body)
               if m.group(0).lower() not in title_words)
