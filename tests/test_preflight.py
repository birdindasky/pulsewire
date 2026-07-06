"""开跑前余额预检(preflight.py)纯逻辑测试 —— 无需数据库。

编排层"余额低就整跑中止"的集成测试在 test_pipeline.py(需数据库)。
check_deepseek_balance 真函数体的 fail-open 覆盖在本文件下半(独立考官指出这段最高危却零覆盖)。
"""

from __future__ import annotations

import pytest

from pulsewire.preflight import balance_below_floor, check_deepseek_balance


def test_below_floor_when_known_and_low():
    assert balance_below_floor(1.0, 5.0) is True
    assert balance_below_floor(0.0, 5.0) is True


def test_not_below_when_at_or_above_floor():
    assert balance_below_floor(5.0, 5.0) is False  # 恰好等于阈值 = 放行(严格小于才拦)
    assert balance_below_floor(99.47, 5.0) is False


def test_unknown_balance_passes():
    # 查不到余额(None)= 放行,绝不因预检本身故障挡掉正常跑
    assert balance_below_floor(None, 5.0) is False


def test_zero_floor_never_blocks_positive_balance():
    assert balance_below_floor(0.01, 0.0) is False
    assert balance_below_floor(None, 0.0) is False


# ---- check_deepseek_balance 真函数体:fail-open 覆盖(最高危段,防未来回归)---- #
class _FakeSettings:
    def __init__(self, key):
        self._key = key

    def resolve_deepseek_key(self):
        return self._key


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _fake_client(data=None, exc=None):
    """伪造 httpx.AsyncClient:get 返回 data 或抛 exc。"""
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if exc is not None:
                raise exc
            return _Resp(data)

    return lambda *a, **k: _C()


@pytest.mark.asyncio
async def test_check_balance_no_key_returns_none():
    # 取不到 key → None(放行),连网络都不碰
    assert await check_deepseek_balance(_FakeSettings(None)) is None


@pytest.mark.asyncio
async def test_check_balance_parses_string_total(monkeypatch):
    # DeepSeek 真接口 total_balance 是字符串,须正确解析成 float
    import pulsewire.preflight as pf
    monkeypatch.setattr(pf.httpx, "AsyncClient", _fake_client(
        {"balance_infos": [{"currency": "CNY", "total_balance": "110.00"}]}))
    assert await pf.check_deepseek_balance(_FakeSettings("k")) == 110.0


@pytest.mark.asyncio
async def test_check_balance_http_error_fails_open(monkeypatch):
    # 网络炸 → None(放行),绝不抛、绝不误挡正常日报(正是 2026-07-02 那类 except 吞异常代码)
    import httpx

    import pulsewire.preflight as pf
    monkeypatch.setattr(pf.httpx, "AsyncClient", _fake_client(exc=httpx.ConnectError("boom")))
    assert await pf.check_deepseek_balance(_FakeSettings("k")) is None


@pytest.mark.asyncio
async def test_check_balance_malformed_fails_open(monkeypatch):
    # 畸形响应(缺字段/空/非数字)一律 fail-open 成 None
    import pulsewire.preflight as pf
    for data in ({}, {"balance_infos": []}, {"balance_infos": [{}]},
                 {"balance_infos": [{"total_balance": "not-a-number"}]}):
        monkeypatch.setattr(pf.httpx, "AsyncClient", _fake_client(data))
        assert await pf.check_deepseek_balance(_FakeSettings("k")) is None
