"""要闻够格闸(worthiness_judge):默认踢、够格才留 + fail-safe 留(不因故障误杀)+ 多数票 + 成本闸。

命门(与水货闸方向相反):默认踢,某条留需 LLM 多数票判 worthy;但**基础设施失败一律留**
(LLM 失败/超预算/脏返回 → 该票算 worthy),绝不因故障误杀真新闻。
"""
from __future__ import annotations

from pulsewire.events import worthiness_judge as W


class _Settings:
    class rank:
        class event_pool:
            max_worthiness_judges_per_run = 99
            worthiness_judge_top_n = 25
            worthiness_judge_votes = 3

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


class _Settings1(_Settings):
    """单票(votes=1)便于测基本判定。"""
    class rank:
        class event_pool:
            max_worthiness_judges_per_run = 99
            worthiness_judge_top_n = 25
            worthiness_judge_votes = 1

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


def _ev(item_id: str, heat: float = 1.0, headline: str = "x") -> dict:
    return {"rep_item_id": item_id, "heat_score": heat, "headline": headline, "snippet": "body"}


# ---------- judge_is_worthy:严格判定(脏值→留) ----------

def test_judge_worthy_true(monkeypatch):
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": True, "reason": "真发布"})
    w, _ = W.judge_is_worthy("h", "b", _Settings())
    assert w is True


def test_judge_unworthy_strict_false(monkeypatch):
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": False, "reason": "论坛闲聊"})
    w, _ = W.judge_is_worthy("h", "b", _Settings())
    assert w is False


def test_judge_dirty_return_keeps(monkeypatch):
    # 脏返回/缺字段 → 留(保护真新闻不被故障误踢)
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {})
    w, _ = W.judge_is_worthy("h", "b", _Settings())
    assert w is True
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": "no"})  # "no" 非 False → 留
    w2, _ = W.judge_is_worthy("h", "b", _Settings())
    assert w2 is True


# ---------- make_worthiness_judge:工厂(默认踢 + fail-safe 留 + 成本闸 + 多数票) ----------

def test_factory_keeps_worthy(monkeypatch):
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": True})
    judge = W.make_worthiness_judge(_Settings1())
    assert judge(_ev("a")) is True


def test_factory_drops_unworthy(monkeypatch):
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": False})
    judge = W.make_worthiness_judge(_Settings1())
    assert judge(_ev("a")) is False  # 不够格 → 踢


def test_factory_llm_failure_keeps(monkeypatch):
    # LLM 抛异常 → 留(fail-safe,与水货闸同:绝不因故障误杀真新闻)
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(W, "complete_json", boom)
    judge = W.make_worthiness_judge(_Settings1())
    assert judge(_ev("a")) is True


def test_factory_budget_exhausted_drops(monkeypatch):
    # 2026-07-01 纯优先改:成本闸到顶 → 未判尾部**丢弃**(fail-closed),不补成"够格"塞边缘货。
    # 注意与 test_factory_llm_failure_keeps 的区别:那是单条 LLM 失败→留(护真新闻),此处是预算耗尽→丢(护纯)。
    class _S(_Settings1):
        class rank:
            class event_pool:
                max_worthiness_judges_per_run = 0  # 立刻到顶,一票没投
                worthiness_judge_top_n = 25
                worthiness_judge_votes = 3

        class threads:
            judge_model = "x"
            judge_max_tokens = 2048
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: {"worthy": True})  # 即便本会判够格
    judge = W.make_worthiness_judge(_S())
    assert judge(_ev("a")) is False  # 预算耗尽、0 票 → 丢(fail-closed,宁少报不放水)


def test_majority_split_drops(monkeypatch):
    # worthy / unworthy / unworthy → 够格票 1 < 多数 2 → 踢
    seq = iter([{"worthy": True}, {"worthy": False}, {"worthy": False}])
    monkeypatch.setattr(W, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(W, "parse_json", lambda _s: next(seq))
    judge = W.make_worthiness_judge(_Settings())  # votes=3
    assert judge(_ev("a")) is False


def test_filter_unworthy_none_is_noop():
    evs = [_ev("a"), _ev("b")]
    assert W.filter_unworthy(evs, None, top_n=25, final_limit=10) is evs


def test_filter_unworthy_removes_dropped():
    keep = _ev("keep")
    drop = _ev("drop")
    # 假 judge:headline=="drop" 的判不够格
    def judge(e):
        return e["rep_item_id"] != "drop"
    out = W.filter_unworthy([keep, drop], judge, top_n=25, final_limit=10)
    assert keep in out and drop not in out


def test_filter_unworthy_drops_unjudged_tail():
    # 闭 codex MEDIUM1:超出 effective_top_n 的低热尾部不判、一律不返回(纯,不靠尾部凑数)
    evs = [_ev(f"e{i}", heat=float(100 - i)) for i in range(40)]  # 40 条,heat 降序
    out = W.filter_unworthy(evs, lambda e: True, top_n=25, final_limit=10)  # effective=max(25,15)=25
    assert len(out) == 25  # 只返回判过的头部 25,尾部 15 条丢掉
    assert all(e in evs[:25] for e in out)  # 全来自 heat 头部
    assert evs[39] not in out  # 最低热尾部条目不在


def test_filter_unworthy_keeps_on_judge_exception():
    # 闭 codex MINOR:judge 抛异常 → 留(fail-safe,不拖垮选稿/不误杀)
    def boom(e):
        raise RuntimeError("judge crashed")
    evs = [_ev("a"), _ev("b")]
    out = W.filter_unworthy(evs, boom, top_n=25, final_limit=10)
    assert set(id(e) for e in out) == set(id(e) for e in evs)  # 全留
