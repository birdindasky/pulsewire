"""complete_json:pro 推理模型可能把 token 烧在推理上 → finish_reason=length 截断 / 返空 JSON。

rank5(2026-07-01):这不是"正常空判"是**故障**,必须打告警让人看得见(否则各闸把空返回当正常判定、
fail 方向静默生效=纯度静默漏水)。只加告警、不改控制流(内容原样返回,下游脏返回兜底照旧接住)。
"""
from __future__ import annotations

from pulsewire.threads import llm as L


class _Settings:
    class threads:
        request_timeout = 30

    def resolve_deepseek_key(self):
        return "fake-key"


def _resp(content, finish):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def _patch(monkeypatch, resp):
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **k: resp)
    warned: list = []
    monkeypatch.setattr(L.log, "warning", lambda *a, **k: warned.append((a, k)))
    return warned


def _run():
    return L.complete_json("sys", "user", model="deepseek-v4-pro",
                           max_tokens=8, settings=_Settings(), stage="topic_judge")


def test_truncated_length_warns(monkeypatch):
    warned = _patch(monkeypatch, _resp('{"off_topic": tr', "length"))  # 截断的半截 JSON
    out = _run()
    assert out == '{"off_topic": tr'  # 🔴 内容原样返回(不改控制流,零回归)
    assert warned and warned[0][0][0] == "llm.truncated_or_empty"  # 打了告警


def test_empty_content_warns(monkeypatch):
    warned = _patch(monkeypatch, _resp("   ", "stop"))  # 返空(只有空白)
    out = _run()
    assert out == "   "
    assert warned and warned[0][0][0] == "llm.truncated_or_empty"


def test_normal_no_warn(monkeypatch):
    warned = _patch(monkeypatch, _resp('{"off_topic": false}', "stop"))  # 正常完整 JSON
    out = _run()
    assert out == '{"off_topic": false}'
    assert warned == []  # 正常返回不告警(不制造噪音)
