"""微信交付(Server酱,best-effort):推日报概述 + 要点到个人微信。

Server酱 sct:POST https://sctapi.ftqq.com/<token>.send,title + desp(markdown)。
图需公网 URL(本地 Mac 无)→ v1 推文字摘要 + 原文链接,best_effort(超免费额度/失败靠飞书+App 兜底)。
缺 token(env PULSEWIRE_SERVERCHAN_TOKEN)→ skipped。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import ChannelResult, DeliverPayload

if TYPE_CHECKING:
    from pulsewire.config import Settings

_SCT_URL = "https://sctapi.ftqq.com/{token}.send"


def _build_markdown(payload: DeliverPayload) -> str:
    lines = []
    if payload.digest:
        lines.append(payload.digest + "\n")
    for i, it in enumerate(payload.items, 1):
        review = " `待核实`" if it.get("needs_review") else ""
        lines.append(f"**{i:02d}. {it['headline']}**{review}")
        lines.append(f"{it['tldr']}")
        lines.append(f"_{it['source']}_ · [原文]({it['url']})\n")
    return "\n".join(lines)


async def send(payload: DeliverPayload, settings: Settings) -> ChannelResult:
    token = settings.serverchan_token
    if not token:
        return ChannelResult("wechat", "skipped", "未配置 PULSEWIRE_SERVERCHAN_TOKEN(.env)")
    title = f"pulsewire · {payload.title} · {payload.date_str}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _SCT_URL.format(token=token),
                data={"title": title[:100], "desp": _build_markdown(payload)},
            )
        data = resp.json()
    except Exception as exc:
        # best-effort:失败记为 failed(不假装发成功),靠飞书 + App 兜底
        return ChannelResult("wechat", "failed", f"请求失败:{exc}")
    if data.get("code") == 0:
        return ChannelResult("wechat", "sent")
    # 频率限制等:best-effort 跳过(不当致命)
    return ChannelResult("wechat", "failed", f"Server酱返回:{str(data)[:160]}")
