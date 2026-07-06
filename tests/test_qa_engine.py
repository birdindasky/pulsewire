"""语义问答引擎:零编造校验 + 召回空→没找到 + fail-safe。见 docs/DESIGN.md §3。

盯铁律:
- parse_validate 严格——enough is True + used 非空且全在 [1,n],否则降级"没找到"(绝不展示可能编的)。
- answer() 任何失败/空召回路径都不返回编造内容。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pulsewire.qa import engine as E


class _Card:
    def __init__(self, headline, tldr="t", insight="i", item_id="x", day=1):
        self.headline = headline
        self.tldr_rendered = tldr
        self.insight_rendered = insight
        self.item_id = item_id
        self.created_at = datetime(2026, 6, day, tzinfo=timezone.utc)


# ---------- parse_validate:零编造校验(命门) ----------

def test_valid_answer_passes():
    raw = json.dumps({"enough": True, "answer": "中东缓和了 [1][2]", "used": [1, 2]})
    r = E.parse_validate(raw, 3)
    assert r == {"enough": True, "answer": "中东缓和了 [1][2]", "used": [1, 2]}


@pytest.mark.parametrize("payload", [
    {"enough": False, "answer": "x", "used": [1]},          # enough=false
    {"enough": True, "answer": "x", "used": []},            # used 空
    {"enough": True, "answer": "x", "used": [4]},           # 越界(n=3)
    {"enough": True, "answer": "x", "used": [0]},           # 0 越界
    {"enough": True, "answer": "x", "used": ["1"]},         # 字符串非 int
    {"enough": True, "answer": "x", "used": [True]},        # bool 不算 int
    {"enough": True, "answer": "", "used": [1]},            # answer 空
    {"enough": "yes", "answer": "x", "used": [1]},          # enough 非严格 True
    {"answer": "x", "used": [1]},                           # 缺 enough
    {},                                                      # 全空(乱码)
])
def test_invalid_degrades_to_not_enough(payload):
    r = E.parse_validate(json.dumps(payload), 3)
    assert r == {"enough": False, "answer": "", "used": []}


def test_garbage_json_degrades(monkeypatch):
    monkeypatch.setattr(E, "parse_json", lambda _s: None)  # 乱码 → None
    r = E.parse_validate("garbage", 3)
    assert r["enough"] is False


def test_parse_json_raises_degrades():
    # 真 parse_json 对空串/坏 JSON 抛 json.loads 异常(deepseek 偶发返空)→ 必须兜成降级不崩
    assert E.parse_validate("", 3) == {"enough": False, "answer": "", "used": []}
    assert E.parse_validate("not json at all", 3)["enough"] is False


# ---------- format_evidence ----------

def test_format_evidence_numbered():
    cards = [(_Card("头条A", day=1), 0.9), (_Card("头条B", day=2), 0.8)]
    ev = E.format_evidence(cards)
    assert "[1] (2026-06-01) 头条A" in ev
    assert "[2] (2026-06-02) 头条B" in ev


# ---------- answer() orchestration ----------

def _wire(monkeypatch, *, cards, llm_raw=None, llm_exc=None, recall_exc=None):
    monkeypatch.setattr(E, "get_embedder",
                        lambda s: type("Emb", (), {"embed_query": lambda self, t: [0.1] * 8})())

    class _CM:
        async def __aenter__(self): return "session"
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(E, "get_sessionmaker", lambda: (lambda: _CM()))

    async def _recall(session, **kw):
        if recall_exc:
            raise recall_exc
        return cards
    monkeypatch.setattr(E, "recall_cards_by_vector", _recall)

    def _llm(*a, **k):
        if llm_exc:
            raise llm_exc
        return llm_raw
    monkeypatch.setattr(E, "complete_json", _llm)


@pytest.mark.asyncio
async def test_answer_empty_recall_says_not_found(monkeypatch):
    called = {"llm": False}
    _wire(monkeypatch, cards=[])
    monkeypatch.setattr(E, "complete_json",
                        lambda *a, **k: called.__setitem__("llm", True) or "{}")
    r = await E.answer("无关问题")
    assert r["ok"] is True and r["enough"] is False
    assert r["answer"] == E._NOT_FOUND
    assert called["llm"] is False  # 空召回不调 LLM


@pytest.mark.asyncio
async def test_answer_valid(monkeypatch):
    cards = [(_Card("中东缓和", item_id="m1"), 0.9), (_Card("油价跌", item_id="m2"), 0.8)]
    _wire(monkeypatch, cards=cards,
          llm_raw=json.dumps({"enough": True, "answer": "缓和了 [1]", "used": [1]}))
    r = await E.answer("中东咋样")
    assert r["ok"] and r["enough"]
    assert r["answer"] == "缓和了 [1]"
    assert len(r["cards"]) == 1 and r["cards"][0]["item_id"] == "m1"


@pytest.mark.asyncio
async def test_answer_llm_fails_unavailable_not_fabricate(monkeypatch):
    cards = [(_Card("x"), 0.9)]
    _wire(monkeypatch, cards=cards, llm_exc=RuntimeError("llm down"))
    r = await E.answer("q")
    assert r["ok"] is False and r["enough"] is False
    assert r["answer"] == E._UNAVAILABLE and r["cards"] == []


@pytest.mark.asyncio
async def test_answer_invalid_used_degrades(monkeypatch):
    # LLM 填越界卡号 → 降级没找到,绝不展示可能编的 answer
    cards = [(_Card("x"), 0.9)]
    _wire(monkeypatch, cards=cards,
          llm_raw=json.dumps({"enough": True, "answer": "编的 [5]", "used": [5]}))
    r = await E.answer("q")
    assert r["enough"] is False and r["answer"] == E._NOT_FOUND


@pytest.mark.asyncio
async def test_answer_recall_fails_unavailable(monkeypatch):
    _wire(monkeypatch, cards=[], recall_exc=RuntimeError("db down"))
    r = await E.answer("q")
    assert r["ok"] is False and r["answer"] == E._UNAVAILABLE
