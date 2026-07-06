"""回归:events 选稿链 `_select` 的四道闸接线顺序 + 数据链完整(rank3,2026-07-01 补)。

对抗测试挖到:`filter_off_topic → filter_water → filter_unworthy → apply_event_quotas` 这条链只在
`run_event_rank` 内的嵌套闭包 `_select`(engine.py:438-447)里串起来,全仓无任何测试真调它——谁把顺序
改错、或漏把某闸输出喂给下一环(如 `apply_event_quotas(after_water)` 跳过 worthiness),447 测照样全绿。

本测用「假会话」跑通 run_event_rank(不需数据库),把四道闸 monkeypatch 成:①记录自己被调的顺序;
②各自删掉一个特定标记的事件。喂 4 个事件(clean/跑题/水货/不够格),断言:
- 四闸按 off_topic→water→unworthy→quotas 顺序被调(顺序回归);
- 最终只有 clean 活下来(证明每道闸的删除真作用在链上、quotas 真跑在幸存者上=数据链没断)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import pulsewire.events.engine as E
from pulsewire.config import get_settings

def _cache_off(settings):
    """本测试测选稿/排除逻辑,非判决缓存;钉死缓存关,免得真配置开关把假会话带进缓存查询。"""
    ep = settings.rank.event_pool.model_copy(update={
        "judgment_cache_enabled": False,
        # 已剪记忆闸同钉死:真 config 开着它会让假会话吃到账本查询(列数分流被打乱)——
        # 单测免疫真开关的老纪律(同 feishu_card/judgment_cache)。
        "clip_memory_enabled": False,
    })
    return settings.model_copy(update={"rank": settings.rank.model_copy(update={"event_pool": ep})})


_NEWS_ID = "ai-news-pro"  # 普通专源(非 mixed/board_only):合法新闻源
now = datetime.now(timezone.utc)


def _src(sid, domain, *, board_only=False, mixed=False, enabled=True, weight=1.0):
    return SimpleNamespace(id=sid, domain=domain, board_only=board_only,
                           mixed=mixed, enabled=enabled, weight=weight)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """候选查询(单列 cluster_id)回全部 4 簇;成员查询(7 列)回罐装成员行。"""

    def __init__(self, member_rows, cluster_ids):
        self._member_rows = member_rows
        self._cluster_ids = cluster_ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        outer = self

        class _B:
            async def __aenter__(self):
                return outer

            async def __aexit__(self, *a):
                return False

        return _B()

    async def execute(self, stmt, *a, **k):
        if len(list(stmt.selected_columns)) == 1:  # 候选/混簇池:选 cluster_id 单列
            return _Result([(cid,) for cid in self._cluster_ids])
        return _Result(self._member_rows)  # 成员查询:7 列


class _Embedder:
    def embed(self, texts):
        return [np.ones(8, dtype=np.float32) for _ in texts]


def _mrow(cid, iid, title):
    body = "正文足够长的真实新闻内容，用于让该簇成为有效事件并算出热度与速度，避免被壳过滤。" * 2
    # 末位 None = Item.facts(2026-07 P1 成员查询加了 facts 列做全文读侧桥)
    return (cid, iid, _NEWS_ID, title, body, now, "ai", None)


@pytest.mark.asyncio
async def test_select_gate_chain_order_and_effect(monkeypatch):
    sources = [_src(_NEWS_ID, "ai")]
    member_rows = [
        _mrow("c1", "it-clean", "CLEAN 合法新闻，够格且不跑题不水"),
        _mrow("c2", "it-off", "OFF 跑题内容（该被话题闸删）"),
        _mrow("c3", "it-water", "WATER 水货内容（该被水货闸删）"),
        _mrow("c4", "it-unworthy", "UNWORTHY 不够格内容（该被够格闸删）"),
    ]
    cluster_ids = ["c1", "c2", "c3", "c4"]
    written: list[dict] = []
    order: list[str] = []

    def _sm():
        return _Session(member_rows, cluster_ids)

    async def _prune(session, *, interest_key, keep_item_ids):
        pass

    async def _upsert(session, **kw):
        written.append(kw)

    # 四道闸:记录调用顺序 + 各删一个标记事件(数据链真跑在幸存者上才可能全删对)
    def _off(evs, j, **k):
        order.append("off_topic")
        return [e for e in evs if e.get("rep_item_id") != "it-off"]

    def _water(evs, j, **k):
        order.append("water")
        return [e for e in evs if e.get("rep_item_id") != "it-water"]

    def _unworthy(evs, j, **k):
        order.append("unworthy")
        return [e for e in evs if e.get("rep_item_id") != "it-unworthy"]

    def _quotas(evs, **k):
        order.append("quotas")
        return evs

    monkeypatch.setattr("pulsewire.config.load_sources", lambda: sources)
    monkeypatch.setattr("pulsewire.store.prune_rankings", _prune)
    monkeypatch.setattr("pulsewire.store.upsert_ranking", _upsert)
    monkeypatch.setattr(E, "extract_subject", lambda title, **k: (title or "")[:60])
    # 同事件判官必须打桩:4 个假事件正文相同,真调 LLM 会被(偶发)判同→全并成 1 事件,测试闪断且烧真 token
    monkeypatch.setattr(E, "judge_same_event", lambda *a, **k: False)
    monkeypatch.setattr(E, "make_board_classifier", lambda *a, **k: None)
    monkeypatch.setattr(E, "classify_mixed_events", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_water_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_topic_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_worthiness_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "filter_off_topic", _off)
    monkeypatch.setattr(E, "filter_water", _water)
    monkeypatch.setattr(E, "filter_unworthy", _unworthy)
    monkeypatch.setattr(E, "apply_event_quotas", _quotas)

    domains = [SimpleNamespace(key="ai", interest="人工智能", tags=[], enabled=True,
                               interest_key="int_ai", freshness_window_hours=144)]
    res = await E.run_event_rank(_cache_off(get_settings()), domains=domains, run_id="t",
                                 sessionmaker=_sm, embedder=_Embedder())

    # ① 四道闸按 _select 里的固定顺序被调(顺序回归:改错顺序即红)
    assert order == ["off_topic", "water", "unworthy", "quotas"], order
    # ② 只有 clean 活到写榜:证明每道闸的删除都真作用在链上、quotas 跑在幸存者上(漏接某环→对应标记货会活下来)
    assert [w["item_id"] for w in written] == ["it-clean"], [w["item_id"] for w in written]
    assert res["domains"]["ai"] == 1
