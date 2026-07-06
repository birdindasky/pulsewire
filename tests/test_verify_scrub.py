"""破文本通用消毒 scrub_residual_markup(2026-06-25 三考:litellm insight 漏 `{…}F2S26`)。

治 LLM 偶吐字面花括号 / 损坏 token 裸漏给用户;是 06-16 TOKEN_RE/FOOTNOTE_RE 的通用补充。
"""
from __future__ import annotations

import pulsewire.summarize  # noqa: F401 — 先触发 summarize 完成,避开 verify↔summarize 循环导入(同 test_verify.py 顺序)
from pulsewire.verify import scrub_residual_markup


def test_unwraps_residual_braces_keep_inner():
    assert scrub_residual_markup("覆盖了{包括A、B等在内的}平台") == "覆盖了包括A、B等在内的平台"


def test_garbled_token_to_review_marker():
    assert scrub_residual_markup("等在内的F2S26个平台") == "等在内的[待核实]个平台"


def test_litellm_real_case():
    s = "它覆盖了{包括AWS Bedrock、Anthropic等在内的}F2S26个平台"
    out = scrub_residual_markup(s)
    assert "{" not in out and "}" not in out
    assert "F2S26" not in out
    assert out == "它覆盖了包括AWS Bedrock、Anthropic等在内的[待核实]个平台"


def test_orphan_braces_stripped():
    assert scrub_residual_markup("半个{括号") == "半个括号"
    assert scrub_residual_markup("另半个}") == "另半个"


def test_clean_text_untouched():
    s = "litellm 提供统一接口，支持上百个模型。"
    assert scrub_residual_markup(s) == s


def test_resolved_number_untouched():
    # _render 已把 {F13} 换成真数字 47378,scrub 绝不该动它(只清残留花括号/损坏 token)
    assert scrub_residual_markup("用 47378 颗星换来") == "用 47378 颗星换来"


def test_empty_and_no_markup():
    assert scrub_residual_markup("") == ""
    assert scrub_residual_markup("纯文本无标记") == "纯文本无标记"
