"""话题闸(P0:治 AI 板混入非 AI):保守判官 + 头部回填 + fail-safe + 成本闸 + 跨板块共享预算。

命门 = 砍跑题不误杀真内容,照搬 magnitude_judge 哲学:
- 只**移除**跑题、**不截断** board_evs(回填靠 apply_event_quotas 天然实现)。
- 严格 `out.get("off_topic") is True` 才砍——脏返回值/字符串/缺失一律 KEEP。
- 成本闸/缓存**跨所有板块共享**(max_topic_judges_per_run 是真·每 run 全局硬顶)。
"""
from __future__ import annotations

import pytest

from pulsewire.events import topic_judge as T


class _Domain:
    def __init__(self, key="ai", label="AI", interest="AI 编程助手与大语言模型", tags=None):
        self.key = key
        self.label = label
        self.interest = interest
        self.tags = tags or ["llm", "ai"]


class _Settings:
    class rank:
        class event_pool:
            max_topic_judges_per_run = 2
            topic_judge_votes = 1  # 基类测缓存/成本闸/fail-safe 等机制,用单发隔离(多数票另有专测,见文件末)
            topic_portraits: dict = {}

    class threads:
        judge_model = "x"
        judge_max_tokens = 256


def _ev(item_id: str, heat: float, headline: str = "x") -> dict:
    return {"rep_item_id": item_id, "heat_score": heat, "headline": headline,
            "subject": headline, "snippet": "body", "representative_source": "src"}


# ---------- filter_off_topic:闸关 / 移除不截断 / clamp ----------

def test_gate_off_returns_unchanged():
    evs = [_ev("a", 3), _ev("b", 2)]
    assert T.filter_off_topic(evs, None, top_n=25, final_limit=10) is evs  # judge=None 原样返回


def test_removes_only_offtopic_not_truncate():
    # 5 条,只有 b 判跑题 → 返回 4 条(a,c,d,e),不截断成"头部窗口"
    evs = [_ev("a", 5), _ev("b", 4), _ev("c", 3), _ev("d", 2), _ev("e", 1)]
    judge = lambda e: e["rep_item_id"] == "b"  # noqa: E731
    kept = T.filter_off_topic(evs, judge, top_n=25, final_limit=10)
    assert [e["rep_item_id"] for e in kept] == ["a", "c", "d", "e"]


def test_no_offtopic_returns_original_list():
    evs = [_ev("a", 2), _ev("b", 1)]
    kept = T.filter_off_topic(evs, lambda e: False, top_n=25, final_limit=10)
    assert [e["rep_item_id"] for e in kept] == ["a", "b"]


def test_only_judges_head_by_heat():
    # top_n 限制:只判 heat 头部。判官把"被判到的都标跑题",验证只有头部被判(尾部幸存)
    judged: list[str] = []

    def judge(e: dict) -> bool:
        judged.append(e["rep_item_id"])
        return True

    evs = [_ev(f"e{i}", float(i)) for i in range(40)]  # heat 0..39
    # top_n=10 但 final_limit=20 → clamp 到 max(10, 25)=25,判 heat 最高的 25 条(e39..e15)
    kept = T.filter_off_topic(evs, judge, top_n=10, final_limit=20)
    assert len(judged) == 25  # clamp 生效:不是 10
    assert set(judged) == {f"e{i}" for i in range(15, 40)}  # 头部 25 条
    assert len(kept) == 15  # 40 - 25 全判跑题被移除


def test_clamp_protects_final_limit_tail():
    # top_n 配得比 final_limit 还小 → clamp 兜底,final_limit 尾部不漏判
    judged: list[str] = []
    evs = [_ev(f"e{i}", float(i)) for i in range(30)]
    T.filter_off_topic(evs, lambda e: judged.append(e["rep_item_id"]) or False, top_n=5, final_limit=20)
    assert len(judged) == 25  # max(5, 20+5)=25,绝不只判 5 条


# ---------- judge_off_topic:严格 is True(命门) ----------

@pytest.mark.parametrize("payload,expected", [
    ({"off_topic": True}, True),       # 唯一会砍的情况
    ({"off_topic": False}, False),
    ({"off_topic": "yes"}, False),     # 字符串!bool("yes")==True 会误杀 → 必须 KEEP
    ({"off_topic": "true"}, False),    # 同上
    ({"off_topic": 1}, False),         # 1 is True 为 False(is 比较) → KEEP
    ({"off_topic": None}, False),
    ({}, False),                       # 字段缺失 → KEEP
])
def test_strict_is_true_only(monkeypatch, payload, expected):
    monkeypatch.setattr(T, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(T, "parse_json", lambda _s: payload)
    is_off, _ = T.judge_off_topic(_Domain(), _ev("a", 1), settings=_Settings())
    assert is_off is expected


def test_parse_json_empty_keeps(monkeypatch):
    monkeypatch.setattr(T, "complete_json", lambda *a, **k: "garbage")
    monkeypatch.setattr(T, "parse_json", lambda _s: {})  # 乱码 → 空 dict
    is_off, _ = T.judge_off_topic(_Domain(), _ev("a", 1), settings=_Settings())
    assert is_off is False


# ---------- make_topic_judge:fail-safe + 成本闸 + 缓存 + 跨板块共享 + 画像 ----------

def test_judge_failure_keeps(monkeypatch):
    # LLM 抛异常 → KEEP(False),绝不拖垮选稿/误杀
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(T, "complete_json", boom)
    judge = T.make_topic_judge(_Settings())(_Domain())
    assert judge(_ev("a", 1)) is False


def test_cost_cap_shared_across_boards(monkeypatch):
    # 成本闸=2 且跨板块共享:ai 板判 2 条用满全局预算,bio 板第 3 条直接到顶 → 保守 KEEP
    monkeypatch.setattr(T, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(T, "parse_json", lambda _s: {"off_topic": True})
    for_board = T.make_topic_judge(_Settings())
    ai = for_board(_Domain(key="ai"))
    bio = for_board(_Domain(key="bio", label="生物医疗"))
    assert ai(_ev("a", 3)) is True
    assert ai(_ev("b", 2)) is True
    assert bio(_ev("c", 1)) is False  # 全局预算已被 ai 板用尽 → KEEP(证明计数跨板块共享)


def test_cache_no_double_judge(monkeypatch):
    calls = [0]

    def counting(*a, **k):
        calls[0] += 1
        return "ignored"
    monkeypatch.setattr(T, "complete_json", counting)
    monkeypatch.setattr(T, "parse_json", lambda _s: {"off_topic": False})
    judge = T.make_topic_judge(_Settings())(_Domain())
    ev = _ev("a", 1)
    judge(ev)
    judge(ev)  # 同 (board, rep_item_id) → 命中缓存
    assert calls[0] == 1


def test_same_item_different_board_not_cached(monkeypatch):
    # 同一 item 在不同板块判归属是两件事(可能 ai 跑题 bio 不跑题)→ 缓存键含 board,不互相污染
    calls = [0]

    def counting(*a, **k):
        calls[0] += 1
        return "ignored"
    monkeypatch.setattr(T, "complete_json", counting)
    monkeypatch.setattr(T, "parse_json", lambda _s: {"off_topic": False})
    for_board = T.make_topic_judge(_Settings())
    ev = _ev("a", 1)
    for_board(_Domain(key="ai"))(ev)
    for_board(_Domain(key="bio", label="生物医疗"))(ev)
    assert calls[0] == 2  # 不同板块 → 各判一次


def test_portrait_passed_into_prompt(monkeypatch):
    captured = {}

    def cap(_system, user, **k):
        captured["user"] = user
        return "ignored"
    monkeypatch.setattr(T, "complete_json", cap)
    monkeypatch.setattr(T, "parse_json", lambda _s: {"off_topic": False})

    class S(_Settings):
        class rank:
            class event_pool:
                max_topic_judges_per_run = 5
                topic_judge_votes = 1
                topic_portraits = {"ai": "只要 AI/大模型/AI芯片即属于"}

    judge = T.make_topic_judge(S())(_Domain(key="ai"))
    judge(_ev("a", 1))
    assert "只要 AI/大模型/AI芯片即属于" in captured["user"]  # 画像注入 prompt


# ---------- 多数票(2026-07-01):判跑题需严格多数,凑不齐 KEEP(保守,治单判抖动放跑题货进 AI 板)----------

def _votes_settings(votes: int, cap: int = 99):
    class S(_Settings):
        class rank:
            class event_pool:
                max_topic_judges_per_run = cap
                topic_judge_votes = votes
                topic_portraits: dict = {}

        class threads:
            judge_model = "x"
            judge_max_tokens = 256
    return S()


def test_voting_majority_drops(monkeypatch):
    # 3 票里 2 票判跑题 → 达严格多数(need=2)→ 踢
    seq = iter([{"off_topic": True}, {"off_topic": False}, {"off_topic": True}])
    monkeypatch.setattr(T, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(T, "parse_json", lambda _s: next(seq))
    judge = T.make_topic_judge(_votes_settings(3))(_Domain())
    assert judge(_ev("a", 1)) is True


def test_voting_split_keeps(monkeypatch):
    # 3 票里只 1 票判跑题 → 不够多数 → KEEP(命门:宁错放别错杀,单次抖动杀不掉真内容)
    seq = iter([{"off_topic": True}, {"off_topic": False}, {"off_topic": False}])
    monkeypatch.setattr(T, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(T, "parse_json", lambda _s: next(seq))
    judge = T.make_topic_judge(_votes_settings(3))(_Domain())
    assert judge(_ev("a", 1)) is False


def test_voting_all_fail_keeps(monkeypatch):
    # 每票都抛异常(该票=不跑题)→ off 票=0 < 多数 → KEEP(故障绝不误杀真内容)
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(T, "complete_json", boom)
    judge = T.make_topic_judge(_votes_settings(3))(_Domain())
    assert judge(_ev("a", 1)) is False
