"""summarize — 统一总结引擎,JSON Schema 结构化输出;每条论断带 value + source_id。

可切换后端(config.summarize.backend):
- api : litellm 按 token 调(DeepSeek / Opus 4.8 / GPT-5.5;可分层——粗活 DeepSeek、最终合成换强模型)
- cli : 调本地登录的 claude -p / codex exec(走订阅,灰色风险自负)

三道笼子(无论哪个后端都成立):
1. LLM 只当"一站"——cli 用 -p + 禁用所有工具,只产文本,不当全包 agent。
2. 产物照常进 verify 做数字回源对账 → "0 编造"与用哪个大脑无关。
3. cli 失败/限流/超时/非法JSON → 按 cli_fallback_to_api 回退 DeepSeek API,绝不开天窗。
[阶段 5]
- schema   : 结构化契约 + {Fn} 数字占位机制(数字 0 编造的核心)。
- backends : api(litellm/DeepSeek)/ cli(claude/codex) 后端 + JSON 容错解析。
- engine   : run_summarize —— 召回精排结果 → 提示 → 校验 → verify 对账 → 落库。
"""

from __future__ import annotations

from .engine import run_summarize
from .schema import DigestOutput, FactToken, ItemSummary

__all__ = ["DigestOutput", "FactToken", "ItemSummary", "run_summarize"]
