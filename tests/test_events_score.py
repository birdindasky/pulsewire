"""events.score:事件打分纯函数(热度主轴 + 源族折叠 + 两道硬门)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulsewire.config.models import EventPoolCfg
from pulsewire.events import score as S


def test_source_family_folds_google_news_mirrors():
    assert S.source_family("geo-google-news-ru") == "google-news"
    assert S.source_family("ap-via-google-news") == "google-news"
    assert S.source_family("reuters") == "reuters"
    assert S.source_family("") == ""


def test_weighted_distinct_sources_collapses_mirrors():
    # 5 个 google-news 镜像 → 折叠成 1 个弱源(防"镜像冒充多源")
    mirrors = [(f"geo-google-news-{i}", 1.0) for i in range(5)]
    assert S.weighted_distinct_sources(mirrors) == 1.0
    # 真 3 个不同源(按权重求和)
    real = [("reuters", 1.0), ("cbc", 0.8), ("ap", 1.0)]
    assert abs(S.weighted_distinct_sources(real) - 2.8) < 1e-9
    # 镜像 + 真源混合
    mixed = mirrors + real
    assert abs(S.weighted_distinct_sources(mixed) - 3.8) < 1e-9  # 1(镜像族) + 2.8


def test_velocity_clamp():
    assert S.velocity(6, 6, v_max=1.0) == 1.0
    assert S.velocity(12, 6, v_max=1.0) == 1.0   # 超 v_max 截顶
    assert S.velocity(3, 6, v_max=1.0) == 0.5
    assert S.velocity(5, 0, v_max=1.0) == 0.0    # 基窗 0 不爆


def test_heat_score_multisource_beats_singleton_with_floor():
    # 多源齐报热度 > 单源+量级地板(地板不该盖过真多源,DESIGN §5.2)
    multi = S.heat_score(S.weighted_distinct_sources([(f"s{i}", 1.0) for i in range(8)]), 0.5)
    floor_single = S.heat_score(S.weighted_distinct_sources([("labs-openai", 1.0)]), 0.0, magnitude_bonus=1.1)
    assert multi > floor_single
    # 但量级地板让单源也能上桌(>0)
    assert floor_single > S.heat_score(0.0, 0.0)


def test_relevance_gate_magnitude_exemption():
    cfg = EventPoolCfg()  # τ_rel=0.5, τ_floor=0.3
    assert S.passes_relevance_gate(0.4, is_magnitude=False, cfg=cfg) is False  # 普通条 0.4<0.5 挡
    assert S.passes_relevance_gate(0.4, is_magnitude=True, cfg=cfg) is True   # 量级首发 0.4≥0.3 放
    assert S.passes_relevance_gate(0.2, is_magnitude=True, cfg=cfg) is False  # 但仍需 ≥τ_floor


def test_freshness_hard_window():
    cfg = EventPoolCfg()  # 48h
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    fresh = now - timedelta(hours=10)
    stale = now - timedelta(hours=72)
    assert S.passes_freshness_window(fresh, now, cfg=cfg) is True
    assert S.passes_freshness_window(stale, now, cfg=cfg) is False
    assert S.passes_freshness_window(stale, now, cfg=cfg, has_fresh_update=True) is True  # 今日有进展放行
    assert S.passes_freshness_window(None, now, cfg=cfg) is False  # 无日期保守不放行


def test_freshness_per_domain_window_override():
    """按域覆盖窗口(见 docs/DESIGN.md §2.7):AI 板 144h 收下 100h 老闻,bio/geo 全局 48h 仍踢。"""
    cfg = EventPoolCfg()  # 全局 48h
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    age_100h = now - timedelta(hours=100)
    # 全局 48h:100h 老闻出局
    assert S.passes_freshness_window(age_100h, now, cfg=cfg) is False
    assert S.passes_freshness_window(age_100h, now, cfg=cfg, window_hours=None) is False  # None=退回全局
    # AI 放宽 144h:100h 收下
    assert S.passes_freshness_window(age_100h, now, cfg=cfg, window_hours=144) is True
    # 放宽窗也有边界:150h > 144h 仍出局
    age_150h = now - timedelta(hours=150)
    assert S.passes_freshness_window(age_150h, now, cfg=cfg, window_hours=144) is False
    # 无 peak_at:即便放宽也保守不放行
    assert S.passes_freshness_window(None, now, cfg=cfg, window_hours=144) is False
    # has_fresh_update 仍优先(今日有进展)
    assert S.passes_freshness_window(age_150h, now, cfg=cfg, window_hours=144,
                                     has_fresh_update=True) is True


def test_is_magnitude_entity():
    wl = ["OpenAI", "Anthropic", "英伟达"]
    assert S.is_magnitude_entity("OpenAI 发布 GPT-6", wl) is True
    assert S.is_magnitude_entity("某小公司融资", wl) is False
