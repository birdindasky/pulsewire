"""分板分类器(board_classifier:治 50 综合源乱跑):判板归属 + 保守丢弃 + 成本闸 + 缓存 + 旁路过滤。

命门(与 topic_judge **保守方向相反**,务必看清):纯 mixed 事件无专源锚、无可信 domain →
判不出时保守 = **丢弃(None)**,绝不退回乱跑。故脏返回/abstain/other/低置信/LLM 失败/成本闸到顶
→ 一律 None(丢弃)。只有"高置信归某个 active 板"才放行。
"""
from __future__ import annotations

import pytest

from pulsewire.events import board_classifier as B


class _Domain:
    def __init__(self, key, label=None, interest=None, tags=None):
        self.key = key
        self.label = label or key.upper()
        self.interest = interest or f"{key} 领域"
        self.tags = tags or [key]


_ACTIVE = [_Domain("ai", "AI", "AI 编程助手与大语言模型", ["llm", "ai"]),
           _Domain("bio", "生物医疗", "生物医疗与生命科学"),
           _Domain("geo", "国际局势", "国际局势与地缘政治")]


class _Settings:
    class rank:
        class event_pool:
            max_board_judges_per_run = 2
            board_judge_top_n = 30
            topic_portraits: dict = {}

    class threads:
        judge_model = "x"
        judge_max_tokens = 256


def _ev(item_id: str, heat: float = 1.0, headline: str = "x", is_mixed: bool = True) -> dict:
    e = {"rep_item_id": item_id, "heat_score": heat, "headline": headline,
         "subject": headline, "snippet": "body", "representative_source": "axios",
         "mixed_sources": [("axios", "ai"), ("it", "ai")]}
    if is_mixed:
        e["is_mixed"] = True
    return e


# ---------- classify_board:判定逻辑(严格,保守丢弃) ----------

@pytest.mark.parametrize("payload,expected_board", [
    ({"board": "ai", "confidence": 0.9, "abstain": False}, "ai"),   # 高置信归板
    ({"board": "bio", "confidence": 0.8}, "bio"),
    ({"board": "AI", "confidence": 0.9}, "ai"),                     # 大小写 normalize
    ({"board": " geo ", "confidence": 0.9}, "geo"),                # 空格 normalize
    ({"board": "other", "confidence": 0.95}, None),                # other → 丢
    ({"board": "ai", "confidence": 0.9, "abstain": True}, None),   # abstain → 丢(即便给了 board)
    ({"board": "ai", "confidence": 0.5}, None),                    # 低置信(<0.6)→ 丢
    ({"board": "finance", "confidence": 0.9}, None),               # 无效 key → 丢
    ({"board": "ai", "confidence": "bad"}, None),                  # 脏 confidence 兜 0 → 丢
    ({"board": "", "confidence": 0.9}, None),                      # 空 board → 丢
    ({}, None),                                                    # 空返回 → 丢
])
def test_classify_board_decision(monkeypatch, payload, expected_board):
    monkeypatch.setattr(B, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(B, "parse_json", lambda _s: payload)
    board, _conf, _abstain, _reason = B.classify_board(_ev("a"), _ACTIVE, _Settings())
    assert board == expected_board


def test_abstain_strict_is_true(monkeypatch):
    # abstain 必须严格 is True;"yes"/1 这类不算弃权(走正常判定)
    monkeypatch.setattr(B, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(B, "parse_json", lambda _s: {"board": "ai", "confidence": 0.9, "abstain": "yes"})
    board, _c, abstain, _r = B.classify_board(_ev("a"), _ACTIVE, _Settings())
    assert board == "ai" and abstain is False  # "yes" 不是 True → 不弃权,正常归板


# ---------- make_board_classifier:工厂(保守丢弃 + 成本闸 + 缓存 + fail-safe) ----------

def test_judge_failure_drops(monkeypatch):
    # LLM 抛异常 → None(丢弃,与 topic_judge 的 KEEP 相反)
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(B, "complete_json", boom)
    classify = B.make_board_classifier(_Settings(), _ACTIVE)
    assert classify(_ev("a")) is None


def test_cost_cap_drops_when_exhausted(monkeypatch):
    # 成本闸=2:判满 2 个后,第 3 个到顶 → None(保守丢弃,绝不放行无 domain 的事件)
    monkeypatch.setattr(B, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(B, "parse_json", lambda _s: {"board": "ai", "confidence": 0.9})
    classify = B.make_board_classifier(_Settings(), _ACTIVE)
    assert classify(_ev("a")) == "ai"
    assert classify(_ev("b")) == "ai"
    assert classify(_ev("c")) is None  # 预算耗尽 → 丢(不是放行)


def test_cache_no_double_judge(monkeypatch):
    calls = [0]

    def counting(*a, **k):
        calls[0] += 1
        return "ignored"
    monkeypatch.setattr(B, "complete_json", counting)
    monkeypatch.setattr(B, "parse_json", lambda _s: {"board": "ai", "confidence": 0.9})
    classify = B.make_board_classifier(_Settings(), _ACTIVE)
    ev = _ev("a")
    assert classify(ev) == "ai"
    assert classify(ev) == "ai"  # 同 rep_item_id 命中缓存
    assert calls[0] == 1


class _Settings3:
    """board_judge_votes=3 的设置(测多数票;预算给足不触成本闸)。"""
    class rank:
        class event_pool:
            max_board_judges_per_run = 99
            board_judge_top_n = 30
            board_judge_votes = 3
            topic_portraits: dict = {}

    class threads:
        judge_model = "x"
        judge_max_tokens = 256


def test_majority_vote_assigns_when_consistent(monkeypatch):
    # 3 票判官一致归 ai → 严格多数(早停在 2 票)→ 归板
    monkeypatch.setattr(B, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(B, "parse_json", lambda _s: {"board": "ai", "confidence": 0.9})
    classify = B.make_board_classifier(_Settings3(), _ACTIVE)
    assert classify(_ev("a")) == "ai"


def test_split_vote_drops(monkeypatch):
    # 判官抽风:ai / other / other → ai 只 1 票,任何真板都凑不齐多数(need=2)→ 丢弃(保守,治边缘货飘进板)
    seq = iter([{"board": "ai", "confidence": 0.9},
                {"board": "other", "confidence": 0.9},
                {"board": "other", "confidence": 0.9}])
    monkeypatch.setattr(B, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(B, "parse_json", lambda _s: next(seq))
    classify = B.make_board_classifier(_Settings3(), _ACTIVE)
    assert classify(_ev("a")) is None


def test_member_evidence_in_prompt(monkeypatch):
    captured = {}

    def cap(_system, user, **k):
        captured["user"] = user
        return "ignored"
    monkeypatch.setattr(B, "complete_json", cap)
    monkeypatch.setattr(B, "parse_json", lambda _s: {"board": "ai", "confidence": 0.9})
    B.make_board_classifier(_Settings(), _ACTIVE)(_ev("a"))
    # member 证据(综合源 id)注入 prompt,闭 codex 反馈②
    assert "axios" in captured["user"] and "it" in captured["user"]
    assert "标签不可信" in captured["user"]  # 教 LLM 别信源标签


# ---------- classify_mixed_events:旁路过滤(只碰 is_mixed、头部判尾部丢、专源零接触) ----------

def test_gate_off_touches_nothing():
    pro = {"headline": "专源", "source_domain": "ai"}
    mixed = {"headline": "m", "is_mixed": True, "source_domain": None, "heat_score": 5}
    events = [pro, mixed]
    B.classify_mixed_events(events, None, top_n=30)  # classify=None
    assert pro["source_domain"] == "ai" and mixed["source_domain"] is None  # 全没动


def test_only_mixed_head_judged_pro_untouched():
    def fake(ev):
        return "ai" if "AI" in ev["headline"] else None  # 含 AI 归板,否则丢
    pro = {"headline": "专源真AI", "source_domain": "ai", "heat_score": 9}        # 专源,无 is_mixed
    m_hi = {"headline": "mixed AI 高热", "is_mixed": True, "source_domain": None, "heat_score": 8}
    m_fin = {"headline": "mixed 财经", "is_mixed": True, "source_domain": None, "heat_score": 7}
    m_lo = {"headline": "mixed AI 低热", "is_mixed": True, "source_domain": None, "heat_score": 1}
    events = [pro, m_hi, m_fin, m_lo]
    B.classify_mixed_events(events, fake, top_n=2)  # 只判 heat 头部 2 个 is_mixed
    assert pro["source_domain"] == "ai"      # 专源零接触
    assert m_hi["source_domain"] == "ai"     # 归对板
    assert m_fin["source_domain"] is None    # 判 other 丢
    assert m_lo["source_domain"] is None     # 超 top_n 未判 = 丢


def test_top_n_zero_judges_none():
    judged = []
    events = [{"headline": "m", "is_mixed": True, "source_domain": None, "heat_score": 5}]
    B.classify_mixed_events(events, lambda e: judged.append(e) or "ai", top_n=0)
    assert judged == [] and events[0]["source_domain"] is None  # top_n=0 → 一个不判,全丢


# ---------- is_bypass_cluster:旁路判据(闭 codex Fix2 精确触发) ----------

class _Src:
    def __init__(self, board_only=False, mixed=False):
        self.board_only = board_only
        self.mixed = mixed


_SOURCES = {
    "pro-ai": _Src(),                       # 专源
    "axios": _Src(mixed=True),              # 综合源
    "it": _Src(mixed=True),                 # 综合源
    "gh": _Src(board_only=True),            # GitHub 项目榜
}


@pytest.mark.parametrize("members,expected,why", [
    (["axios", "it"], True, "全 mixed → 旁路"),
    (["axios", "pro-ai"], False, "含专源锚 → 主路(mixed 搭车不算)"),
    (["pro-ai"], False, "纯专源 → 主路"),
    (["gh"], False, "全 board_only,无 mixed → 主路(Fix2:不误纳进判官)"),
    (["axios", "gh"], True, "mixed+board_only,无专源 → 旁路"),
    (["unknown-src"], False, "源不在 dict、无 mixed → 主路"),
    (["unknown-src", "axios"], True, "未知源+mixed,无专源 → 旁路"),
    ([], False, "空 → 主路"),
])
def test_is_bypass_cluster(members, expected, why):
    assert B.is_bypass_cluster(members, _SOURCES) is expected, why
