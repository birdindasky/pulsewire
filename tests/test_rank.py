"""rank 测试:interest_key 稳定、新鲜度门、规则打分、限额(各类/单源/老项)、事件热度——纯函数,无库无 LLM。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulsewire.config import Source, get_settings
from pulsewire.rank import (
    Candidate,
    apply_quotas,
    filter_candidates_by_domain,
    interest_key,
    passes_freshness,
    rule_score,
)
from pulsewire.rank.heat import compute_heat, pick_hot_reps

NOW = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)


def _cand(item_id, *, age_hours, category="tech", freshness=24, sim=0.8, weight=0.5,
          src_count=1, source=None):
    return Candidate(
        # source 缺省=每条目独立源(限额测试互不干扰;测单源限额时显式传同源)
        item_id=item_id, title=f"t-{item_id}", source=source or f"src-{item_id}",
        category=category,
        cluster_id=None, published_at=NOW - timedelta(hours=age_hours),
        source_count=src_count, source_weight=weight, freshness_hours=freshness,
        enriched=[], recall_sim=sim,
    )


def test_interest_key_stable_and_tag_insensitive_to_order():
    assert interest_key("AI 芯片", ["nvidia", "tpu"]) == interest_key("ai 芯片", ["TPU", "nvidia"])
    assert interest_key("AI 芯片", ["nvidia"]) != interest_key("AI 芯片", ["tpu"])


def test_freshness_gate():
    fresh = _cand("a", age_hours=10, freshness=24)
    stale = _cand("b", age_hours=48, freshness=24)
    assert passes_freshness(fresh, NOW) is True
    assert passes_freshness(stale, NOW) is False
    # 无发布时间(GitHub 等)→ 不武断判旧,放行;由 apply_quotas 老项限额兜底封顶
    no_date = _cand("c", age_hours=0)
    no_date.published_at = None
    assert passes_freshness(no_date, NOW) is True


def test_rule_score_prefers_fresh_relevant_multisource():
    cfg = get_settings().rank
    hot = _cand("hot", age_hours=1, sim=0.9, weight=0.9, src_count=3)
    cold = _cand("cold", age_hours=20, sim=0.3, weight=0.2, src_count=1)
    assert rule_score(hot, cfg, NOW, 3) > rule_score(cold, cfg, NOW, 3)
    # 分数落在合理区间(各因子 0~1,权重和=1)
    assert 0.0 <= rule_score(cold, cfg, NOW, 3) <= 1.0


def test_quota_per_category():
    cfg = get_settings().rank.model_copy(update={"final_limit": 10, "per_category_limit": 2})
    cands = []
    for i in range(5):
        c = _cand(f"tech-{i}", age_hours=1, category="tech")
        c.final_score = 1.0 - i * 0.01
        cands.append(c)
    for i in range(3):
        c = _cand(f"sci-{i}", age_hours=1, category="science")
        c.final_score = 0.5 - i * 0.01
        cands.append(c)
    kept = apply_quotas(cands, cfg, NOW)
    cats = [c.category for c in kept]
    assert cats.count("tech") == 2  # 各类限额生效
    assert cats.count("science") == 2


def test_quota_old_item_limit():
    cfg = get_settings().rank.model_copy(
        update={"final_limit": 10, "per_category_limit": 10,
                "old_item_age_hours": 168, "old_item_limit": 1}
    )
    cands = []
    for i in range(4):  # 4 个老项(超 168h)
        c = _cand(f"old-{i}", age_hours=200, category="tech")
        c.final_score = 0.9 - i * 0.01
        cands.append(c)
    for i in range(2):  # 2 个新项
        c = _cand(f"new-{i}", age_hours=1, category="tech")
        c.final_score = 0.4 - i * 0.01
        cands.append(c)
    kept = apply_quotas(cands, cfg, NOW)
    old_kept = [c for c in kept if (NOW - c.published_at).total_seconds() / 3600 > 168]
    assert len(old_kept) == 1  # 老项限额=1
    assert len(kept) == 3  # 1 老 + 2 新


def test_quota_final_limit():
    cfg = get_settings().rank.model_copy(
        update={"final_limit": 3, "per_category_limit": 100, "old_item_limit": 100}
    )
    cands = []
    for i in range(10):
        c = _cand(f"x-{i}", age_hours=1)
        c.final_score = 1.0 - i * 0.01
        cands.append(c)
    kept = apply_quotas(cands, cfg, NOW)
    assert len(kept) == 3
    assert [c.item_id for c in kept] == ["x-0", "x-1", "x-2"]  # 按终分降序


def test_quota_per_source():
    """单源限额:同一源刷屏(如官方博客一天 N 篇)只留前 per_source_limit 条。"""
    cfg = get_settings().rank.model_copy(
        update={"final_limit": 10, "per_category_limit": 100,
                "old_item_limit": 100, "per_source_limit": 2}
    )
    cands = []
    for i in range(5):
        c = _cand(f"blog-{i}", age_hours=1, source="vendor-blog")
        c.final_score = 1.0 - i * 0.01
        cands.append(c)
    c = _cand("other", age_hours=1, source="indie")
    c.final_score = 0.5
    cands.append(c)
    kept = apply_quotas(cands, cfg, NOW)
    assert sum(1 for c in kept if c.source == "vendor-blog") == 2  # 刷屏源被压到 2
    assert any(c.source == "indie" for c in kept)  # 没挤掉别人


def test_quota_same_event_dedup():
    """同事件去重:与已选条目向量相似≥阈值的候选跳过(镜像源/重复报道只出一条)。"""
    cfg = get_settings().rank.model_copy(
        update={"final_limit": 10, "per_category_limit": 100,
                "old_item_limit": 100, "per_source_limit": 100, "select_sim_dedup": 0.8}
    )
    cands = []
    for i, score in [("ipo-1", 0.9), ("ipo-2", 0.85), ("fable", 0.7)]:
        c = _cand(i, age_hours=1)
        c.final_score = score
        cands.append(c)
    vectors = {"ipo-1": [1.0, 0.0], "ipo-2": [1.0, 0.0], "fable": [0.0, 1.0]}
    kept = apply_quotas(cands, cfg, NOW, vectors=vectors)
    ids = [c.item_id for c in kept]
    assert "ipo-1" in ids and "fable" in ids
    assert "ipo-2" not in ids  # 同一事件第二条被压掉
    # 不传向量 → 不抑制(向后兼容)
    assert len(apply_quotas(cands, cfg, NOW)) == 3


def test_quota_same_event_dedup_production_threshold():
    """回归:守住生产 select_sim_dedup 落在验过的安全窗 (0.65, 0.71]。

    2026-06-23 Qwen3 重标:换嵌入模型后同事件边界上移(实测同事件余弦中位 ~0.85、不同事件 ~0.72),
    select_sim_dedup 操作点匹配重标为 0.74(2 独立考官签字)。合成值按 Qwen3 操作点重锚:
    ~0.80 的同事件重复必须折叠、~0.68 的不同事件必须保留 → 生产阈值须落在 (0.68, 0.80] 才安全。
    旧 jina 标定(2026-06-14,留作考古):AI「Anthropic 下架」重复卡余弦=0.71,窗口曾为 (0.65, 0.71]。
    有人把阈值调回过高(漏合并)或过低(误折叠)即失败。
    """
    import math

    cfg = get_settings().rank.model_copy(
        update={"final_limit": 10, "per_category_limit": 100,
                "old_item_limit": 100, "per_source_limit": 100}
    )  # 故意不覆盖 select_sim_dedup,用生产值

    def vec(c: float) -> list[float]:  # 与 [1,0] 夹角余弦=c 的单位向量
        return [c, math.sqrt(1 - c * c)]

    cands = []
    for iid, score in [("base", 0.9), ("dup", 0.85), ("diff", 0.8)]:
        c = _cand(iid, age_hours=1)
        c.final_score = score
        cands.append(c)
    vectors = {"base": [1.0, 0.0], "dup": vec(0.80), "diff": vec(0.68)}  # Qwen3 操作点(2026-06-23)
    kept = [c.item_id for c in apply_quotas(cands, cfg, NOW, vectors=vectors)]
    assert "base" in kept and "diff" in kept  # 不同事件(0.68)保留
    assert "dup" not in kept  # 同事件重复(0.80)被折叠 → 生产阈值须 ≤0.80 且 >0.68


# ---- 方案 B:中等相似带语义同事件复判(2026-06-16) ---- #

def _evt_cfg():
    return get_settings().rank.model_copy(
        update={"final_limit": 10, "per_category_limit": 100, "old_item_limit": 100,
                "per_source_limit": 100, "select_sim_dedup": 0.70, "event_dedup_min_sim": 0.55})


def _evt_cands(*specs):
    cands = []
    for iid, score in specs:
        c = _cand(iid, age_hours=1)
        c.final_score = score
        cands.append(c)
    return cands


def test_event_dedup_judge_folds_borderline_same_event():
    """中等相似带 [0.55, 0.70) 由 same_event 判官复判;判同事件→折叠。只问带内对,带外不问。"""
    import math

    def vec(c):
        return [c, math.sqrt(1 - c * c)]

    vectors = {"base": [1.0, 0.0], "angle": vec(0.62), "low": vec(0.50)}
    calls = []

    def same(a, b):
        calls.append((a.item_id, b.item_id))
        return True

    kept = [c.item_id for c in apply_quotas(
        _evt_cands(("base", 0.9), ("angle", 0.85), ("low", 0.8)),
        _evt_cfg(), NOW, vectors=vectors, same_event=same)]
    assert "base" in kept and "low" in kept   # 0.50 在带外,未判,保留
    assert "angle" not in kept                # 0.62 在带内 + 判官说同 → 折叠
    assert calls == [("angle", "base")]       # 只问了带内那一对


def test_event_dedup_judge_keeps_when_diff():
    """判官说不同事件 → 不折叠(保守:宁可留两条也别误合)。"""
    import math

    vectors = {"base": [1.0, 0.0], "angle": [0.62, math.sqrt(1 - 0.62 ** 2)]}
    kept = [c.item_id for c in apply_quotas(
        _evt_cands(("base", 0.9), ("angle", 0.85)),
        _evt_cfg(), NOW, vectors=vectors, same_event=lambda a, b: False)]
    assert "base" in kept and "angle" in kept


def test_event_dedup_high_sim_autofold_skips_judge():
    """高相似(≥select_sim)仍走词法自动折叠,判官只管中等带、不被问。"""
    import math

    vectors = {"base": [1.0, 0.0], "dup": [0.80, math.sqrt(1 - 0.80 ** 2)]}
    calls = []
    kept = [c.item_id for c in apply_quotas(
        _evt_cands(("base", 0.9), ("dup", 0.85)),
        _evt_cfg(), NOW, vectors=vectors,
        same_event=lambda a, b: (calls.append(1), False)[1])]
    assert "dup" not in kept   # 0.80 ≥ 0.70 自动折叠
    assert calls == []         # 判官没被问


def test_event_dedup_no_callback_is_lexical_only():
    """不传 same_event → 纯词法现状,中等带不折叠(向后兼容、无回归)。"""
    import math

    vectors = {"base": [1.0, 0.0], "angle": [0.62, math.sqrt(1 - 0.62 ** 2)]}
    kept = [c.item_id for c in apply_quotas(
        _evt_cands(("base", 0.9), ("angle", 0.85)), _evt_cfg(), NOW, vectors=vectors)]
    assert "base" in kept and "angle" in kept


def test_event_judge_failsafe_and_cost_cap(monkeypatch):
    """工厂:LLM 抛错→保守不折叠(不冒泡);成本闸到顶→不再调 LLM。"""
    from pulsewire.rank import event_judge as ej

    base = get_settings()
    settings = base.model_copy(update={"rank": base.rank.model_copy(update={"event_dedup_max_judges": 1})})
    n = []

    def boom(*a, **k):
        n.append(1)
        raise RuntimeError("llm down")

    monkeypatch.setattr(ej, "complete_json", boom)
    judge = ej.make_same_event_judge(settings)
    a, b, c = _cand("a", age_hours=1), _cand("b", age_hours=1), _cand("c", age_hours=1)
    assert judge(a, b) is False   # LLM 抛错 → fail-safe 不折叠
    assert judge(a, c) is False   # 成本闸到顶(max=1)→ 直接不折叠
    assert len(n) == 1            # 只真调了一次 LLM


def test_rule_score_heat_lifts_event_signal():
    """热度补大事信号:同事件被多源同报(heat 高)应比单源孤帖得分高。"""
    cfg = get_settings().rank
    flood = _cand("flood", age_hours=1)
    flood.heat = 10  # 10 个源在报相似内容
    lone = _cand("lone", age_hours=1)
    assert rule_score(flood, cfg, NOW, 3) > rule_score(lone, cfg, NOW, 3)


def test_rule_score_thread_boost_lifts_tracked():
    """持续关注反哺(step6-B):同条件下属于多天在追线(tracking)的候选规则分更高;thread=0 可关。"""
    cfg = get_settings().rank
    tracked = _cand("tracked", age_hours=1)
    tracked.tracking = True
    plain = _cand("plain", age_hours=1)
    assert rule_score(tracked, cfg, NOW, 3) > rule_score(plain, cfg, NOW, 3)
    # 关掉权重(thread=0)→ 不再加分(可回退到 step6-A 前行为)
    cfg0 = cfg.model_copy(update={"weights": cfg.weights.model_copy(update={"thread": 0.0})})
    assert rule_score(tracked, cfg0, NOW, 3) == rule_score(plain, cfg0, NOW, 3)


def test_compute_heat_counts_distinct_sources():
    """热度=邻域覆盖的不同源数;同源重复帖不抬热度。"""
    a, b = [1.0, 0.0], [0.0, 1.0]
    # 3 个源在报同一事件 + 1 个不相关孤帖
    heat = compute_heat([a, a, a, b], ["s1", "s2", "s3", "s4"], threshold=0.75)
    assert heat == [3, 3, 3, 1]
    # 同一个源连发 3 帖:热度仍是 1(不同源数才算热)
    heat2 = compute_heat([a, a, a], ["s1", "s1", "s1"], threshold=0.75)
    assert heat2 == [1, 1, 1]
    assert compute_heat([], [], threshold=0.75) == []


def test_filter_candidates_by_domain():
    """领域夹回:多领域同库时只留本领域候选,防热点/白名单直通把别领域头条混进来。"""
    def _src(sid, dom):
        return Source(id=sid, type="rss", url=f"https://x/{sid}", domain=dom)

    sources = {"ai-s": _src("ai-s", "ai"), "bio-s": _src("bio-s", "bio"),
               "geo-s": _src("geo-s", "geo")}
    cands = [_cand("a", age_hours=1, source="ai-s"),
             _cand("b", age_hours=1, source="bio-s"),
             _cand("c", age_hours=1, source="geo-s"),
             _cand("d", age_hours=1, source="unknown-s")]  # 不在注册表

    bio = filter_candidates_by_domain(cands, sources, "bio")
    assert [c.item_id for c in bio] == ["b"]               # 只留 bio;ai/geo 头条被夹掉
    ai = filter_candidates_by_domain(cands, sources, "ai")
    assert [c.item_id for c in ai] == ["a"]
    # 源不在注册表(领域未知)→ 丢
    assert all(c.item_id != "d" for c in filter_candidates_by_domain(cands, sources, "ai"))
    # domain=None → 不过滤(单兴趣 back-compat)
    assert len(filter_candidates_by_domain(cands, sources, None)) == 4


def test_pick_hot_reps_one_rep_per_event():
    """热点代表:同一事件只出一个代表(抑制邻居);不够热(<min_sources)不入选。"""
    a, b = [1.0, 0.0], [0.0, 1.0]
    ids = ["e1-a", "e1-b", "e1-c", "lone"]
    vecs = [a, a, a, b]
    heat = compute_heat(vecs, ["s1", "s2", "s3", "s4"], threshold=0.75)
    reps = pick_hot_reps(ids, vecs, heat, threshold=0.75, min_sources=3, top_n=5)
    assert len(reps) == 1  # 同事件 3 帖只出 1 个代表;孤帖热度 1 不够格
    assert reps[0].startswith("e1-")
    # top_n=0 → 关闭
    assert pick_hot_reps(ids, vecs, heat, threshold=0.75, min_sources=3, top_n=0) == []


# ---------- 三③ 内容领域分类(2026-06-15) ---------- #
def test_classify_parse_maps_unknown_domain_to_other():
    """LLM 分类解析:未知/缺失领域归 'other';只收有效候选 id。"""
    from pulsewire.rank.classify import _parse

    content = '{"items":[{"id":"a","domain":"ai"},{"id":"b","domain":"火星"},{"id":"z","domain":"ai"}]}'
    out = _parse(content, valid_ids={"a", "b"}, valid_domains={"ai", "bio", "geo"})
    assert out == {"a": "ai", "b": "other"}  # 火星→other;z 不在 valid_ids 被丢


def test_classify_resolve_drops_basic():
    """要剔除=不属本域的(缺判按本域不剔)。"""
    from pulsewire.rank.classify import resolve_drops

    verdict = {"a": "ai", "b": "geo", "c": "other"}  # 本域=ai
    drop, over = resolve_drops(["a", "b", "c", "d"], verdict, "ai", max_drop_ratio=0.9)
    assert drop == {"b", "c"} and over is False  # b(geo)/c(other)剔;a留;d缺判按ai留


def test_classify_resolve_drops_over_cap_keeps_all():
    """要剔的比例超上限 → 疑分类器抽风,一个不剔(over=True)。"""
    from pulsewire.rank.classify import resolve_drops

    verdict = {"a": "geo", "b": "geo", "c": "geo", "d": "ai"}  # 本域=ai,3/4 判别域
    drop, over = resolve_drops(["a", "b", "c", "d"], verdict, "ai", max_drop_ratio=0.4)
    assert drop == set() and over is True  # 0.75 > 0.4 → 全留
