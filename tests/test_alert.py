"""失败告警通道(obs/alert.py)测试 —— 2026-07-02 E2 回归。

核心:告警必须走那条"活"的自建应用通道(app_id/secret/openid),而不是从未配置的
feishu_webhook;且成功判定要严格(畸形响应 = failed,不再把"缺 code"当发成功)。
"""

from __future__ import annotations

import pytest

from pulsewire.config import get_settings
from pulsewire.obs.alert import _alert_feishu


def _settings(**over):
    return get_settings().model_copy(update=over)


@pytest.mark.asyncio
async def test_alert_prefers_feishu_app_over_webhook(monkeypatch):
    """配了自建应用三件套 → 走 app 通道(即便 webhook 也配了,也不碰它)。"""
    calls: list[str] = []

    async def _fake_send_text(text, settings):
        calls.append(text)
        return True

    monkeypatch.setattr("pulsewire.deliver.feishu_app.send_text", _fake_send_text)
    s = _settings(
        feishu_app_id="a", feishu_app_secret="b", feishu_user_openid="c",
        feishu_webhook="http://should-not-be-touched",
    )
    assert await _alert_feishu("boom", s) == "sent"
    assert calls == ["boom"]  # 确实走了 app 通道


@pytest.mark.asyncio
async def test_alert_reports_app_send_failure(monkeypatch):
    """app 通道发送失败 → 如实 failed,不假装 sent。"""
    async def _fail(text, settings):
        return False

    monkeypatch.setattr("pulsewire.deliver.feishu_app.send_text", _fail)
    s = _settings(feishu_app_id="a", feishu_app_secret="b", feishu_user_openid="c")
    assert (await _alert_feishu("boom", s)).startswith("failed")


@pytest.mark.asyncio
async def test_alert_app_exception_does_not_crash(monkeypatch):
    """纵深防御:app 通道内部抛异常 → _alert_feishu 吞成 failed,绝不冒泡带崩(告警不盖原始故障)。"""
    async def _boom(text, settings):
        raise RuntimeError("token endpoint down")

    monkeypatch.setattr("pulsewire.deliver.feishu_app.send_text", _boom)
    s = _settings(feishu_app_id="a", feishu_app_secret="b", feishu_user_openid="c")
    assert (await _alert_feishu("boom", s)).startswith("failed")


@pytest.mark.asyncio
async def test_alert_skipped_when_nothing_configured():
    """三件套和 webhook 都没配 → skipped(不假装发成功)。"""
    s = _settings(
        feishu_app_id=None, feishu_app_secret=None, feishu_user_openid=None, feishu_webhook=None,
    )
    assert await _alert_feishu("boom", s) == "skipped"


@pytest.mark.asyncio
async def test_webhook_fallback_strict_success_judgment(monkeypatch):
    """app 未配 → 回退 webhook;严格判定:缺 code 的畸形响应 = failed(旧 `code in (0,None)` 是 fail-open)。"""
    import pulsewire.obs.alert as alertmod

    class _Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    class _Client:
        def __init__(self, data):
            self._d = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp(self._d)

    def _factory(data):
        return lambda *a, **k: _Client(data)

    s = _settings(
        feishu_app_id=None, feishu_app_secret=None, feishu_user_openid=None,
        feishu_webhook="http://hook",
    )
    monkeypatch.setattr(alertmod.httpx, "AsyncClient", _factory({"code": 0}))
    assert await _alert_feishu("x", s) == "sent"
    # 畸形/空响应缺 code:必须 failed(旧版会误判 sent = URL 配错也记发出了)
    monkeypatch.setattr(alertmod.httpx, "AsyncClient", _factory({"garbage": 1}))
    assert (await _alert_feishu("x", s)).startswith("failed")
