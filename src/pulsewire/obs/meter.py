"""LLM token 计量:在 litellm 收口点累加每次调用的 usage,按 (stage, model) 归账。

为什么要这个:优化日报的"时长 + token"前先量基线(硬指标,别移动球门)。所有 LLM 请求
都收口在两个函数(summarize.backends._api_complete / threads.llm.complete_json),在那里
调 record_llm_usage 即全覆盖,**不改任何 LLM 逻辑**。

铁律:计量是观测增强,**绝不能拖垮主报**——record 内部任何异常一律静默吞掉(取不到 usage
字段、resp 形状异常都不报错)。线程安全:rank 判官经 asyncio.to_thread 在线程池并发调用,
故用 threading.Lock 串行化累加。

usage 字段走 litellm 标准(OpenAI 兼容):
- usage.prompt_tokens      全部输入 token(含缓存命中+未命中)
- usage.completion_tokens  输出 token
- usage.prompt_tokens_details.cached_tokens  缓存命中数(DeepSeek 服务端自动前缀缓存)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _Bucket:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0  # 缓存命中(已自动打折的输入 token);省钱看这个
    errors: int = 0


_lock = threading.Lock()
_buckets: dict[tuple[str, str], _Bucket] = {}


def reset_meter() -> None:
    """清零(每次 run 开跑前调,免跨 run 累加污染基线)。"""
    with _lock:
        _buckets.clear()


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def record_llm_usage(stage: str, model: str, resp: object) -> None:
    """从 litellm completion response 抓 usage 累加到 (stage, model) 桶。

    防御读取:resp 没有 usage / 没有 prompt_tokens_details 都不报错(取 0)。
    内部任何异常一律静默吞掉——计量绝不拖垮主报。
    """
    try:
        usage = getattr(resp, "usage", None)
        pt = _to_int(getattr(usage, "prompt_tokens", 0))
        ct = _to_int(getattr(usage, "completion_tokens", 0))
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _to_int(getattr(details, "cached_tokens", 0)) if details is not None else 0
        with _lock:
            b = _buckets.setdefault((stage, model), _Bucket())
            b.calls += 1
            b.prompt_tokens += pt
            b.completion_tokens += ct
            b.cached_tokens += cached
    except Exception:  # noqa: BLE001 — 计量绝不拖垮主报
        pass


def record_llm_error(stage: str, model: str) -> None:
    """记一次 LLM 调用失败(可选;给基线看失败率)。同样绝不抛。"""
    try:
        with _lock:
            _buckets.setdefault((stage, model), _Bucket()).errors += 1
    except Exception:  # noqa: BLE001
        pass


def meter_snapshot() -> dict:
    """返回当前累计快照:按 (stage, model) 明细 + 总计。供 run 收尾打总账。"""
    with _lock:
        rows = []
        tp = tc = tcached = tcalls = terr = 0
        for (stage, model), b in sorted(_buckets.items()):
            rows.append({
                "stage": stage, "model": model, "calls": b.calls,
                "prompt_tokens": b.prompt_tokens, "completion_tokens": b.completion_tokens,
                "cached_tokens": b.cached_tokens, "errors": b.errors,
            })
            tp += b.prompt_tokens
            tc += b.completion_tokens
            tcached += b.cached_tokens
            tcalls += b.calls
            terr += b.errors
    pt_billable = tp - tcached  # 缓存命中部分已打折;未命中才是大头计费输入
    return {
        "rows": rows,
        "total_calls": tcalls,
        "total_prompt_tokens": tp,
        "total_completion_tokens": tc,
        "total_cached_tokens": tcached,
        "total_uncached_prompt_tokens": max(pt_billable, 0),
        "total_tokens": tp + tc,
        "total_errors": terr,
        "cache_hit_rate": round(tcached / tp, 4) if tp else 0.0,
    }
