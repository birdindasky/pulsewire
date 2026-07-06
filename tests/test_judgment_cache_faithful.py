"""判决缓存(S1)**忠实性 + 省钱**证明:暖缓存 = 逐字节同裁决 + 零 LLM 调用。

pulsewire 的判官(pro)天生跑间非确定(见 deploy/perf/rank_ab.py:同代码背靠背两跑推送集也
churn,bio 板重合率仅 18-25%)。故「缓存开 vs 关跑两遍比选稿」测的是 LLM 噪声、**不是**缓存忠实性。

缓存忠实性的正确证法 = **给定同一批裁决,走缓存路径与走 LLM 路径产出逐字节相同的选稿**:
  第一遍(全 miss):判官对每条给出裁决,收集进 new_verdicts、数 LLM 调用次数 N;
  第二遍(暖缓存):用第一遍的裁决预载 judgment_cache,**把底层 LLM 打成"一调就炸"**,
  跑真实的 filter_*(选稿出口)→ 断言 (a) kept 集与第一遍逐字节相同 (b) LLM 零调用(全命中)。
第二遍任何一次 LLM 调用都会炸 → 证明省钱是真的;kept 集不一致 → 证明缓存不忠实。这是 S1 的硬保证。
"""

from __future__ import annotations

from types import SimpleNamespace

from pulsewire.config import get_settings


def _boom(*a, **k):  # 暖缓存路径下 LLM 一旦被调用即炸 → 证明"零调用"
    raise AssertionError("LLM 被调用了 —— 缓存没命中(不省钱)")


def _evs():
    """一小撮合成事件(heat 分开,覆盖会/不会进 final_limit 的头尾)。"""
    return [
        {"rep_item_id": f"it{i}", "heat_score": 10.0 - i,
         "headline": f"标题{i}某公司发布模型", "subject": f"主体{i}",
         "snippet": f"正文内容{i}" * 20, "representative_source": "src"}
        for i in range(12)
    ]


def _keyset(evs):
    return [e["rep_item_id"] for e in evs]


# ---------------- 水货闸(magnitude):真实 filter_water 忠实性 ---------------- #
def test_magnitude_warm_cache_faithful_and_free(monkeypatch):
    from pulsewire.events import magnitude_judge as mj

    settings = get_settings()
    evs = _evs()
    # 确定性裁决:偶数条判水货。第一遍走真判官(计数),收集裁决。
    calls = []

    def _judge(headline, body, _s):
        calls.append(1)
        idx = int("".join(c for c in headline if c.isdigit())[:2] or "0")
        return (idx % 2 == 0, "even=water")

    monkeypatch.setattr(mj, "judge_is_water", _judge)
    nv: list = []
    j1 = mj.make_water_judge(settings, judgment_cache={}, new_verdicts=nv)
    kept1 = mj.filter_water(list(evs), j1, top_n=25, final_limit=20)
    n_calls_round1 = len(calls)
    assert n_calls_round1 > 0 and nv, "第一遍应真判 + 收集裁决"

    # 第二遍:暖缓存(第一遍裁决)+ LLM 打炸 → 必须零调用、kept 逐字节相同。
    cache = {r["item_hash"]: r["verdict"] for r in nv}
    calls.clear()
    monkeypatch.setattr(mj, "judge_is_water", _boom)
    j2 = mj.make_water_judge(settings, judgment_cache=cache)
    kept2 = mj.filter_water(list(evs), j2, top_n=25, final_limit=20)
    assert calls == [], "暖缓存必须零 LLM 调用(省钱)"
    assert _keyset(kept1) == _keyset(kept2), "暖缓存选稿必须与真判逐字节相同(忠实)"


# ---------------- 够格闸(worthiness):真实 filter_unworthy 忠实性 ---------------- #
def test_worthiness_warm_cache_faithful_and_free(monkeypatch):
    from pulsewire.events import worthiness_judge as wj

    settings = get_settings()
    evs = _evs()
    calls = []

    def _judge(headline, body, _s):
        calls.append(1)
        idx = int("".join(c for c in headline if c.isdigit())[:2] or "0")
        return (idx % 3 != 0, "3n=unworthy")  # 每 3 条判不够格

    monkeypatch.setattr(wj, "judge_is_worthy", _judge)
    nv: list = []
    j1 = wj.make_worthiness_judge(settings, judgment_cache={}, new_verdicts=nv)
    kept1 = wj.filter_unworthy(list(evs), j1, top_n=25, final_limit=20)
    assert calls and nv

    cache = {r["item_hash"]: r["verdict"] for r in nv}
    calls.clear()
    monkeypatch.setattr(wj, "judge_is_worthy", _boom)
    j2 = wj.make_worthiness_judge(settings, judgment_cache=cache)
    kept2 = wj.filter_unworthy(list(evs), j2, top_n=25, final_limit=20)
    assert calls == [], "暖缓存必须零 LLM 调用"
    assert _keyset(kept1) == _keyset(kept2), "暖缓存选稿必须逐字节相同"


# ---------------- 话题闸(topic,board-相关):真实 filter_off_topic 忠实性 ---------------- #
def test_topic_warm_cache_faithful_and_free(monkeypatch):
    from pulsewire.events import topic_judge as tj

    settings = get_settings()
    d = SimpleNamespace(key="ai", label="人工智能", interest="AI 前沿", tags=[])
    portrait = (settings.rank.event_pool.topic_portraits or {}).get("ai")
    evs = _evs()
    calls = []

    def _judge(_d, ev, _s, *, portrait=None):
        calls.append(1)
        idx = int("".join(c for c in ev["headline"] if c.isdigit())[:2] or "0")
        return (idx % 4 == 0, "4n=off_topic")

    monkeypatch.setattr(tj, "judge_off_topic", _judge)
    nv: list = []
    for_board1 = tj.make_topic_judge(settings, judgment_cache={}, new_verdicts=nv)
    kept1 = tj.filter_off_topic(list(evs), for_board1(d), top_n=25, final_limit=20)
    assert calls and nv

    cache = {r["item_hash"]: r["verdict"] for r in nv}
    calls.clear()
    monkeypatch.setattr(tj, "judge_off_topic", _boom)
    for_board2 = tj.make_topic_judge(settings, judgment_cache=cache)
    kept2 = tj.filter_off_topic(list(evs), for_board2(d), top_n=25, final_limit=20)
    assert calls == [], "暖缓存必须零 LLM 调用"
    assert _keyset(kept1) == _keyset(kept2), "暖缓存选稿必须逐字节相同"
    # 键含 portrait:预载须用配置画像(否则假 miss)。旁证:换 portrait → item_hash 变。
    assert tj.topic_item_hash(d, evs[0], portrait) != tj.topic_item_hash(d, evs[0], "别的画像")


# ---------------- 分板器(board):真实 classify_mixed_events 忠实性(干净裁决)---------------- #
def test_board_warm_cache_faithful_and_free(monkeypatch):
    from pulsewire.events import board_classifier as bc

    settings = get_settings()
    active = [SimpleNamespace(key="ai", label="人工智能", interest="AI", tags=[]),
              SimpleNamespace(key="bio", label="生物", interest="Bio", tags=[])]
    evs = [dict(e, is_mixed=True, mixed_sources=[("s36kr", "ai")]) for e in _evs()]
    calls = []

    def _classify(ev, _active, _s, *, portraits=None):
        calls.append(1)
        idx = int("".join(c for c in ev["headline"] if c.isdigit())[:2] or "0")
        # 干净裁决:偶数归 ai、其余真 other(None)。皆非故障 → 可缓存。
        return ("ai", 0.9, False, "ok") if idx % 2 == 0 else (None, 0.0, False, "other")

    monkeypatch.setattr(bc, "classify_board", _classify)
    nv: list = []
    clf1 = bc.make_board_classifier(settings, active, judgment_cache={}, new_verdicts=nv)
    ev1 = [dict(e) for e in evs]
    bc.classify_mixed_events(ev1, clf1, top_n=40)
    doms1 = [e["source_domain"] for e in ev1]
    assert calls and nv

    cache = {r["item_hash"]: r["verdict"] for r in nv}
    calls.clear()
    monkeypatch.setattr(bc, "classify_board", _boom)
    clf2 = bc.make_board_classifier(settings, active, judgment_cache=cache)
    ev2 = [dict(e) for e in evs]
    bc.classify_mixed_events(ev2, clf2, top_n=40)
    doms2 = [e["source_domain"] for e in ev2]
    assert calls == [], "暖缓存必须零 LLM 调用"
    assert doms1 == doms2, "暖缓存分板结果必须逐字节相同"
