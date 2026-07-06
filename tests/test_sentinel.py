"""交付哨兵(二⑥)测试:收据读写 + 缺失/陈旧/新鲜判定 + 告警触发。纯文件,不需 DB。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pulsewire.config import get_settings
from pulsewire.obs import sentinel


def _now(s) -> datetime:
    return datetime(2026, 6, 15, 8, 0, tzinfo=ZoneInfo(s.app.timezone))


def test_write_read_receipt_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "state")
    assert sentinel.read_receipt("feishu") is None  # 无收据 → None
    sentinel.write_receipt("feishu", "2026-06-15")
    assert sentinel.read_receipt("feishu") == "2026-06-15"


@pytest.mark.asyncio
async def test_sentinel_ok_when_receipt_is_today(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "state")
    alerts: list[dict] = []

    async def _rec(settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr(sentinel, "alert_delivery_missing", _rec)
    s = get_settings()
    sentinel.write_receipt("feishu", "2026-06-15")
    r = await sentinel.check_delivery_sentinel(s, now=_now(s))
    assert r["delivered"] is True and r["alerted"] is False and alerts == []


@pytest.mark.asyncio
async def test_sentinel_alerts_when_no_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sentinel, "_LOG_DIR", tmp_path / "logs")  # 无 run 日志 → 非"正在跑"
    alerts: list[dict] = []

    async def _rec(settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr(sentinel, "alert_delivery_missing", _rec)
    s = get_settings()
    r = await sentinel.check_delivery_sentinel(s, now=_now(s))  # 无收据
    assert r["delivered"] is False and r["alerted"] is True and r["pending"] is False
    assert len(alerts) == 1 and alerts[0]["date_str"] == "2026-06-15"


@pytest.mark.asyncio
async def test_sentinel_alerts_when_receipt_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sentinel, "_LOG_DIR", tmp_path / "logs")
    alerts: list[dict] = []

    async def _rec(settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr(sentinel, "alert_delivery_missing", _rec)
    s = get_settings()
    sentinel.write_receipt("feishu", "2026-06-14")  # 昨天的收据 = 今天没送达
    r = await sentinel.check_delivery_sentinel(s, now=_now(s))
    assert r["delivered"] is False and r["alerted"] is True
    assert alerts[0]["receipt_date"] == "2026-06-14"


@pytest.mark.asyncio
async def test_sentinel_pending_when_run_in_progress(tmp_path, monkeypatch):
    """机器睡过头唤醒后日报与哨兵同时触发:有 run 正在跑 → 这轮不报警(pending),防误报。"""
    monkeypatch.setattr(sentinel, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sentinel, "_run_recently_active", lambda *a, **k: True)
    alerts: list[dict] = []

    async def _rec(settings, **kw):
        alerts.append(kw)
        return {}

    monkeypatch.setattr(sentinel, "alert_delivery_missing", _rec)
    s = get_settings()
    r = await sentinel.check_delivery_sentinel(s, now=_now(s))  # 无收据但 run 在跑
    assert r["delivered"] is False and r["alerted"] is False and r["pending"] is True
    assert alerts == []  # 不误报
