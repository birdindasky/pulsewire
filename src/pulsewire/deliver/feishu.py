"""飞书交付(主渠道):incoming webhook 推日报概述 + 要点。

v1 走 webhook 文字/卡片(无需公网图床)。图卡(上传 image_key)需机器人应用凭证 + 公网图,
属后续/部署细节(本地 Mac 无公网 URL)——先用富文本把概述+要点+原文链接推出去。
缺 webhook(env PULSEWIRE_FEISHU_WEBHOOK)→ skipped,不假装发成功。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import ChannelResult, DeliverPayload

if TYPE_CHECKING:
    from pulsewire.config import Settings


def _build_card(payload: DeliverPayload) -> dict:
    """飞书 interactive 卡片:标题 + 概述 + 各要点(headline + 来源 + 原文链接)。"""
    elements: list[dict] = []
    if payload.digest:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": payload.digest}})
        elements.append({"tag": "hr"})
    for i, it in enumerate(payload.items, 1):
        review = " `待核实`" if it.get("needs_review") else ""
        line = (
            f"**{i:02d}. {it['headline']}**{review}\n"
            f"{it['tldr']}\n"
            f"<font color='grey'>{it['source']}</font> · [原文]({it['url']})"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"pulsewire · {payload.title} · {payload.date_str}"},
                "template": "wathet",
            },
            "elements": elements,
        },
    }


async def send(payload: DeliverPayload, settings: Settings) -> ChannelResult:
    webhook = settings.feishu_webhook
    if not webhook:
        return ChannelResult("feishu", "skipped", "未配置 PULSEWIRE_FEISHU_WEBHOOK(.env)")
    body = _build_card(payload)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook, json=body)
        data = resp.json()
    except Exception as exc:  # 网络/解析失败:如实冒泡为 failed,不假装发成功
        return ChannelResult("feishu", "failed", f"请求失败:{exc}")
    # 飞书 webhook 成功返回 code==0(或 StatusCode==0)
    if data.get("code") in (0, None) and data.get("StatusCode", 0) == 0:
        return ChannelResult("feishu", "sent")
    return ChannelResult("feishu", "failed", f"飞书返回异常:{str(data)[:160]}")
