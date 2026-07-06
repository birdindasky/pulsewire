"""CLI 参数解析测试:rank 的 --domain 解析 + 领域解析(显式优先 / config 自动匹配 / 不匹配=None)。"""

from __future__ import annotations

from pulsewire.config import get_settings
from pulsewire.run import _parse_rank_args, _resolve_rank_domain


def test_parse_rank_args_extracts_domain():
    interest, tags, limit, domain = _parse_rank_args(
        ["生物医疗", "--tags=bio,health", "--limit=5", "--domain=bio"]
    )
    assert interest == "生物医疗"
    assert tags == ["bio", "health"]
    assert limit == 5
    assert domain == "bio"


def test_parse_rank_args_domain_optional():
    interest, tags, limit, domain = _parse_rank_args(["AI 助手"])
    assert interest == "AI 助手"
    assert tags == [] and limit is None and domain is None


def test_resolve_rank_domain_explicit_wins():
    """显式 --domain 优先,哪怕兴趣能匹配到别的 config 领域也以显式为准。"""
    settings = get_settings()
    d = settings.run.domains[0]  # 通常 ai
    assert _resolve_rank_domain(settings, d.interest, list(d.tags), "geo") == "geo"


def test_resolve_rank_domain_auto_matches_config():
    """没给 --domain:兴趣+tags 精确匹配 config 领域 → 套用其 key(防手动单跑污染 rankings)。"""
    settings = get_settings()
    d = settings.run.domains[-1]  # 取最后一个领域(geo)
    assert _resolve_rank_domain(settings, d.interest, list(d.tags), None) == d.key


def test_resolve_rank_domain_no_match_is_none():
    """兴趣不属任何 config 领域 → None(不过滤,沿用旧行为)。"""
    settings = get_settings()
    assert _resolve_rank_domain(settings, "完全不相干的随手兴趣 xyz", [], None) is None
