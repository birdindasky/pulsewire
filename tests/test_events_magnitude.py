"""重磅度闸(B 档语义"水货"筛):保守判官 + 头部回填 + fail-safe + 成本闸。

盯 codex 审的两条 major:
- M1:filter_water 只**移除**水货、**不截断** board_evs(回填靠 apply_event_quotas 天然实现)。
- M2:严格 `out.get("water") is True` 才砍——脏返回值/字符串/缺失一律 KEEP(B 档命门=零误杀)。
"""
from __future__ import annotations

import pytest

from pulsewire.events import magnitude_judge as M


class _Settings:
    class rank:
        class event_pool:
            max_water_judges_per_run = 2

    class threads:
        judge_model = "x"
        judge_max_tokens = 256


def _ev(item_id: str, heat: float, headline: str = "x") -> dict:
    return {"rep_item_id": item_id, "heat_score": heat, "headline": headline, "snippet": "body"}


# ---------- filter_water:闸关 / 移除不截断 / clamp(M1, m3) ----------

def test_gate_off_returns_unchanged():
    evs = [_ev("a", 3), _ev("b", 2)]
    assert M.filter_water(evs, None, top_n=25, final_limit=20) is evs  # judge=None 原样返回


def test_removes_only_water_not_truncate():
    # 5 条,只有 b 判水货 → 返回 4 条(a,c,d,e),不截断成"头部窗口"
    evs = [_ev("a", 5), _ev("b", 4), _ev("c", 3), _ev("d", 2), _ev("e", 1)]
    judge = lambda e: e["rep_item_id"] == "b"  # noqa: E731
    kept = M.filter_water(evs, judge, top_n=25, final_limit=20)
    assert [e["rep_item_id"] for e in kept] == ["a", "c", "d", "e"]


def test_no_water_returns_original_list():
    evs = [_ev("a", 2), _ev("b", 1)]
    kept = M.filter_water(evs, lambda e: False, top_n=25, final_limit=20)
    assert [e["rep_item_id"] for e in kept] == ["a", "b"]


def test_only_judges_head_by_heat():
    # top_n 限制:只判 heat 头部。判官把"被判到的都标水货",验证只有头部被判(尾部幸存)
    judged: list[str] = []

    def judge(e: dict) -> bool:
        judged.append(e["rep_item_id"])
        return True

    evs = [_ev(f"e{i}", float(i)) for i in range(40)]  # heat 0..39
    # top_n=10 但 final_limit=20 → clamp 到 max(10, 25)=25,判 heat 最高的 25 条(e39..e15)
    kept = M.filter_water(evs, judge, top_n=10, final_limit=20)
    assert len(judged) == 25  # clamp 生效(m3):不是 10
    assert set(judged) == {f"e{i}" for i in range(15, 40)}  # 头部 25 条
    assert len(kept) == 15  # 40 - 25 全判水货被移除


def test_clamp_protects_final_limit_tail():
    # top_n 配得比 final_limit 还小 → clamp 兜底,final_limit 尾部不漏判(m3 命门)
    judged: list[str] = []
    evs = [_ev(f"e{i}", float(i)) for i in range(30)]
    M.filter_water(evs, lambda e: judged.append(e["rep_item_id"]) or False, top_n=5, final_limit=20)
    assert len(judged) == 25  # max(5, 20+5)=25,绝不只判 5 条


# ---------- judge_is_water:严格 is True(M2 命门) ----------

@pytest.mark.parametrize("payload,expected", [
    ({"water": True}, True),       # 唯一会砍的情况
    ({"water": False}, False),
    ({"water": "yes"}, False),     # 字符串!bool("yes")==True 会误杀 → 必须 KEEP
    ({"water": "true"}, False),    # 同上
    ({"water": 1}, False),         # 1 is True 为 False(is 比较) → KEEP
    ({"water": None}, False),
    ({}, False),                   # 字段缺失 → KEEP
])
def test_strict_is_true_only(monkeypatch, payload, expected):
    monkeypatch.setattr(M, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(M, "parse_json", lambda _s: payload)
    is_water, _ = M.judge_is_water("h", "b", settings=_Settings())
    assert is_water is expected


def test_parse_json_none_keeps(monkeypatch):
    monkeypatch.setattr(M, "complete_json", lambda *a, **k: "garbage")
    monkeypatch.setattr(M, "parse_json", lambda _s: {})  # 乱码 → 空 dict
    is_water, _ = M.judge_is_water("h", "b", settings=_Settings())
    assert is_water is False


# ---------- make_water_judge:fail-safe + 成本闸 + 缓存 ----------

def test_judge_failure_keeps(monkeypatch):
    # LLM 抛异常 → KEEP(False),绝不拖垮选稿/误杀
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(M, "complete_json", boom)
    judge = M.make_water_judge(_Settings())
    assert judge(_ev("a", 1)) is False


def test_cost_cap_keeps_rest(monkeypatch):
    # 成本闸=2:前 2 条真判(都判水货),第 3 条起到顶 → 保守全留(False)
    monkeypatch.setattr(M, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(M, "parse_json", lambda _s: {"water": True})
    judge = M.make_water_judge(_Settings())
    assert judge(_ev("a", 3)) is True
    assert judge(_ev("b", 2)) is True
    assert judge(_ev("c", 1)) is False  # 超闸 → KEEP


def test_cache_no_double_judge(monkeypatch):
    calls = [0]

    def counting(*a, **k):
        calls[0] += 1
        return "ignored"
    monkeypatch.setattr(M, "complete_json", counting)
    monkeypatch.setattr(M, "parse_json", lambda _s: {"water": False})
    judge = M.make_water_judge(_Settings())
    ev = _ev("a", 1)
    judge(ev)
    judge(ev)  # 同 rep_item_id → 命中缓存
    assert calls[0] == 1


# ---------- 多数票(治 flash 抽风偶发返错漏水货,2026-06-25 缝) ----------

class _SettingsVote:
    class rank:
        class event_pool:
            max_water_judges_per_run = 99
            magnitude_judge_votes = 3

    class threads:
        judge_model = "x"
        judge_max_tokens = 256


def _seq_judge(monkeypatch, verdicts):
    it = iter(verdicts)
    monkeypatch.setattr(M, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(M, "parse_json", lambda _s: {"water": next(it)})


def test_vote_majority_drops(monkeypatch):
    # 真水货:头两票就够多数 → 砍(且提前停,只判 2 次)
    _seq_judge(monkeypatch, [True, True, True])
    assert M.make_water_judge(_SettingsVote())(_ev("a", 1)) is True


def test_vote_single_flake_kept(monkeypatch):
    # 1 真 2 假 → 不够多数 → 留(单条假阳被多数稀释,B 档零误杀更稳)
    _seq_judge(monkeypatch, [True, False, False])
    assert M.make_water_judge(_SettingsVote())(_ev("a", 1)) is False


def test_vote_resilient_to_one_flake_drop(monkeypatch):
    # 真水货但首票抽风返假(F,T,T)→ 多数仍砍(正是 06:00 漏 Reddit 那条要补的)
    _seq_judge(monkeypatch, [False, True, True])
    assert M.make_water_judge(_SettingsVote())(_ev("a", 1)) is True


def test_vote_budget_cut_is_conservative_keep(monkeypatch):
    # 成本闸只够 1 票、votes=3 需 2 票 → 凑不够多数 → 保守留(绝不因预算耗尽误杀)
    class _Cap1(_SettingsVote):
        class rank:
            class event_pool:
                max_water_judges_per_run = 1
                magnitude_judge_votes = 3
    monkeypatch.setattr(M, "complete_json", lambda *a, **k: "ignored")
    monkeypatch.setattr(M, "parse_json", lambda _s: {"water": True})
    assert M.make_water_judge(_Cap1())(_ev("a", 1)) is False
