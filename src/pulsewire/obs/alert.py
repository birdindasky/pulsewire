"""失败告警:流水线某阶段失败时,复用飞书 webhook + Server酱推纯文本告警。

铁律:失败要冒泡 + 记录 + 多通道告警,绝不静默产空日报。
- 告警走纯文本(与日报卡片不同形状),不依赖 DeliverPayload。
- 告警本身 best-effort:发不出去也不再抛(否则告警失败盖住原始失败),但会记日志。
- 未配 webhook / token → 该渠道 skipped,不假装发成功。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

_SCT_URL = "https://sctapi.ftqq.com/{token}.send"


async def _alert_feishu(text: str, settings: Settings) -> str:
    # 首选:复用天天推日报图那条"活"的自建应用通道(app_id/secret/openid 已在 .env 配好)。
    # 2026-07-02 教训:告警此前只认从未配置的 feishu_webhook → 上线以来所有失败告警全 skipped,
    # 系统躺了两天零通知。缺的不是密钥是接线,这里把告警接到真能发的通道。
    if settings.feishu_app_id and settings.feishu_app_secret and settings.feishu_user_openid:
        from pulsewire.deliver.feishu_app import send_text
        try:  # 纵深防御:告警自身绝不抛(即便将来被裸调,也不盖住原始故障)
            return "sent" if await send_text(text, settings) else "failed:feishu_app 发送失败"
        except Exception as exc:  # noqa: BLE001
            return f"failed:{exc}"
    # 回退:老的自定义机器人 webhook(若配了)
    webhook = settings.feishu_webhook
    if not webhook:
        return "skipped"
    body = {"msg_type": "text", "content": {"text": text}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook, json=body)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — 告警发送失败不抛(别盖住原始故障)
        return f"failed:{exc}"
    # 严格判定成功(webhook 正常返回 {"code":0} 或 {"StatusCode":0});缺字段/畸形响应=failed,
    # 不再把"缺 code"当成功(旧 `code in (0, None)` 是 fail-open,URL 配错也记 sent)。
    if data.get("code") == 0 or data.get("StatusCode") == 0:
        return "sent"
    return f"failed:{str(data)[:120]}"


async def _alert_wechat(title: str, text: str, settings: Settings) -> str:
    token = settings.serverchan_token
    if not token:
        return "skipped"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _SCT_URL.format(token=token), data={"title": title[:100], "desp": text}
        )
    data = resp.json()
    if data.get("code") == 0:
        return "sent"
    return f"failed:{str(data)[:120]}"


async def _fan_out(settings: Settings, *, title: str, text: str, **log_ctx) -> dict[str, str]:
    """把一条文本告警扇出到飞书 + Server酱。本身不抛(best-effort),返回 {channel: status}。"""
    log = get_logger()
    results: dict[str, str] = {}
    for channel, coro in (
        ("feishu", _alert_feishu(text, settings)),
        ("wechat", _alert_wechat(title, text, settings)),
    ):
        try:
            results[channel] = await coro
        except Exception as exc:  # 告警失败不再抛,只记日志(别盖住原始失败)
            results[channel] = f"failed:{exc}"
            log.warning("alert.channel.failed", channel=channel, error=str(exc))
    log.info("alert.sent", **log_ctx, **{f"alert_{k}": v for k, v in results.items()})
    return results


async def alert_failure(
    settings: Settings,
    *,
    run_id: str,
    stage: str,
    error: str,
    error_type: str = "",
) -> dict[str, str]:
    """流水线失败时多通道告警。返回 {channel: status};本身不抛(best-effort)。"""
    title = f"⚠️ pulsewire run 失败 · {stage}"
    text = (
        f"{title}\n"
        f"run_id: {run_id}\n"
        f"阶段: {stage}\n"
        f"错误: {error_type}{(' — ' if error_type else '')}{error}\n"
        f"(各站幂等,修复后 `pulsewire run` 同 run_id 会从此阶段断点续跑)"
    )
    return await _fan_out(settings, title=title, text=text, run_id=run_id, stage=stage)


async def alert_delivery_missing(
    settings: Settings, *, date_str: str, channel: str = "feishu", receipt_date: str | None = None,
) -> dict[str, str]:
    """交付哨兵:今天没查到日报送达 → 多通道告警(2026-06-15 二⑥)。本身不抛(best-effort)。"""
    title = f"🚨 pulsewire 日报哨兵 · {date_str} 未送达"
    text = (
        f"{title}\n"
        f"截至现在,今天没有 {channel} 日报送达记录(最近一次收据日期:{receipt_date or '无'})。\n"
        f"可能:06:00 launchd 没触发 / Docker 没起 / 机器还睡着 / 主流程崩了且告警也没发出。\n"
        f"排查:看 deploy/logs/run_*.log;`pulsewire run` 从检查点续跑补救。"
    )
    return await _fan_out(settings, title=title, text=text, sentinel=channel, date=date_str)
