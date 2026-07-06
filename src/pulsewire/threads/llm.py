"""事件线共用的 DeepSeek JSON 调用(A 主体抽取 / B 判官共用)。

独立于 summarize 后端:归线是廉价结构化活,固定直连 DeepSeek API + 省档(flash),
不走 summarize 的 cli 路由。失败冒泡,由各调用方决定重试/降级。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


def complete_json(
    system: str, user: str, *, model: str, max_tokens: int, settings: Settings, stage: str = "llm"
) -> str:
    """直连 DeepSeek 出 JSON 文本(response_format=json_object,temperature=0)。

    stage:计量归账标签(subject / event_judge / select_dedup_judge / thread_judge)。
    """
    import logging

    import litellm

    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    key = settings.resolve_deepseek_key()
    if not key:
        raise RuntimeError(
            "事件线 LLM 取不到 DeepSeek key(PULSEWIRE_DEEPSEEK_API_KEY / AI_API_KEY / Keychain 均空)"
        )
    try:
        resp = litellm.completion(
            model=f"deepseek/{model}",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            api_key=key,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=max_tokens,
            timeout=settings.threads.request_timeout,  # 防卡住连接无限挂死(失败走调用方退避重试/降级)
        )
    except Exception as exc:
        # 永久错(没钱/凭证失效)→ 分类成 PermanentLLMError 冒泡熔断,判官绝不得吞成 fail-safe 票
        # (2026-07-02 E1);瞬时错原样冒泡,由各调用方退避重试/降级(零回归)。
        from pulsewire.llm_errors import classify_llm_error
        perm = classify_llm_error(exc)
        if perm is not None:
            raise perm from exc
        raise
    from pulsewire.obs.meter import record_llm_usage

    record_llm_usage(stage, model, resp)  # 计量(只读 usage,不改逻辑;绝不抛)
    choice = resp["choices"][0]
    content = choice["message"]["content"]
    # 🔴 推理模型(pro)可能把 token 烧在推理上 → finish_reason=length 截断 / 返空 JSON。这不是"正常空判"
    #    是**故障**:打告警让人看得见(否则各闸把空返回当正常判定,fail 方向静默生效=纯度静默漏水)。
    #    只加告警、不改控制流(下游各闸的脏返回兜底照旧接住,零回归)。见记忆 reasoning-model-maxtokens-empty。
    finish = choice.get("finish_reason")
    if finish == "length" or not (content or "").strip():
        log.warning("llm.truncated_or_empty", stage=stage, model=model,
                    finish_reason=finish, content_chars=len(content or ""))
    return content
