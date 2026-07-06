"""LLM 主编选题:让模型像日报主编一样给候选条目打"今日新闻价值"(0-1)。

只问"和兴趣相关吗"会选出一堆语义贴近的论文、漏掉当天头条——主编要综合:
重大性、多源热度(N 个源在报=大事)、一手优先、实质内容(压 meme/闲聊帖)、兴趣相关性。

- deepseek 后端:litellm 调 DeepSeek(需 `PULSEWIRE_DEEPSEEK_API_KEY`,走 .env)。
- LLM 只当"一站":只产结构化分数,**不让它编数字**(数字回源在 enrich 已挂 source_id)。
- 失败要冒泡:没 key / JSON 非法重试耗尽 → 抛错,绝不静默退化成"全 0 分"。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

    from .engine import Candidate

log = get_logger()

_SYSTEM = (
    "你是一份科技日报的主编,为今天的日报选稿。给每条候选打 0~1 的「今日价值分」,综合判断:\n"
    "1. 重大性:模型/产品发布、重要研究突破、公司与行业大事 > 常规更新、个人观点帖;\n"
    "2. 多源热度:标注「热度N源」表示近期有 N 个不同信息源在报道相似内容,N 大=今天的大事,必须给高分;\n"
    "3. 一手优先:官方公告、原始论文、当事人访谈 > 二手转述、社区讨论帖;\n"
    "4. 实质内容:标题应承载真实信息;梗图/晒图/闲聊/无实质的惊叹帖,即使话题相关也给低分(≤0.2);\n"
    "5. 与读者兴趣的相关性(给定兴趣只是读者画像,重大行业事件即使不完全对口也值得高分)。\n"
    "只依据标题与给定信息判断,不要编造任何事实或数字。严格只输出 JSON。"
)


def _age_label(published_at: datetime | None) -> str:
    if published_at is None:
        return "无日期"
    hours = (datetime.now(timezone.utc) - published_at).total_seconds() / 3600.0
    if hours < 0:
        hours = 0.0
    return f"{int(hours)}h前"


def _build_prompt(interest: str, tags: list[str], cands: list[Candidate]) -> str:
    lines = [f"读者兴趣:{interest}"]
    if tags:
        lines.append(f"标签:{', '.join(tags)}")
    lines.append("\n候选条目(id: [源|发布时间|热度] 标题):")
    for c in cands:
        heat = f"|热度{c.heat}源" if c.heat > 1 else ""
        lines.append(f"- {c.item_id}: [{c.source}|{_age_label(c.published_at)}{heat}] {c.title}")
    lines.append(
        '\n输出 JSON,格式严格为:{"scores": [{"id": "<item_id>", "relevance": <0~1 小数>}, ...]},'
        "为上面每个 id 各给一条,不要多不要少。"
    )
    return "\n".join(lines)


def _parse_scores(content: str, valid_ids: set[str]) -> dict[str, float]:
    data = json.loads(content)
    scores = data.get("scores")
    if not isinstance(scores, list):
        raise ValueError("LLM 输出缺少 scores 数组")
    out: dict[str, float] = {}
    for row in scores:
        rid = row.get("id")
        if rid in valid_ids:
            out[rid] = max(0.0, min(1.0, float(row.get("relevance", 0.0))))
    if not out:
        raise ValueError("LLM 输出未匹配到任何候选 id")
    return out


def llm_rerank(
    interest: str, tags: list[str], cands: list[Candidate], settings: Settings
) -> dict[str, float]:
    """调 DeepSeek 给候选打相关度,返回 {item_id: 0~1}。失败重试耗尽则冒泡。"""
    import logging

    import litellm

    litellm.suppress_debug_info = True  # 关掉 litellm 默认往 stdout 打的 provider 信息,保持结构化日志干净
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)

    key = settings.resolve_deepseek_key()
    if not key:
        # 失败要冒泡:没 key 不能静默退化(用户已选 deepseek 真精排)
        raise RuntimeError(
            "rerank_provider=deepseek 但取不到 DeepSeek key("
            "PULSEWIRE_DEEPSEEK_API_KEY / AI_API_KEY env / macOS Keychain service=AI_API_KEY 均空);"
            "请配置 key,或把 config.rank.rerank_provider 改为 rule(只用规则分)"
        )

    valid_ids = {c.item_id for c in cands}
    prompt = _build_prompt(interest, tags, cands)
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}]
    model = f"deepseek/{settings.rank.rerank_model}"

    last_err: Exception | None = None
    for attempt in range(settings.summarize.json_schema_retry + 1):
        try:
            resp = litellm.completion(
                model=model,
                messages=messages,
                api_key=key,
                response_format={"type": "json_object"},
                temperature=0,
                timeout=settings.rank.request_timeout,  # 防卡住连接无限挂死整条流水线;失败走外层 json_schema_retry 重试(2026-06-15 一②)
            )
            content = resp["choices"][0]["message"]["content"]
            return _parse_scores(content, valid_ids)
        except Exception as exc:  # 解析/网络失败 → 重试
            last_err = exc
            log.warning("rank.rerank.retry", attempt=attempt, error=str(exc))

    raise RuntimeError(f"LLM 精排重试耗尽:{last_err}")
