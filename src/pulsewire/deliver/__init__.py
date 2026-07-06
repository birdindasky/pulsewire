"""deliver — 渠道抽象(各 Sender):微信(Server酱)/ 飞书图卡 / 网页App。

投递幂等键 = cluster_id(=interest_key:date) + channel + trigger_type。[阶段 6]
- base   : DeliverPayload / ChannelResult 契约。
- webapp : 零后端,写 data.json + 轻壳 + 放图(本地主看面)。
- feishu : incoming webhook 推概述+要点(主渠道)。
- wechat : Server酱 best-effort 推文字摘要。
- engine : run_deliver —— 各渠道编排 + 投递幂等。
"""

from __future__ import annotations

from .base import ChannelResult, DeliverPayload
from .engine import run_deliver

__all__ = ["ChannelResult", "DeliverPayload", "run_deliver"]
