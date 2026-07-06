"""总结后端:把 (system, user) 提示词跑成一段 JSON 文本。

- api : litellm 调 DeepSeek(key 走 Settings.resolve_deepseek_key(),复用阶段4 同一把)。
- cli : 调本地登录的 claude -p / codex exec(走订阅);**只产文本**。
三道笼子:① LLM 只当一站(cli 用 -p 打印模式,不当全包 agent);② 产物照样进 verify 对账;
③ cli 失败/超时/空输出 → 按 cli_fallback_to_api 回退 DeepSeek,**绝不开天窗**(旧版踩过当天 0 产出)。
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


def _api_complete(system: str, user: str, settings: Settings, *, stage: str = "summarize") -> str:
    import logging

    import litellm

    litellm.suppress_debug_info = True
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)  # 关掉 litellm 往 stderr 打的 INFO 噪声
    key = settings.resolve_deepseek_key()
    if not key:
        raise RuntimeError(
            "summarize backend=api 但取不到 DeepSeek key("
            "PULSEWIRE_DEEPSEEK_API_KEY / AI_API_KEY env / macOS Keychain service=AI_API_KEY 均空)"
        )
    try:
        resp = litellm.completion(
            model=f"deepseek/{settings.summarize.model}",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            api_key=key,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=settings.summarize.max_tokens,  # 给足输出 headroom,防大批次响应被截断成坏 JSON
            timeout=settings.summarize.request_timeout,  # 防卡住连接无限挂死整条流水线
            num_retries=settings.summarize.request_retries,  # 瞬时超时/限流 litellm 内部重试
        )
    except Exception as exc:
        # 永久错(没钱/凭证失效)→ PermanentLLMError 冒泡熔断,别再当"JSON 没写好"傻重试
        # (2026-07-02 E1:402 被 _complete_validated 追加提示重试 3 轮);瞬时错原样冒泡。
        from pulsewire.llm_errors import classify_llm_error
        perm = classify_llm_error(exc)
        if perm is not None:
            raise perm from exc
        raise
    from pulsewire.obs.meter import record_llm_usage

    record_llm_usage(stage, settings.summarize.model, resp)  # 计量(只读 usage,不改逻辑;绝不抛)
    return resp["choices"][0]["message"]["content"]


def _cli_complete(system: str, user: str, settings: Settings) -> str:
    """调本地 CLI(claude/codex)打印模式产出文本。失败/空输出冒泡(由上层决定是否回退 api)。"""
    cmd_name = settings.summarize.cli_command
    prompt = f"{system}\n\n{user}\n\n只输出 JSON,不要任何额外解释或代码块标记。"
    if cmd_name == "claude":
        # 打印模式:产出后即退出,不进交互;只产文本
        argv = ["claude", "-p", prompt, "--output-format", "text"]
    elif cmd_name == "codex":
        argv = ["codex", "exec", prompt]
    else:
        raise RuntimeError(f"summarize backend=cli 但 cli_command 非法:{cmd_name!r}(应为 claude|codex)")

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"cli {cmd_name} 退出码 {proc.returncode}:{proc.stderr.strip()[:200]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"cli {cmd_name} 空输出")
    return out


def complete(system: str, user: str, settings: Settings, *, stage: str = "summarize") -> str:
    """按配置后端产出 JSON 文本;cli 失败按 cli_fallback_to_api 回退。

    stage:计量归账标签(写稿=summarize / 断言审计=audit);cli 走订阅不烧 API token,不计量。
    """
    backend = settings.summarize.backend
    if backend == "api":
        return _api_complete(system, user, settings, stage=stage)
    # backend == "cli"
    try:
        return _cli_complete(system, user, settings)
    except Exception as exc:
        log.warning("summarize.cli.failed", cmd=settings.summarize.cli_command, error=str(exc))
        if settings.summarize.cli_fallback_to_api:
            log.info("summarize.cli.fallback_to_api")
            return _api_complete(system, user, settings, stage=stage)
        raise  # 不开天窗:不回退就如实冒泡,不静默产空


def parse_json(content: str) -> dict:
    """容错解析:剥掉可能的 ```json``` 代码块包裹后 json.loads。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())
