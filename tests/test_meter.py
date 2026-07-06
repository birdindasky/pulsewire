"""LLM 计量器单元测试:累加分桶 / 防御读取(坏 response 不炸)/ 缓存命中 / reset / 线程安全。"""
from __future__ import annotations

import threading

import pytest

from pulsewire.obs import meter


class _Details:
    def __init__(self, cached):
        self.cached_tokens = cached


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, cached=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = _Details(cached) if cached is not None else None


class _Resp:
    def __init__(self, usage):
        self.usage = usage


def _resp(pt, ct, cached=None):
    return _Resp(_Usage(pt, ct, cached))


@pytest.fixture(autouse=True)
def _clean_meter():
    meter.reset_meter()
    yield
    meter.reset_meter()


def test_single_record_accumulates():
    meter.record_llm_usage("summarize", "deepseek-v4-pro", _resp(1000, 500, cached=200))
    snap = meter.meter_snapshot()
    assert snap["total_calls"] == 1
    assert snap["total_prompt_tokens"] == 1000
    assert snap["total_completion_tokens"] == 500
    assert snap["total_cached_tokens"] == 200
    assert snap["total_uncached_prompt_tokens"] == 800
    assert snap["total_tokens"] == 1500
    assert snap["cache_hit_rate"] == 0.2


def test_buckets_split_by_stage_and_model():
    meter.record_llm_usage("summarize", "deepseek-v4-pro", _resp(1000, 500))
    meter.record_llm_usage("summarize", "deepseek-v4-pro", _resp(1000, 500))
    meter.record_llm_usage("event_judge", "deepseek-v4-flash", _resp(300, 50))
    meter.record_llm_usage("subject", "deepseek-v4-flash", _resp(200, 30))
    snap = meter.meter_snapshot()
    by_key = {(r["stage"], r["model"]): r for r in snap["rows"]}
    assert by_key[("summarize", "deepseek-v4-pro")]["calls"] == 2
    assert by_key[("summarize", "deepseek-v4-pro")]["prompt_tokens"] == 2000
    assert by_key[("event_judge", "deepseek-v4-flash")]["calls"] == 1
    assert by_key[("subject", "deepseek-v4-flash")]["completion_tokens"] == 30
    assert snap["total_calls"] == 4
    assert len(snap["rows"]) == 3


def test_missing_usage_does_not_raise():
    # resp 没有 usage 属性
    meter.record_llm_usage("summarize", "m", object())
    # usage=None
    meter.record_llm_usage("summarize", "m", _Resp(None))
    # 完全是 None
    meter.record_llm_usage("summarize", "m", None)
    snap = meter.meter_snapshot()
    # 都记成一次调用,但 token 全 0(不报错才是关键)
    assert snap["total_calls"] == 3
    assert snap["total_prompt_tokens"] == 0
    assert snap["cache_hit_rate"] == 0.0


def test_missing_cache_details_treated_as_zero():
    meter.record_llm_usage("summarize", "m", _resp(1000, 500, cached=None))
    snap = meter.meter_snapshot()
    assert snap["total_cached_tokens"] == 0
    assert snap["total_uncached_prompt_tokens"] == 1000


def test_garbage_token_values_coerced_to_zero():
    meter.record_llm_usage("summarize", "m", _resp("oops", None, cached="bad"))
    snap = meter.meter_snapshot()
    assert snap["total_prompt_tokens"] == 0
    assert snap["total_completion_tokens"] == 0
    assert snap["total_cached_tokens"] == 0
    assert snap["total_calls"] == 1


def test_record_error():
    meter.record_llm_error("subject", "deepseek-v4-flash")
    meter.record_llm_error("subject", "deepseek-v4-flash")
    snap = meter.meter_snapshot()
    assert snap["total_errors"] == 2


def test_reset_clears():
    meter.record_llm_usage("summarize", "m", _resp(1000, 500))
    meter.reset_meter()
    snap = meter.meter_snapshot()
    assert snap["total_calls"] == 0
    assert snap["total_tokens"] == 0
    assert snap["rows"] == []


def test_thread_safe_concurrent_records():
    n_threads = 8
    per_thread = 200

    def worker():
        for _ in range(per_thread):
            meter.record_llm_usage("judge", "deepseek-v4-flash", _resp(10, 5, cached=2))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = meter.meter_snapshot()
    total = n_threads * per_thread
    assert snap["total_calls"] == total
    assert snap["total_prompt_tokens"] == total * 10
    assert snap["total_completion_tokens"] == total * 5
    assert snap["total_cached_tokens"] == total * 2
