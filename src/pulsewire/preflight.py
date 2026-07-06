"""开跑前健康预检。

目前只有一道:**DeepSeek 余额闸**(治 2026-07-02 E1——余额烧干后判官全线 fail-open
出毒日报 + 每 5 分钟全量重试风暴)。开跑前查一次余额,确切低于阈值就不跑、发告警。

设计铁律:
- **best-effort 放行**:查不到余额(网络抖/接口变/无 key)→ 返回 None → 放行。绝不因预检
  本身故障挡掉正常跑(预检是安全网,不是新的单点)。
- **只拦确切过低**:只有"真查到余额 < 阈值"才拦。
- 拦下时不产任何内容、不推进检查点(由调用方 pipeline 写 failed + 告警)。
- 只读 GET,不改账户任何状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_BALANCE_URL = "https://api.deepseek.com/user/balance"


async def check_deepseek_balance(settings: Settings) -> float | None:
    """查 DeepSeek 账户余额,返回首个币种的 total_balance(float)。

    查不到(无 key / 网络失败 / 接口返回异常)一律返回 None = 放行。单位随账户币种
    (用户为 CNY),阈值语义须与之一致。
    """
    key = settings.resolve_deepseek_key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
        infos = resp.json().get("balance_infos") or []
        if not infos:
            return None
        return float(infos[0]["total_balance"])
    except Exception as exc:  # noqa: BLE001 — 预检 best-effort:查不到就放行,绝不挡正常跑
        log.warning("preflight.balance.unavailable", error=str(exc))
        return None


def balance_below_floor(balance: float | None, floor: float) -> bool:
    """确切查到余额且低于阈值才 True。None(查不到)= False = 放行。"""
    return balance is not None and balance < floor
