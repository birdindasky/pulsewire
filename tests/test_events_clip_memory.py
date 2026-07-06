"""已剪记忆闸(clip_memory):账本对账 + novelty 判官 + 过滤器 + 增量写稿随行。

命门(全部对应模块 docstring 的铁律护栏):
- fail-open:判官故障/脏返回/预算耗尽 → 一律留(最坏=回到重复现状,绝不误杀真新闻);
- 重跑安全:linked_today 跳闸直接留(拿今天自己的稿自比必误杀);
- 材料全旧(stale_material)确定性踢,零 LLM;
- S1 缓存:键含前情(昨天又剪一天→自然失效重判);只缓存全程干净票。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pulsewire.events import clip_memory as C

_TZ = ZoneInfo("Asia/Shanghai")


class _Settings:
    class rank:
        class event_pool:
            max_novelty_judges_per_run = 99
            novelty_judge_top_n = 25
            novelty_judge_votes = 3
            clip_window_days = 14

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


class _Settings1(_Settings):
    """单票(votes=1)便于测基本判定。"""
    class rank:
        class event_pool:
            max_novelty_judges_per_run = 99
            novelty_judge_top_n = 25
            novelty_judge_votes = 1
            clip_window_days = 14

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


def _ev(item_id: str, *, heat: float = 1.0, prev: dict | None = None,
        peak: datetime | None = None) -> dict:
    e = {"rep_item_id": item_id, "heat_score": heat, "headline": f"头条 {item_id}",
         "snippet": "正文材料", "member_cluster_ids": [f"c_{item_id}"]}
    if peak is not None:
        e["peak_at"] = peak
    if prev is not None:
        e["prev_report"] = prev
    return e


def _prev(**kw) -> dict:
    base = {"thread_id": "thr_1", "days_prior": 2, "last_date": "2026-07-04",
            "prev_text": "昨天报过:X 公司发布 Y", "linked_today": False, "stale_material": False}
    base.update(kw)
    return base


# ---------- logical_date:run_id 逻辑日(f05 口径) ----------

def test_logical_date_from_run_id():
    assert C.logical_date("daily_20260705", _TZ) == "2026-07-05"


def test_logical_date_fallback_now():
    out = C.logical_date("custom-run", _TZ)
    assert len(out) == 10 and out[4] == "-"  # 解析不出 → 当下本地日期(格式即可,别赌钟点)


# ---------- annotate_prev_reports:标注 + 材料全旧判定 ----------

def test_annotate_marks_matched_event():
    ev = _ev("a", peak=datetime(2026, 7, 5, 3, 0, tzinfo=timezone.utc))
    ledger = {"c_a": _prev()}
    n = C.annotate_prev_reports([ev], ledger, tz=_TZ)
    assert n == 1
    assert ev["prev_report"]["days_prior"] == 2
    assert ev["prev_report"]["stale_material"] is False  # peak 晚于 07-04 00:00 → 交判官


def test_annotate_stale_material_when_peak_before_last_date():
    # 最新材料 07-03(早于上次已剪日 07-04 的零点)→ 材料全旧,确定性踢
    ev = _ev("a", peak=datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc))
    n = C.annotate_prev_reports([ev], {"c_a": _prev()}, tz=_TZ)
    assert n == 1
    assert ev["prev_report"]["stale_material"] is True


def test_annotate_no_peak_is_not_stale():
    # peak_at 缺失 → 不判旧(fail-open 交判官),绝不确定性误杀
    ev = _ev("a")
    C.annotate_prev_reports([ev], {"c_a": _prev()}, tz=_TZ)
    assert ev["prev_report"]["stale_material"] is False


def test_annotate_linked_today_never_stale():
    # 重跑:线今天已挂过 → 不做材料全旧判定(闸层直接留)
    ev = _ev("a", peak=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))
    C.annotate_prev_reports([ev], {"c_a": _prev(linked_today=True)}, tz=_TZ)
    assert ev["prev_report"]["stale_material"] is False


def test_annotate_picks_latest_thread_on_multi_hit():
    # 事件多个成员簇命中不同线 → 取最近已剪的那条(prev 最新才是读者刚看过的)
    ev = _ev("a")
    ev["member_cluster_ids"] = ["c1", "c2"]
    ledger = {"c1": _prev(last_date="2026-07-01", days_prior=1),
              "c2": _prev(last_date="2026-07-04", days_prior=3)}
    C.annotate_prev_reports([ev], ledger, tz=_TZ)
    assert ev["prev_report"]["last_date"] == "2026-07-04"
    assert ev["prev_report"]["days_prior"] == 3


def test_annotate_unmatched_untouched():
    ev = _ev("a")
    assert C.annotate_prev_reports([ev], {}, tz=_TZ) == 0
    assert "prev_report" not in ev


# ---------- judge_has_new:单票判定(脏值→留) ----------

def test_judge_new_true(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": True, "reason": "有裁决落地"})
    has_new, _ = C.judge_has_new("前情", "h", "b", _Settings())
    assert has_new is True


def test_judge_new_strict_false(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": False, "reason": "纯复述"})
    has_new, _ = C.judge_has_new("前情", "h", "b", _Settings())
    assert has_new is False


def test_judge_dirty_return_keeps(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {})
    assert C.judge_has_new("p", "h", "b", _Settings())[0] is True
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": "no"})  # 脏值非 False → 留
    assert C.judge_has_new("p", "h", "b", _Settings())[0] is True


# ---------- make_novelty_judge:工厂(默认留 + fail-open + 多数票 + 成本闸) ----------

def test_factory_drops_no_new_development(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": False})
    judge = C.make_novelty_judge(_Settings1())
    assert judge(_ev("a", prev=_prev())) is False


def test_factory_keeps_new_development(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": True})
    judge = C.make_novelty_judge(_Settings1())
    assert judge(_ev("a", prev=_prev())) is True


def test_factory_llm_failure_keeps(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(C, "complete_json", boom)
    judge = C.make_novelty_judge(_Settings1())
    assert judge(_ev("a", prev=_prev())) is True  # fail-open:故障绝不误杀


def test_factory_budget_exhausted_keeps(monkeypatch):
    # 🔴 与 worthiness 的 fail-closed 相反:novelty 预算耗尽 → 留(砍了=丢真新闻)
    class _S(_Settings1):
        class rank:
            class event_pool:
                max_novelty_judges_per_run = 0  # 立刻到顶
                novelty_judge_top_n = 25
                novelty_judge_votes = 3
                clip_window_days = 14

        class threads:
            judge_model = "x"
            judge_max_tokens = 2048
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": False})  # 即便本会判"无新进展"
    judge = C.make_novelty_judge(_S())
    assert judge(_ev("a", prev=_prev())) is True


def test_factory_majority_needed_to_drop(monkeypatch):
    # 票序 无新/有新/有新 → 踢票 1 < 多数 2 → 留(宁多报别误杀)
    seq = iter([{"new": False}, {"new": True}, {"new": True}])
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: next(seq))
    judge = C.make_novelty_judge(_Settings())
    assert judge(_ev("a", prev=_prev())) is True


def test_factory_majority_drop(monkeypatch):
    seq = iter([{"new": False}, {"new": False}])  # 提前停:2 票已够多数
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: next(seq))
    judge = C.make_novelty_judge(_Settings())
    assert judge(_ev("a", prev=_prev())) is False


# ---------- S1 判决缓存:命中不调 LLM;干净票才记;键含前情 ----------

def test_cache_hit_skips_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("缓存命中不许调 LLM")
    monkeypatch.setattr(C, "complete_json", boom)
    ev = _ev("a", prev=_prev())
    cache = {C.novelty_item_hash(ev): {"new": False}}
    judge = C.make_novelty_judge(_Settings(), judgment_cache=cache)
    assert judge(ev) is False  # 逐字复用裁决


def test_clean_verdict_recorded(monkeypatch):
    monkeypatch.setattr(C, "complete_json", lambda *a, **k: "x")
    monkeypatch.setattr(C, "parse_json", lambda _s: {"new": False})
    ev = _ev("a", prev=_prev())
    new_verdicts: list = []
    judge = C.make_novelty_judge(_Settings1(), new_verdicts=new_verdicts)
    judge(ev)
    assert len(new_verdicts) == 1
    row = new_verdicts[0]
    assert row["judge_name"] == "novelty"
    assert row["item_hash"] == C.novelty_item_hash(ev)
    assert row["verdict"] == {"new": False}


def test_dirty_vote_not_cached(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(C, "complete_json", boom)
    new_verdicts: list = []
    judge = C.make_novelty_judge(_Settings1(), new_verdicts=new_verdicts)
    judge(_ev("a", prev=_prev()))
    assert new_verdicts == []  # 故障兜底票不缓存(append-only 不自愈)


def test_item_hash_includes_prev_text():
    # 键含前情:昨天又剪了一天(prev 变)→ 裁决必须重判,不能复用旧键
    e1 = _ev("a", prev=_prev(prev_text="版本一"))
    e2 = _ev("a", prev=_prev(prev_text="版本二"))
    assert C.novelty_item_hash(e1) != C.novelty_item_hash(e2)


# ---------- filter_already_clipped:过滤器语义 ----------

def test_filter_none_judge_is_noop():
    evs = [_ev("a", prev=_prev(stale_material=True))]
    assert C.filter_already_clipped(evs, None, top_n=25, final_limit=20) == evs


def test_filter_fresh_events_pass_untouched():
    def judge(_e):
        raise AssertionError("无 prev_report 不许进判官")
    evs = [_ev("a"), _ev("b")]
    out = C.filter_already_clipped(evs, judge, top_n=25, final_limit=20)
    assert out == evs  # 原序保留


def test_filter_stale_material_dropped_without_llm():
    def judge(_e):
        raise AssertionError("材料全旧确定性踢,不许调判官")
    evs = [_ev("a", prev=_prev(stale_material=True)), _ev("b")]
    out = C.filter_already_clipped(evs, judge, top_n=25, final_limit=20)
    assert [e["rep_item_id"] for e in out] == ["b"]


def test_filter_linked_today_kept_without_llm():
    # 重跑安全:今天已挂线的事件跳闸直接留(自比必误杀)
    def judge(_e):
        raise AssertionError("linked_today 不许进判官")
    evs = [_ev("a", prev=_prev(linked_today=True))]
    out = C.filter_already_clipped(evs, judge, top_n=25, final_limit=20)
    assert out == evs


def test_filter_judge_decides_repeats(monkeypatch):
    evs = [_ev("a", prev=_prev()), _ev("b"), _ev("c", prev=_prev())]
    out = C.filter_already_clipped(
        evs, lambda e: e["rep_item_id"] != "c", top_n=25, final_limit=20)
    assert [e["rep_item_id"] for e in out] == ["a", "b"]


def test_filter_judge_exception_keeps():
    def judge(_e):
        raise RuntimeError("judge down")
    evs = [_ev("a", prev=_prev())]
    out = C.filter_already_clipped(evs, judge, top_n=25, final_limit=20)
    assert out == evs  # fail-open


def test_filter_beyond_head_kept_without_llm():
    # heat 头部外的既往事件不烧判官、原样留(本就进不了 final_limit)
    calls: list[str] = []

    def judge(e):
        calls.append(e["rep_item_id"])
        return True
    evs = [_ev(f"e{i}", heat=100 - i, prev=_prev()) for i in range(8)]
    out = C.filter_already_clipped(evs, judge, top_n=2, final_limit=1)
    assert len(out) == 8
    assert set(calls) == {"e0", "e1", "e2", "e3", "e4", "e5"}  # max(2, 1+5)=6 头部


# ---------- 账本查询(需库;连不上自动跳过) ----------

@pytest.mark.asyncio
async def test_load_clip_ledger_roundtrip(db_session):
    from pulsewire.store import create_thread, link_cluster_to_thread
    from pulsewire.store.tables import Cluster

    for cid in ("clp_c1", "clp_c2", "clp_c3"):
        db_session.add(Cluster(cluster_id=cid, first_item_id="i", source_count=1))
    await db_session.flush()
    now = datetime(2026, 7, 4, 7, 0, tzinfo=timezone.utc)
    await create_thread(db_session, thread_id="thr_clp", name="X 事件", subject="x",
                        domain="ik_test", summary="上次的稿:X 有了进展", seen_at=now, heat=1)
    await link_cluster_to_thread(
        db_session, thread_id="thr_clp", cluster_id="clp_c1", run_id=None, subject="x",
        link_reason="new", confidence=1.0, headline="第一天头条", url="u", source="s",
        progress_date="2026-07-03")
    await link_cluster_to_thread(
        db_session, thread_id="thr_clp", cluster_id="clp_c2", run_id=None, subject="x",
        link_reason="judge", confidence=0.9, headline="第二天头条", url="u", source="s",
        progress_date="2026-07-04")

    ledger = await C.load_clip_ledger(
        db_session, ["clp_c1", "clp_c2", "clp_c3"], today="2026-07-05", window_days=14)
    assert set(ledger) == {"clp_c1", "clp_c2"}  # c3 没挂过线,不进账本
    rec = ledger["clp_c1"]
    assert rec["days_prior"] == 2 and rec["last_date"] == "2026-07-04"
    assert rec["linked_today"] is False
    assert rec["prev_text"] == "上次的稿:X 有了进展"  # 线现状(未挂今天 → 可信)

    # 今天已挂线(重跑场景):linked_today=True,前情退回冻结的挂线痕 headline
    await link_cluster_to_thread(
        db_session, thread_id="thr_clp", cluster_id="clp_c3", run_id=None, subject="x",
        link_reason="judge", confidence=0.9, headline="第三天头条", url="u", source="s",
        progress_date="2026-07-05")
    ledger2 = await C.load_clip_ledger(
        db_session, ["clp_c1"], today="2026-07-05", window_days=14)
    rec2 = ledger2["clp_c1"]
    assert rec2["linked_today"] is True
    assert rec2["days_prior"] == 2  # 今天不计入既往
    assert rec2["prev_text"] == "第二天头条"  # summary 已被今天刷新风险 → 用冻结 headline


@pytest.mark.asyncio
async def test_load_clip_ledger_window_expiry(db_session):
    from pulsewire.store import create_thread, link_cluster_to_thread
    from pulsewire.store.tables import Cluster

    db_session.add(Cluster(cluster_id="clp_old", first_item_id="i", source_count=1))
    await db_session.flush()
    now = datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
    await create_thread(db_session, thread_id="thr_old", name="旧事", subject="o",
                        domain="ik_test", summary="旧稿", seen_at=now, heat=1)
    await link_cluster_to_thread(
        db_session, thread_id="thr_old", cluster_id="clp_old", run_id=None, subject="o",
        link_reason="new", confidence=1.0, headline="旧头条", url="u", source="s",
        progress_date="2026-06-01")
    # 出 14 天记忆窗:旧事重浮=当新事重报,不进账本
    ledger = await C.load_clip_ledger(
        db_session, ["clp_old"], today="2026-07-05", window_days=14)
    assert ledger == {}
