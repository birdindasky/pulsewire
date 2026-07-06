"""内容领域分类(三③):按【内容】判条目属哪个领域,纠正"领域跟着源走"的错放。

问题:条目的领域现在继承自源(`source.domain`)——geo 源里夹的 AI 文章会进地缘日报,
Reddit 求职帖混进生物日报。这里让 LLM 看标题判"这条内容到底属 ai/bio/geo 还是都不属",
rank 据此把【确信不属本域】的条目从该域日报里剔掉(止损错放;不路由到正确域=v1 留待后续)。

省钱:只判**已选中要上报的条目**(每域约 final_limit 条,一次批量调用),不碰几百条召回池。
失败要冒泡:JSON 非法重试耗尽 → 抛,由调用方降级(保留全部,绝不静默丢内容)。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

    from .engine import Candidate

log = get_logger()

_SYSTEM = (
    "你是科技日报的内容分拣员。给定若干『领域』和若干『条目标题』,判断每条标题的内容"
    "**主要属于**哪个领域。只按内容判,不要被来源左右。\n"
    "- 明确属于某领域 → 输出该领域的 key;\n"
    "- 哪个领域都不沾(如招聘帖、纯八卦、与所有领域无关)→ 输出 \"other\";\n"
    "- 拿不准、模糊跨域 → 输出最贴近的那个领域 key(别轻易判 other,以免误删合理内容)。\n"
    "严格只输出 JSON。"
)


def _build_prompt(domains: list[tuple[str, str]], cands: list[Candidate]) -> str:
    lines = ["领域(key:描述):"]
    for key, interest in domains:
        lines.append(f"- {key}: {interest}")
    lines.append("\n条目(id: 标题):")
    for c in cands:
        lines.append(f"- {c.item_id}: {c.title}")
    lines.append(
        '\n输出 JSON,格式严格为:{"items": [{"id": "<item_id>", "domain": "<领域key或other>"}, ...]},'
        "为上面每个 id 各给一条,不要多不要少。"
    )
    return "\n".join(lines)


def _parse(content: str, valid_ids: set[str], valid_domains: set[str]) -> dict[str, str]:
    data = json.loads(content)
    rows = data.get("items")
    if not isinstance(rows, list):
        raise ValueError("分类输出缺少 items 数组")
    out: dict[str, str] = {}
    for row in rows:
        rid = row.get("id")
        dom = (row.get("domain") or "").strip()
        if rid in valid_ids:
            out[rid] = dom if dom in valid_domains else "other"
    if not out:
        raise ValueError("分类输出未匹配到任何候选 id")
    return out


def resolve_drops(
    kept_ids: list[str], verdict: dict[str, str], domain: str, max_drop_ratio: float
) -> tuple[set[str], bool]:
    """据分类结果定要剔除的 id(纯函数,好测)。

    不属本域(verdict != domain;缺判=按本域不剔)的剔掉;但要剔的比例 > max_drop_ratio
    视为分类器抽风 → 一个不剔(返回空集 + over=True),交调用方告警全留。返回 (drop_ids, over)。
    """
    drop = {iid for iid in kept_ids if verdict.get(iid, domain) != domain}
    if kept_ids and len(drop) > max_drop_ratio * len(kept_ids):
        return set(), True
    return drop, False


def classify_item_domains(
    cands: list[Candidate], domains: list[tuple[str, str]], settings: Settings
) -> dict[str, str]:
    """LLM 判每条候选的内容领域,返回 {item_id: domain_key|'other'}。失败重试耗尽则冒泡。"""
    import logging

    import litellm

    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)

    key = settings.resolve_deepseek_key()
    if not key:
        raise RuntimeError("content_classify 需 DeepSeek key(同 rerank);或把 rank.content_classify 关掉")

    valid_ids = {c.item_id for c in cands}
    valid_domains = {k for k, _ in domains}
    prompt = _build_prompt(domains, cands)
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}]
    model = f"deepseek/{settings.rank.classify_model}"

    last_err: Exception | None = None
    for attempt in range(settings.summarize.json_schema_retry + 1):
        try:
            resp = litellm.completion(
                model=model, messages=messages, api_key=key,
                response_format={"type": "json_object"}, temperature=0,
                # DeepSeek-v4 推理模型:留足 max_tokens(推理+整列 JSON)。验证发现 2048 对 ~20 条批量
                # 仍被截断("Unterminated string")→ 提到 4096;推理随条数涨,真不够再分批(2026-06-15)
                max_tokens=4096,
                timeout=settings.rank.request_timeout,  # 同精排口径,防卡死
            )
            return _parse(resp["choices"][0]["message"]["content"], valid_ids, valid_domains)
        except Exception as exc:  # 解析/网络失败 → 重试
            last_err = exc
            log.warning("rank.classify.retry", attempt=attempt, error=str(exc))

    raise RuntimeError(f"内容领域分类重试耗尽:{last_err}")
