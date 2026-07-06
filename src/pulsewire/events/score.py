"""events.score —— 事件打分(DESIGN §5):两道硬门(相关性闸 + 新鲜度硬窗)+ 热度主轴。

heat = log1p(加权不同源族) * (1 + clamp(velocity,0,V_max)) + magnitude_floor_bonus
**源族折叠**:google-news/聚合镜像折叠成一个弱源,防"5 个镜像冒充 5 个源"(DESIGN §5.2)。
全纯函数(无 LLM/DB,可测);参数来自 EventPoolCfg(可 A/B 调,用户签字锁)。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pulsewire.config.models import EventPoolCfg

_AGG_MARKERS = ("google-news", "googlenews", "google_news", "google news")


def source_family(source: str | None) -> str:
    """把源归到"源族"(聚合镜像折叠成一个弱源);空→空串。"""
    s = (source or "").strip().lower()
    if not s:
        return ""
    if any(m in s for m in _AGG_MARKERS):
        return "google-news"  # 整个 google-news 族折叠成一个弱源
    return s


def weighted_distinct_sources(sources_and_weights: list[tuple[str, float]]) -> float:
    """Σ over 不同源族 的权重;同族(镜像)只算一次,取族内最大权重(代表性源)。"""
    by_family: dict[str, float] = {}
    for src, w in sources_and_weights:
        fam = source_family(src)
        if not fam:
            continue
        by_family[fam] = max(by_family.get(fam, 0.0), float(w))
    return sum(by_family.values())


def velocity(recent_sources: int, base_sources: int, *, v_max: float) -> float:
    """加速度 = 近窗不同源 / 基窗不同源,clamp 到 [0, v_max];基窗 0 → 0。"""
    if base_sources <= 0:
        return 0.0
    return max(0.0, min(recent_sources / base_sources, v_max))


def heat_score(weighted_sources: float, vel: float, *, magnitude_bonus: float = 0.0) -> float:
    """热度主轴。多源齐报为主、加速度加成、量级地板兜底(都有上限,真多源仍压得过地板)。"""
    return math.log1p(max(0.0, weighted_sources)) * (1.0 + max(0.0, vel)) + magnitude_bonus


def is_magnitude_entity(text: str | None, whitelist: list[str]) -> bool:
    """事件是否涉及白名单实体(大厂/官方机构)→ 可豁免相关性闸到 τ_floor。"""
    t = (text or "").lower()
    return any(w.strip().lower() in t for w in whitelist if w.strip())


def passes_relevance_gate(relevance: float, *, is_magnitude: bool, cfg: EventPoolCfg) -> bool:
    """相关性闸(非排序轴):够沾边才进板块兴趣圈。量级白名单实体降到 τ_floor 但仍需达标。"""
    floor = cfg.magnitude_floor_gate if is_magnitude else cfg.relevance_gate
    return relevance >= floor


def passes_freshness_window(
    peak_at: datetime | None, now: datetime, *, cfg: EventPoolCfg,
    has_fresh_update: bool = False, window_hours: int | None = None,
) -> bool:
    """新鲜度硬窗:主峰超 N 小时直接出局,除非今日有实质更新。无 peak_at=不放行(保守,堵旧引擎 NULL 放行隐患)。

    window_hours:按域覆盖窗口(小时);None=用 cfg.freshness_window_hours 全局值(零行为变化)。
    AI 板放宽到 144h 填满(真鲜 AI 货稀+源翻炒旧货),bio/geo 留全局 48h——见 docs/DESIGN.md §2.7。
    """
    if has_fresh_update:
        return True
    if peak_at is None:
        return False
    win = window_hours if window_hours is not None else cfg.freshness_window_hours
    age_h = (now - peak_at).total_seconds() / 3600.0
    return age_h <= win
