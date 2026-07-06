"""events.allocate:板块分配 + 限额(复刻 apply_quotas 防刷屏)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulsewire.config.models import EventPoolCfg, RankCfg
from pulsewire.events import allocate as A

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def test_assign_board_argmax_passes_gate():
    cfg = EventPoolCfg()  # τ_rel=0.5
    assert A.assign_board({"relevance": {"ai": 0.8, "geo": 0.2}}, cfg=cfg) == "ai"
    assert A.assign_board({"relevance": {"ai": 0.4, "geo": 0.3}}, cfg=cfg) is None  # 都不过闸
    # 量级实体降到 τ_floor=0.3
    assert A.assign_board({"relevance": {"ai": 0.4}, "is_magnitude": True}, cfg=cfg) == "ai"
    assert A.assign_board({"relevance": {}}, cfg=cfg) is None


def test_quota_per_source_family_folds_mirrors():
    # 5 个 google-news 镜像高热事件 → 源族折叠后受 per_source_limit=2 限,不刷屏
    cfg = RankCfg()  # per_source_limit=2, per_category_limit=8
    evs = [
        {"heat_score": 10 - i, "representative_source": f"geo-google-news-{i}", "category": "geo", "peak_at": NOW}
        for i in range(5)
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    assert len(kept) == 2  # 同源族(google-news)只放 per_source_limit 个


def test_quota_per_category_limit():
    cfg = RankCfg()  # per_category_limit=8
    evs = [
        {"heat_score": 50 - i, "representative_source": f"src{i}", "category": "ai", "peak_at": NOW}
        for i in range(12)
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    assert len(kept) == 8  # 单类目封顶


def test_quota_final_limit_and_heat_order():
    cfg = RankCfg()
    evs = [
        {"heat_score": float(i), "representative_source": f"src{i}", "category": f"c{i}", "peak_at": NOW}
        for i in range(30)
    ]
    kept = A.apply_event_quotas(evs, final_limit=5, cfg=cfg, now=NOW)
    assert len(kept) == 5
    assert [e["heat_score"] for e in kept] == [29, 28, 27, 26, 25]  # 热度降序取前 N


def test_quota_old_item_limit():
    cfg = RankCfg()  # old_item_age_hours=168, old_item_limit=5
    old = NOW - timedelta(hours=200)
    evs = [
        {"heat_score": 50 - i, "representative_source": f"src{i}", "category": f"c{i}", "peak_at": old}
        for i in range(10)
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    assert len(kept) == 5  # 老项限额封顶


def test_quota_same_event_dedup_by_subject_vec():
    """同事件去重兜底:主体短语向量高相似(≥select_sim_dedup 0.70)的事件,留高热那张、折叠另一张。"""
    from pulsewire.config.models import RankCfg
    cfg = RankCfg()  # select_sim_dedup=0.70
    evs = [
        {"heat_score": 9.0, "category": "c1", "representative_source": "s1", "peak_at": NOW,
         "subject_vec": [1.0, 0.0], "rep_item_id": "hi"},   # 高热
        {"heat_score": 5.0, "category": "c2", "representative_source": "s2", "peak_at": NOW,
         "subject_vec": [0.99, 0.01], "rep_item_id": "dup"},  # 与上同事件(cos≈1)→折叠
        {"heat_score": 4.0, "category": "c3", "representative_source": "s3", "peak_at": NOW,
         "subject_vec": [0.0, 1.0], "rep_item_id": "other"},  # 正交→保留
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    ids = [e["rep_item_id"] for e in kept]
    assert "hi" in ids and "other" in ids and "dup" not in ids  # 同事件只留高热


def test_quota_midband_judge_folds():
    """中等相似带 [0.55,0.70) 交判官:判同则折叠。"""
    from pulsewire.config.models import RankCfg
    import math
    cfg = RankCfg()
    # 构造 cos≈0.62(落中等带):单位向量夹角
    a = [1.0, 0.0]
    b = [math.cos(0.9), math.sin(0.9)]  # cos(0.9rad)≈0.62
    evs = [
        {"heat_score": 9.0, "category": "c1", "representative_source": "s1", "peak_at": NOW, "subject_vec": a, "rep_item_id": "hi"},
        {"heat_score": 5.0, "category": "c2", "representative_source": "s2", "peak_at": NOW, "subject_vec": b, "rep_item_id": "mid"},
    ]
    # 判官说"同" → 折叠
    kept_same = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW, same_event=lambda x, y: True)
    assert [e["rep_item_id"] for e in kept_same] == ["hi"]
    # 判官说"不同" → 都留
    kept_diff = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW, same_event=lambda x, y: False)
    assert len(kept_diff) == 2


def test_shares_salient_entity():
    """显著实体提取:同公司共享实体 True;泛词/无交集 False。"""
    a = {"headline": "SpaceX is public: everything you need to know post-IPO"}
    b = {"headline": "SpaceX leapfrogs Amazon to become world's fifth-most valuable company"}
    c = {"headline": "Nvidia raises $25 billion in record corporate bond sale"}
    assert A.shares_salient_entity(a, b) is True   # 都含 SpaceX
    assert A.shares_salient_entity(a, c) is False  # SpaceX vs Nvidia,无交集
    # 纯泛词/停用词不算共享实体(都以 The/New 起头但无实体交集)
    d = {"headline": "The market rallies on new optimism"}
    e = {"headline": "The economy slows amid new fears"}
    assert A.shares_salient_entity(d, e) is False


def test_quota_entity_dedup_low_cosine_judge_same():
    """关键:同事件换个角度(余弦低但共享实体 SpaceX),判官说同 → 折叠(治全局聚类漏的同事件)。"""
    from pulsewire.config.models import RankCfg
    cfg = RankCfg()
    evs = [  # 两张主体向量近正交(余弦≈0,过不了 event_dedup_min_sim),但标题都含 SpaceX
        {"heat_score": 9.0, "category": "c1", "representative_source": "s1", "peak_at": NOW,
         "subject_vec": [1.0, 0.0], "rep_item_id": "buy",
         "headline": "Elon Musk's SpaceX just bought AI coding startup Cursor for $60 billion"},
        {"heat_score": 5.0, "category": "c2", "representative_source": "s2", "peak_at": NOW,
         "subject_vec": [0.0, 1.0], "rep_item_id": "cap",
         "headline": "SpaceX leapfrogs Amazon to become world's fifth-most valuable company"},
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW, same_event=lambda x, y: True)
    assert [e["rep_item_id"] for e in kept] == ["buy"]  # 共享实体送判官、判同 → 折叠低热那张


def test_quota_entity_share_judge_differs_keeps_both():
    """同公司不设闸:共享实体但判官说不同(IPO≠收购)→ 都留,不按实体限张数。"""
    from pulsewire.config.models import RankCfg
    cfg = RankCfg()
    evs = [
        {"heat_score": 9.0, "category": "c1", "representative_source": "s1", "peak_at": NOW,
         "subject_vec": [1.0, 0.0], "rep_item_id": "ipo",
         "headline": "SpaceX is public: everything you need to know post-IPO"},
        {"heat_score": 5.0, "category": "c2", "representative_source": "s2", "peak_at": NOW,
         "subject_vec": [0.0, 1.0], "rep_item_id": "buy",
         "headline": "SpaceX bought AI coding startup Cursor for $60 billion"},
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW, same_event=lambda x, y: False)
    assert len(kept) == 2  # 同公司、不同真事件 → 全留(无实体限张数)


def test_quota_academic_paper_limit():
    """硬核学术论文限额:纯论文源(arxiv-*)每板块最多 academic_paper_limit 条,其余名额留给易懂新闻。"""
    cfg = RankCfg()  # academic_paper_limit=2, prefixes=["arxiv"]
    evs = [
        {"heat_score": 50 - i, "category": f"c{i}", "representative_source": f"arxiv-cs-{i}", "peak_at": NOW}
        for i in range(5)  # 5 条 arxiv 论文(各不同子源,避免 per_source 先拦)
    ] + [
        {"heat_score": 5 - i, "category": f"n{i}", "representative_source": f"techcrunch-{i}", "peak_at": NOW}
        for i in range(3)  # 3 条非学术新闻
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    acad = [e for e in kept if e["representative_source"].startswith("arxiv")]
    non = [e for e in kept if not e["representative_source"].startswith("arxiv")]
    assert len(acad) == 2  # arxiv 封顶 2 条(留最热的两条)
    assert len(non) == 3   # 非学术新闻不受此限,全留


def test_quota_academic_limit_off():
    """academic_paper_limit=0 = 关闭学术限额,arxiv 全留。"""
    cfg = RankCfg(academic_paper_limit=0)
    evs = [
        {"heat_score": 50 - i, "category": f"c{i}", "representative_source": f"arxiv-cs-{i}", "peak_at": NOW}
        for i in range(5)
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW)
    assert len(kept) == 5


def test_quota_judge_budget_exhausted_keeps_conservative():
    """判官预算耗尽后保守不折叠(绝不盲合):预算设 0 → 全留,即便判官会判同。"""
    from pulsewire.config.models import RankCfg
    cfg = RankCfg(event_dedup_max_judges=0)
    evs = [
        {"heat_score": 9.0, "category": "c1", "representative_source": "s1", "peak_at": NOW,
         "subject_vec": [1.0, 0.0], "rep_item_id": "a", "headline": "SpaceX IPO"},
        {"heat_score": 5.0, "category": "c2", "representative_source": "s2", "peak_at": NOW,
         "subject_vec": [1.0, 0.0], "rep_item_id": "b", "headline": "SpaceX valuation"},
    ]
    kept = A.apply_event_quotas(evs, final_limit=20, cfg=cfg, now=NOW, same_event=lambda x, y: True)
    assert len(kept) == 2  # 预算 0 → 没问判官 → 保守全留
