"""events.engine 空壳源护栏:全成员无真文章正文的事件被排除出榜,有一条实质正文的保留。

2026-06-18 实锤:google-news 跳转卡(content 只是 <a href>标题</a>,真正文从没抓到)被排进 AI 板块
rank=4,summarize 只拿标题自由发挥写出 10 倍错数字。护栏量「正文剔掉标题词后的字符数」,空壳≈0、
真新闻远超阈值(影子跑实测空壳 ≤2 字、最短真新闻 77 字),只杀"压根没真正文"、不误杀"正文短但真实"。
"""
from __future__ import annotations

from pulsewire.config.models import EventPoolCfg
from pulsewire.events.engine import _event_has_real_body

# 空壳成员:clean 是 clean_text(content) 后只剩标题原文(google-news 跳转卡的真实形态);title 已 clean。
_SHELL_HEADLINE = (
    "Musk’s SpaceX Splurges $60 Billion to Acquire Cursor, Completing the xAI Ecosystem"
)


def _shell_member() -> dict:
    # 引擎里 m["clean"]=clean_text(content);空壳源清洗后正文 == 标题原文(无真正文)
    return {"clean": _SHELL_HEADLINE, "title": _SHELL_HEADLINE + " - TradingKey"}


def _real_member() -> dict:
    return {
        "clean": ('Speaking on Tuesday, Trump said Israel\'s PM Benjamin Netanyahu '
                  'needed "to be more responsible with respect to Lebanon".'),
        "title": "Israel launches fresh strikes on Lebanon despite Trump criticism",
    }


def test_all_shell_event_excluded():
    """全成员空壳(即便有多条镜像)→ 无真正文 → 排除出榜。"""
    cfg = EventPoolCfg()  # min_body_chars=24
    ev_members = [_shell_member(), _shell_member()]
    assert _event_has_real_body(ev_members, cfg.min_body_chars) is False


def test_event_with_one_real_body_kept():
    """混合事件:一条空壳 + 一条真正文 → 有料 → 保留(代表选取另由引擎挑有料那条)。"""
    cfg = EventPoolCfg()
    ev_members = [_shell_member(), _real_member()]
    assert _event_has_real_body(ev_members, cfg.min_body_chars) is True


def test_short_but_real_breaking_news_not_killed():
    """不误杀:正文短但真实的突发新闻(剔标题后仍有实质字符)→ 保留。"""
    cfg = EventPoolCfg()
    short = {"clean": "Britain’s Ministry of Defense said the Russian vessel appeared "
                      "to be trying to avoid a collision in the English Channel.",
             "title": "Russian Navy Ship Fired Warning Shots Near British Couple’s Sailboat"}
    assert _event_has_real_body([short], cfg.min_body_chars) is True


def test_guard_disabled_when_zero():
    """min_body_chars=0 关闭护栏:即便全空壳也判有料(护栏不生效)。"""
    assert _event_has_real_body([_shell_member()], 0) is True


# ----- 2026-07 P1:facts.fulltext 读侧桥(summary-only 源靠富化全文过护栏) ----- #
def test_member_clean_text_uses_fulltext_when_content_thin():
    """content 只是空壳(清洗后只剩标题词),facts.fulltext 有真全文 → 用全文,过得了护栏。"""
    from pulsewire.events.engine import member_clean_text

    cfg = EventPoolCfg()
    title = "DeepMind unveils new protein folding model"
    shell_content = f'<a href="https://news.example/x">{title}</a>'
    fulltext = ("The new model, described in a paper published today, improves accuracy on "
                "membrane proteins by a wide margin and was validated across nine benchmark sets, "
                "researchers said in the announcement.")
    clean = member_clean_text(shell_content, {"fulltext": {"text": fulltext, "chars": len(fulltext)}})
    member = {"clean": clean, "title": title}
    assert _event_has_real_body([member], cfg.min_body_chars) is True
    # 对照:没有全文时同一条是空壳,被护栏排除
    clean_no_ft = member_clean_text(shell_content, None)
    assert _event_has_real_body([{"clean": clean_no_ft, "title": title}], cfg.min_body_chars) is False


def test_member_clean_text_keeps_longer_content():
    """content 本身比全文有料 → 不降级(取更长者)。"""
    from pulsewire.events.engine import member_clean_text

    long_content = "Real body text with plenty of substance. " * 10
    out = member_clean_text(long_content, {"fulltext": {"text": "short", "chars": 5}})
    assert "plenty of substance" in out and len(out) > 100


def test_member_clean_text_tolerates_malformed_facts():
    """facts 形态异常(list / 缺键 / fulltext 非 dict)→ 安静回退 content,不炸。"""
    from pulsewire.events.engine import member_clean_text

    assert member_clean_text("body", ["not", "a", "dict"]) == "body"
    assert member_clean_text("body", {"fulltext": "not-a-dict"}) == "body"
    assert member_clean_text("body", {"other": 1}) == "body"
    assert member_clean_text(None, None) == ""
