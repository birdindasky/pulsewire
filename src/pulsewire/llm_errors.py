"""LLM 调用错误分类:区分「永久错」与「瞬时错」。

2026-07-02 E1 根因:余额烧干(DeepSeek 返 "Insufficient Balance")被各判官的 except
吞成 fail-safe 放行 → 出零把关的毒日报;summarize 把它当"JSON 没写好"追加提示重试 3 轮。

对策:在 LLM 入口把「重试也没用」的错(没钱 / 凭证失效 / 权限)分类成 PermanentLLMError。
- 判官的 except **不得**把 PermanentLLMError 吞成 fail-safe 票 → 让它穿透、熔断整跑;
- 重试循环遇 PermanentLLMError 立即停,不浪费重试;
- 瞬时错(超时 / 限流 / 网络抖 / 偶发 5xx)照旧重试 / fail-safe,零回归。

分类保守:只把「确定是钱 / 凭证 / 权限」拦成永久;拿不准返回 None(当瞬时处理)。
"""

from __future__ import annotations

# DeepSeek 余额不足经 litellm 包成 BadRequestError(status 400),靠文案识别;
# 其余家的配额 / key 失效文案一并收。全部小写子串匹配。
_PERMANENT_TEXT_MARKERS = (
    "insufficient balance",   # DeepSeek 余额烧干(E1 原文)
    "insufficient_quota",     # OpenAI 系配额尽
    "exceeded your current quota",
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "no api key",
    "authentication",         # 认证失败
    "unauthorized",
)


class PermanentLLMError(Exception):
    """重试也没用的 LLM 错误(余额不足 / 凭证失效 / 权限)。

    必须熔断整跑:判官绝不得把它吞成 fail-safe 票,重试循环遇它立即停。

    ⚠️ 刻意**继承 Exception 而非 RuntimeError**:代码里有 `except RuntimeError` 兜块失败
    (summarize/engine.py _do_chunk),若继承 RuntimeError 会被它静默吞回去 → 穿透失效、
    写稿阶段照样丢块发毒稿(2026-07-03 独立考官逮到的假绿灯根因)。继承 Exception 从根上
    免疫所有 `except RuntimeError`;各 fail-safe 的 `except Exception` 已单独加了本类穿透。
    """


def classify_llm_error(exc: BaseException) -> PermanentLLMError | None:
    """把异常判成永久错(返回 PermanentLLMError)或 None(瞬时 / 未知,照旧重试 / fail-safe)。

    判据:HTTP 401/402/403(认证 / 计费 / 权限),或错误文案含余额 / 配额 / 凭证标记。
    保守:400 等其它状态只有文案命中标记才算永久;否则一律 None。
    """
    if isinstance(exc, PermanentLLMError):
        return exc
    status = getattr(exc, "status_code", None)
    if status in (401, 402, 403):
        return PermanentLLMError(str(exc))
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_TEXT_MARKERS):
        return PermanentLLMError(str(exc))
    return None
