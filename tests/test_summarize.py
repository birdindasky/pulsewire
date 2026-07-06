"""summarize 纯函数测试:占位提取、JSON 容错解析、token 分配、schema 校验、提示词不泄露数字。"""

from __future__ import annotations

import pytest

from pulsewire.summarize.backends import parse_json
from pulsewire.summarize.engine import _build_tokens, _build_user_prompt
from pulsewire.summarize.schema import DigestOutput, extract_tokens


def test_extract_tokens_dedup_ordered():
    assert extract_tokens("{F1} 和 {F2},再提 {F1}") == ["F1", "F2"]
    assert extract_tokens("没有占位") == []


def test_parse_json_plain_and_fenced():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json('```\n{"a": 3}\n```') == {"a": 3}


def test_build_tokens_global_numbering():
    facts = {
        "item-a": [
            {"source_id": "item-a:hn:points", "label": "HN points", "value": 901, "unit": "points"},
            {"source_id": "item-a:hn:num_comments", "label": "HN 评论数", "value": 876},
        ],
        "item-b": [
            {"source_id": "item-b:github:stars", "label": "GitHub stars", "value": 221751},
        ],
    }
    all_tokens, by_item = _build_tokens(facts)
    assert [t.token for t in all_tokens] == ["F1", "F2", "F3"]
    assert [t.token for t in by_item["item-a"]] == ["F1", "F2"]
    assert by_item["item-b"][0].source_id == "item-b:github:stars"


def test_prompt_hides_raw_numbers():
    """提示词只给 label,不给具体数字——模型从没见过数字,编不出来。"""
    facts = {"item-a": [{"source_id": "item-a:hn:points", "label": "HN points", "value": 99999, "unit": "points"}]}
    _all, by_item = _build_tokens(facts)
    prompt = _build_user_prompt([("item-a", "Some title", None)], by_item)
    assert "{F1}" in prompt
    assert "HN points" in prompt
    assert "99999" not in prompt  # 真实数字绝不进提示词


def test_prompt_annotates_corroboration():
    """单源条目在提示词里点名要求『据报道』;多源条目给出同报源数。"""
    prompt = _build_user_prompt(
        [("item-a", "Title A", None), ("item-b", "Title B", None)],
        {},
        corroboration={"item-a": 1, "item-b": 5},
    )
    assert "仅单一来源" in prompt and "据报道" in prompt
    assert "5 个源同报" in prompt
    # 不传 corroboration 时不输出佐证行(向后兼容)
    assert "信源佐证" not in _build_user_prompt([("item-a", "Title A", None)], {})


def test_digest_output_schema_validation():
    out = DigestOutput.model_validate(
        {"digest": "今日概述", "items": [
            {"item_id": "x", "headline": "标题", "tldr": "一句速读", "insight": "详细白话解读"}
        ]}
    )
    assert out.items[0].item_id == "x"
    assert out.items[0].tldr == "一句速读" and out.items[0].insight == "详细白话解读"
    # headline/tldr/insight 不能为空
    with pytest.raises(Exception):
        DigestOutput.model_validate(
            {"items": [{"item_id": "x", "headline": "h", "tldr": "", "insight": "y"}]}
        )
