"""交付哨兵:「日报今天到底来没来」的第二道独立保险(2026-06-15 二⑥)。

主流程的失败告警依赖主进程活着——进程崩 / 机器睡 / launchd 没触发,就没人知道日报没来。
哨兵是一个独立的 launchd 任务,在日报该到之后(默认 07:30)只读一个轻量收据文件判断:
- deliver 成功推飞书时,write_receipt 记下当天本地日期;
- 哨兵 read_receipt,日期 != 今天 → alert_delivery_missing 多通道告警。

为什么用文件不用查 DB:日报跑完 run_daily.sh 会退出 Docker Desktop,哨兵 07:30 不该再去
拉起 Docker 查库(重、且本身可能失败)。收据是普通文件,读它不依赖 Docker/postgres,最稳最轻。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pulsewire.config import PROJECT_ROOT
from pulsewire.obs import get_logger
from pulsewire.obs.alert import alert_delivery_missing

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_STATE_DIR = PROJECT_ROOT / "deploy" / "state"
_LOG_DIR = PROJECT_ROOT / "deploy" / "logs"
# 机器睡过头时,06:00 日报与哨兵会在唤醒后同时触发;哨兵可能抢在日报送达前跑。
# 最近的 run 日志若在此窗口内仍被写 = 日报很可能正在跑,这轮先不报警(防误报)。
_RUN_ACTIVE_WINDOW_MIN = 25


def _receipt_path(channel: str) -> Path:
    return _STATE_DIR / f"last_delivery_{channel}"


def write_receipt(channel: str, date_str: str) -> None:
    """记下某渠道最近一次成功交付的本地日期(deliver 调;best-effort,失败只记日志不抛)。"""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _receipt_path(channel).write_text(date_str, encoding="utf-8")
    except OSError as exc:  # 收据写失败不能拖垮交付本身
        log.warning("sentinel.receipt.write_failed", channel=channel, error=str(exc))


def read_receipt(channel: str) -> str | None:
    """读某渠道最近一次成功交付的本地日期;无收据/读不到 → None。"""
    try:
        return _receipt_path(channel).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _run_recently_active(within_min: int = _RUN_ACTIVE_WINDOW_MIN) -> bool:
    """最近的 run 日志是否在 within_min 分钟内仍被写(=日报可能正在跑,哨兵先别误报)。只看文件 mtime。"""
    try:
        logs = list(_LOG_DIR.glob("run_*.log"))
    except OSError:
        return False
    if not logs:
        return False
    newest = max(p.stat().st_mtime for p in logs)
    return (time.time() - newest) / 60.0 < within_min


async def check_delivery_sentinel(
    settings: Settings, *, channel: str = "feishu", now: datetime | None = None
) -> dict:
    """查某渠道今天有没有送达(读收据文件);没有且没有正在跑的 run → 多通道告警。

    返回 {delivered, today, receipt_date, alerted, pending}。只读文件 + 发告警,不碰 DB/Docker。
    pending=True:今天还没送达,但有 run 正在跑(机器睡过头唤醒后日报与哨兵同时触发),这轮先不报。
    """
    tz = ZoneInfo(settings.app.timezone)
    today = (now.astimezone(tz) if now else datetime.now(tz)).strftime("%Y-%m-%d")
    receipt = read_receipt(channel)
    if receipt == today:
        log.info("sentinel.ok", channel=channel, date=today)
        return {"delivered": True, "today": today, "receipt_date": receipt,
                "alerted": False, "pending": False}
    if _run_recently_active():
        log.info("sentinel.pending", channel=channel, today=today, note="run 正在跑,先不报警")
        return {"delivered": False, "today": today, "receipt_date": receipt,
                "alerted": False, "pending": True}
    log.error("sentinel.missing", channel=channel, today=today, receipt_date=receipt)
    await alert_delivery_missing(settings, date_str=today, channel=channel, receipt_date=receipt)
    return {"delivered": False, "today": today, "receipt_date": receipt,
            "alerted": True, "pending": False}
