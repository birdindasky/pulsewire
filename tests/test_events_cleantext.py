"""events.cleantext:正文清洗(Phase 1a 决定性,固化口径)。"""
from __future__ import annotations

from pulsewire.events.cleantext import body_chars_beyond_title, clean_text

# 真实空壳源:google-news 跳转卡(content 只是 <a href>标题</a> + 源名),清洗后只剩标题原文。
# 2026-06-18 实锤:这条被排进 AI 板块前五 rank=4(int_124a02426eee__shadow),summarize 只拿标题写出 10 倍错数字。
_SHELL_CONTENT = (
    '<a href="https://news.google.com/rss/articles/'
    'CBMi4wFBVV95cUxPYWVNY2liZUNTSWFHbmp4YXMtRnNNS3FqWm9JUFBE" target="_blank">'
    'Musk’s SpaceX Splurges $60 Billion to Acquire Cursor, Completing the xAI Ecosystem; '
    'Can It Break Through the Duopoly Era of Anthropic and OpenAI?</a>'
    '&nbsp;&nbsp;<font color="#6f6f6f">TradingKey</font>'
)
_SHELL_TITLE = (
    "Musk’s SpaceX Splurges $60 Billion to Acquire Cursor, Completing the xAI Ecosystem; "
    "Can It Break Through the Duopoly Era of Anthropic and OpenAI? - TradingKey"
)


def test_strips_html_tags():
    assert clean_text("<figure><div><img src='x'/>真正的正文</div></figure>") == "真正的正文"


def test_strips_base64_and_url():
    # P077 那类:google-news RSS 跳转 = 纯 base64,清洗后应几乎为空
    junk = "<a href='x'>https://news.google.com/rss/articles/CBMikAJBVV95cUxPem5KZjVuaXQ1aEdlZnR1ZndH</a>"
    out = clean_text(junk)
    assert "CBMikAJBVV95" not in out  # base64 去掉
    assert "http" not in out  # URL 去掉


def test_unescapes_entities():
    assert clean_text("AT&amp;T 与 Q&amp;A") == "AT&T 与 Q&A"


def test_collapses_whitespace():
    assert clean_text("a\n\n  b\t c") == "a b c"


def test_empty_and_none():
    assert clean_text(None) == ""
    assert clean_text("") == ""
    assert clean_text("   <p></p>  ") == ""


def test_keeps_real_text_signal():
    # P018 那类:HTML 图包裹真正文,清洗后保住可判事件的文字
    raw = "<p><img src='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'/></p>Tango 胰腺癌新药 NCT06625320 数据"
    out = clean_text(raw)
    assert "Tango" in out and "NCT06625320" in out
    assert "iVBORw0KGgo" not in out


def test_body_chars_beyond_title_shell_is_near_zero():
    """空壳源(google-news 跳转卡):清洗后只剩标题原文,真正文几乎为 0 字。"""
    # 注意:naive len(clean_text(content)) 会过 80(标题本身就长),正是旧护栏放它进榜的漏洞;
    # 剔掉标题词后才暴露"没有真正文"。
    assert len(clean_text(_SHELL_CONTENT)) > 80  # naive 口径会误判它"有料"
    assert body_chars_beyond_title(_SHELL_CONTENT, _SHELL_TITLE) < 24  # 真口径:几乎无真正文


def test_body_chars_beyond_title_real_article_well_above_floor():
    """真新闻(哪怕短):正文带标题之外的实质字符,远超阈值。"""
    title = "BBC: Israel launches fresh strikes on Lebanon despite Trump criticism"
    content = (
        'Speaking on Tuesday, Trump said Israel\'s PM Benjamin Netanyahu needed '
        '"to be more responsible with respect to Lebanon".'
    )
    assert body_chars_beyond_title(content, title) >= 24


def test_body_chars_beyond_title_empty_and_none():
    assert body_chars_beyond_title(None, "anything") == 0
    assert body_chars_beyond_title("", "anything") == 0
    # content 全是被剥掉的 URL/标签 → 真正文 0
    assert body_chars_beyond_title("<a href='https://x.com/y'></a>", "title") == 0
