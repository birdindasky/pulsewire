"""verify — 结构化对账:按 source_id 核对数字,核不上→标 [待核实] 或不显示。

取代旧版正则挖空;JSON Schema 失败走降级。[阶段 5]
- engine : 占位替换 + 来源核对 + 裸数字探测 + 高风险定性断言闸门(verify_item / VerifiedItem)。
"""

from __future__ import annotations

from .engine import (
    NEEDS_REVIEW,
    OK,
    VerifiedItem,
    detect_risky_claims,
    scrub_residual_markup,
    scrub_unsourced_numbers,
    verify_item,
)

__all__ = [
    "NEEDS_REVIEW",
    "OK",
    "VerifiedItem",
    "detect_risky_claims",
    "scrub_residual_markup",
    "scrub_unsourced_numbers",
    "verify_item",
]
