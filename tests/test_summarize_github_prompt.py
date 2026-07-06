"""GitHub 项目写稿口径(2026-06-25:治老项目被写成今日新发布,用户选诚实改写文案)。

_build_user_prompt 对 github_ids 里的条目注入"热门 repo 非今日新发布"专属口径,别的条目不碰。
"""
from __future__ import annotations

from pulsewire.summarize.engine import _build_user_prompt

_HINT = "不是今日新发布"


def test_hint_added_for_github_item():
    ordered = [("gh1", "owner/repo-x", "一个开源库")]
    p = _build_user_prompt(ordered, {}, github_ids={"gh1"})
    assert _HINT in p
    assert "严禁写成『发布" in p


def test_hint_only_for_github_items_not_news():
    ordered = [("gh1", "owner/repo-x", "开源库"), ("news1", "某公司今日动态", "正文")]
    p = _build_user_prompt(ordered, {}, github_ids={"gh1"})
    assert p.count(_HINT) == 1  # 只 gh1 一条,新闻条目不加


def test_no_hint_when_no_github_ids():
    ordered = [("a", "标题", "正文")]
    assert _HINT not in _build_user_prompt(ordered, {}, github_ids=None)
    assert _HINT not in _build_user_prompt(ordered, {}, github_ids=set())
