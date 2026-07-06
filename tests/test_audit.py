"""LLM 断言审计纯逻辑测试:提示词构造、判定解析、防 id 编造、重试耗尽冒泡。

LLM 调用一律 monkeypatch(complete),不打真 API。
"""

from __future__ import annotations

import json

import pytest

from pulsewire.config import get_settings
from pulsewire.summarize.audit import _build_audit_prompt, audit_single_source_items

_CANDS = [
    ("i1", "公司X已向SEC递交S-1文件", "X要上市了", "X公司已正式启动上市流程。"),
    ("i2", "新工具发布", "一款新工具", "一款常规的开发者工具发布了。"),
]


def _settings():
    return get_settings()


def test_build_audit_prompt_contains_items_and_contract():
    p = _build_audit_prompt(_CANDS)
    assert "item_id=i1" in p and "item_id=i2" in p
    assert "递交S-1文件" in p and "常规的开发者工具" in p
    assert '"risky"' in p and '"claims"' in p  # 输出契约


def test_audit_flags_risky_and_keeps_clean(monkeypatch):
    resp = json.dumps({"items": [
        {"item_id": "i1", "risky": True, "claims": ["上市流程写成既成事实"]},
        {"item_id": "i2", "risky": False, "claims": []},
    ]})
    monkeypatch.setattr("pulsewire.summarize.audit.complete", lambda *a, **k: resp)
    out = audit_single_source_items(_CANDS, _settings())
    assert out == {"i1": ["上市流程写成既成事实"]}


def test_audit_drops_hallucinated_item_id(monkeypatch):
    # 模型编了个 candidates 里没有的 id → 丢弃,不能污染别的条目
    resp = json.dumps({"items": [{"item_id": "i999", "risky": True, "claims": ["编的"]}]})
    monkeypatch.setattr("pulsewire.summarize.audit.complete", lambda *a, **k: resp)
    assert audit_single_source_items(_CANDS, _settings()) == {}


def test_audit_risky_without_claims_gets_default(monkeypatch):
    resp = json.dumps({"items": [{"item_id": "i1", "risky": True, "claims": []}]})
    monkeypatch.setattr("pulsewire.summarize.audit.complete", lambda *a, **k: resp)
    out = audit_single_source_items(_CANDS, _settings())
    assert out == {"i1": ["传闻当事实"]}


def test_audit_empty_candidates_skips_llm(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("空候选不该调 LLM")
    monkeypatch.setattr("pulsewire.summarize.audit.complete", _boom)
    assert audit_single_source_items([], _settings()) == {}


def test_audit_retry_exhausted_raises(monkeypatch):
    monkeypatch.setattr("pulsewire.summarize.audit.complete", lambda *a, **k: "不是JSON")
    with pytest.raises(RuntimeError, match="重试耗尽"):
        audit_single_source_items(_CANDS, _settings())
