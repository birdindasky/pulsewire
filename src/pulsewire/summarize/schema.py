"""总结的结构化契约 + "数字占位"机制(数字 0 编造的核心)。

关键设计:模型**不写裸数字**,只能用 `{Fn}` 占位符引用我们喂给它的事实(来自 `facts.enriched`,
带 source_id)。最终文本里的数字由 verify 用库里的真实值替换占位符产生——模型无从编造。
- FactToken : 一个可被引用的事实(token=F1.. → item_id + source_id + label + value)。
- ItemSummary / DigestOutput : 模型必须产出的 JSON 形状(parse 后用 pydantic 校验)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

# 占位符:{F<编号>},如 {F1}。只允许引用我们给出的 token。
# 容忍花括号内空格({ F13 }):推理模型偶尔吐带空格变体,不放宽就既不替换也不标待核实、原样漏给用户
# (2026-06-16 eval 实锤:GitHub 榜 6 条 "{ F13 }颗星" 裸漏)。
TOKEN_RE = re.compile(r"\{\s*(F\d+)\s*\}")
# 裸数字探测:连续数字(可带千分位/小数/百分号),用于 verify 兜底"是不是偷偷写了无来源数字"。
BARE_NUMBER_RE = re.compile(r"(?<![\w{])\d[\d,.]*%?")


@dataclass(slots=True)
class FactToken:
    """喂给模型的一个可引用事实。"""

    token: str  # F1, F2, ...
    item_id: str
    source_id: str
    label: str
    value: object  # 真实数字(来自 facts.enriched)
    unit: str = ""


class ItemSummary(BaseModel):
    item_id: str
    headline: str = Field(min_length=1)  # 带信息钩子的中文短标题
    tldr: str = Field(min_length=1)  # 一句话速读(给只想扫一眼的人);数字用 {Fn} 占位
    insight: str = Field(min_length=1)  # 详细白话解读(术语打比方/背景/为何重要/接下来看什么);数字用 {Fn} 占位


class DigestOutput(BaseModel):
    """模型一次产出:全局概述 + 各条目总结。"""

    digest: str = Field(default="")  # 一段日报概述
    items: list[ItemSummary] = Field(default_factory=list)


def extract_tokens(text: str) -> list[str]:
    """取出文本里引用的 {Fn} token(去重保序)。"""
    seen: dict[str, None] = {}
    for m in TOKEN_RE.findall(text):
        seen.setdefault(m, None)
    return list(seen)
