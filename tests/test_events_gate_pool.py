"""闸判官并发执行器(gate_pool)+ 工厂锁(2026-07-02 提速批)。

命门两条:
① map_judge 并发跑但**语义 = [judge(e) for e in head]**——保序、异常照冒泡、≤1 条不开池;
② 各判官工厂的成本闸计数/缓存加锁后,并发下 max_*_judges_per_run 仍是**精确硬顶**
   (锁丢了 → 并发 check-then-increment 竞态 → 超烧预算/失败静默漏,mutation 应红)。
"""
from __future__ import annotations

import random
import threading
import time

import pytest

from pulsewire.events import topic_judge as T
from pulsewire.events import worthiness_judge as W
from pulsewire.events.gate_pool import map_judge


# ---------- ① map_judge:保序 / 单条串行 / 异常冒泡 ----------

def test_map_judge_preserves_order_under_concurrency():
    # 每条随机小睡:若按完成序拼结果(错),顺序必乱;按 head 原序(对)则恒等
    head = list(range(23))

    def judge(x):
        time.sleep(random.uniform(0, 0.02))
        return x * 10

    assert map_judge(judge, head) == [x * 10 for x in head]


def test_map_judge_single_item_stays_serial():
    seen_threads: set = set()

    def judge(x):
        seen_threads.add(threading.current_thread().name)
        return x

    assert map_judge(judge, ["only"]) == ["only"]
    assert seen_threads == {threading.current_thread().name}  # 没开池,在调用线程里跑


def test_map_judge_empty():
    assert map_judge(lambda e: True, []) == []


def test_map_judge_exception_propagates():
    # 与串行版一致:judge 抛异常原样冒泡(fail-safe 兜底属于各闸自己的事,不在执行器里吞)
    def judge(x):
        if x == 2:
            raise RuntimeError("boom")
        return x

    with pytest.raises(RuntimeError):
        map_judge(judge, [1, 2, 3])


def test_map_judge_runs_concurrently():
    # 12 条各睡 0.05s:串行 ≥0.6s,5 路并发应 <0.4s(留宽裕防抖)
    def judge(x):
        time.sleep(0.05)
        return x

    t0 = time.monotonic()
    map_judge(judge, list(range(12)))
    assert time.monotonic() - t0 < 0.4


# ---------- ② 工厂锁:并发下成本闸仍是精确硬顶 ----------

class _SettingsCap:
    class rank:
        class event_pool:
            max_worthiness_judges_per_run = 3  # 故意配小,逼并发去撞闸
            worthiness_judge_top_n = 25
            worthiness_judge_votes = 1

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


def _ev(item_id: str, heat: float = 1.0) -> dict:
    return {"rep_item_id": item_id, "heat_score": heat, "headline": item_id, "snippet": "b"}


def test_worthiness_cap_exact_under_concurrent_filter(monkeypatch):
    """20 条并发过够格闸、预算只有 3:真 LLM 单判必须恰好 3 次(硬顶精确,绝不超烧)。

    去掉工厂锁(mutation)→ check-then-increment 竞态,多线程同时读到 calls<cap 全放行
    → 调用数 > cap,本测应红。慢桩(sleep)把竞态窗口撑大,竞态在≈每次运行都能撞上。
    """
    llm_calls = [0]
    count_lock = threading.Lock()

    def slow_worthy(headline, body, settings):
        with count_lock:
            llm_calls[0] += 1
        time.sleep(0.03)  # 撑大竞态窗口:无锁时多 worker 会同时越过成本闸检查
        return True, ""

    monkeypatch.setattr(W, "judge_is_worthy", slow_worthy)
    judge = W.make_worthiness_judge(_SettingsCap())
    kept = W.filter_unworthy([_ev(f"e{i}") for i in range(20)], judge,
                             top_n=25, final_limit=10)
    assert llm_calls[0] == 3, f"成本闸破了:预算 3,实调 {llm_calls[0]}"
    # 方向回归:判过且 worthy 的留(3 条),预算耗尽的尾部 fail-closed 踢(纯,宁少报)
    assert len(kept) == 3


class _SettingsTopicCap:
    class rank:
        class event_pool:
            max_topic_judges_per_run = 4
            topic_judge_top_n = 25
            topic_judge_votes = 1
            topic_portraits = {}

    class threads:
        judge_model = "x"
        judge_max_tokens = 2048


def test_topic_cap_exact_and_failsafe_keep_under_concurrency(monkeypatch):
    """话题闸同款:预算 4 并发撞闸 → 恰好 4 次真判;预算耗尽的一律 KEEP(绝不误杀)。"""
    llm_calls = [0]
    count_lock = threading.Lock()

    def slow_off(d, ev, settings, portrait=None):
        with count_lock:
            llm_calls[0] += 1
        time.sleep(0.03)
        return True, "off"  # 判过的全判跑题(votes=1 即踢)

    monkeypatch.setattr(T, "judge_off_topic", slow_off)
    from types import SimpleNamespace
    board = SimpleNamespace(key="ai", label="AI", interest="ai", tags=[])
    judge = T.make_topic_judge(_SettingsTopicCap())(board)
    kept = T.filter_off_topic([_ev(f"e{i}") for i in range(20)], judge,
                              top_n=25, final_limit=10)
    assert llm_calls[0] == 4, f"成本闸破了:预算 4,实调 {llm_calls[0]}"
    # 判过的 4 条被踢,预算耗尽的 16 条 KEEP(话题闸方向=宁错放别错杀)
    assert len(kept) == 16
