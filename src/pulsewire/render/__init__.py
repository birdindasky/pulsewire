"""render — Jinja2 → 无头 Chrome 截图 → 「暖白+软黑+粉」便签风 PNG。[阶段 6]

每兴趣出两张:详读长图(headline+tldr+insight)+ 速读卡(tldr 清单)。
只渲染 verify 对账后的成稿(*_rendered);needs_review 标徽标。
- templates : Jinja2 模板 + 视觉(迁移自用户自研资产,品牌改 pulsewire)。
- engine    : run_render —— 取总结/概述 → 拼 HTML → 截两张全页长图。
"""

from __future__ import annotations

from .engine import run_render
from .templates import build_html, build_midview_html, build_overview_html

__all__ = ["build_html", "build_midview_html", "build_overview_html", "run_render"]
