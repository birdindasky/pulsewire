"""已剪记忆下游:③增量写稿提示词行 + ②「追·第N天」章(PNG 模板/飞书卡)。"""
from __future__ import annotations

from pulsewire.deliver.feishu_card import _links_md
from pulsewire.render.templates import build_html, build_midview_html, build_overview_html
from pulsewire.summarize.engine import _build_user_prompt


# ---------- ③ 增量写稿:tracked 条目带前情行,其余零变化 ----------

def test_prompt_tracked_injects_prev_context():
    ordered = [("it1", "标题一", "正文一"), ("it2", "标题二", "正文二")]
    tracked = {"it1": {"days_prior": 2, "last_date": "2026-07-04",
                       "prev_text": "昨天报过:X 公司发布 Y"}}
    p = _build_user_prompt(ordered, {}, tracked=tracked)
    assert "连续报道(此前已报 2 天,最近 2026-07-04)" in p
    assert "昨天报过:X 公司发布 Y" in p
    assert "只写自上次报道以来的" in p
    # 只注给 it1:切出 it2 段落核实无前情行
    seg2 = p.split("[2] item_id=it2")[1]
    assert "连续报道" not in seg2


def test_prompt_untracked_unchanged():
    ordered = [("it1", "标题一", None)]
    assert "连续报道" not in _build_user_prompt(ordered, {}, tracked=None)
    assert "连续报道" not in _build_user_prompt(ordered, {}, tracked={})


def test_prompt_prev_text_truncated():
    tracked = {"it1": {"days_prior": 1, "last_date": "2026-07-04", "prev_text": "长" * 900}}
    p = _build_user_prompt([("it1", "t", None)], {}, tracked=tracked)
    assert "长" * 300 in p and "长" * 301 not in p  # 前情截 300,别把旧稿全文塞回提示词


# ---------- ② PNG 模板:tracking_days 盖章,缺省不盖 ----------

def _item(**kw) -> dict:
    base = {"headline": "标题", "tldr": "速读", "insight": "批注",
            "source": "源", "url": "https://x", "needs_review": False}
    base.update(kw)
    return base


def test_detail_template_stamps_tracked():
    html = build_html(title="t", date_display="d", category="AI", digest="",
                      items=[_item(tracking_days=3)], footer_info="f", width=900)
    assert "追 · 第3天" in html and "trkstamp" in html


def test_templates_no_stamp_by_default():
    kw = dict(title="t", date_display="d", category="AI",
              items=[_item()], footer_info="f", width=900)
    # 注意断章的**渲染文案**而非 CSS 类名/CSS 注释(.trkstamp 与「追·第N天」字样在共享样式表里恒在)
    assert "追 · 第" not in build_html(digest="", **kw)
    assert "〔追·第" not in build_overview_html(**kw)
    assert "〔追·第" not in build_midview_html(**kw)


def test_overview_midview_stamp():
    kw = dict(title="t", date_display="d", category="AI",
              items=[_item(tracking_days=2)], footer_info="f", width=900)
    assert "〔追·第2天〕" in build_overview_html(**kw)
    assert "〔追·第2天〕" in build_midview_html(**kw)


# ---------- ② 飞书卡:链接行小标 ----------

def test_feishu_links_track_badge():
    items = [
        {"headline": "在追的", "url": "https://a", "tracking_days": 4},
        {"headline": "新鲜的", "url": "https://b"},
    ]
    md = _links_md(items)
    assert "`追·第4天`" in md
    assert md.count("追·第") == 1  # 只标在追那条
