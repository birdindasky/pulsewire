"""clip-ON 引擎胶水集成测试(考官+codex 共同点名的缺口):run_event_rank 的 step 6.75 接线端到端。

test_events_clip_memory.py 已用真库测透 clip_memory 各组件;本测钉的是**引擎里那 ~40 行胶水**:
账本查询→annotate 标注→novelty 工厂→_select 闸链首道真踢→留下者把 prev_report 随行写进
rankings.meta.clip。假会话喂罐装数据(仿 test_events_board_only 骨架),LLM 全 monkeypatch,
load_clip_ledger 打桩(账本查询本身在 clip_memory 测试里有真库往返)。

场景四事件同板:
- A 新鲜(不在账本)          → 留,meta=None
- B 已剪过 + 今天有新材料+判官说有新进展 → 留,meta.clip 随行(days_prior/last_date/prev_text)
- C 已剪过 + 材料全旧(peak 早于上次已剪日零点)→ 确定性踢,零判官调用
- D 已剪过 + 判官多数票"无新进展"        → 踢
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import pulsewire.events.clip_memory as C
import pulsewire.events.engine as E
from pulsewire.config import get_settings


def _settings():
    """clip 开 + 判决缓存关(缓存路径有独立测试;假会话不伺候缓存查询)。"""
    s = get_settings()
    ep = s.rank.event_pool.model_copy(update={
        "judgment_cache_enabled": False,
        "clip_memory_enabled": True,
    })
    return s.model_copy(update={"rank": s.rank.model_copy(update={"event_pool": ep})})


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """候选查询(单列)回四个簇;成员查询(多列)回罐装行。账本查询已打桩,不经此。"""

    def __init__(self, member_rows):
        self._member_rows = member_rows

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
        if len(list(stmt.selected_columns)) == 1:
            return _Result([("c1",), ("c2",), ("c3",), ("c4",)])
        return _Result(self._member_rows)


class _OrthoEmbedder:
    """每个不同文本一根近正交向量:掐死 A/B 候选召回,四簇各自成事件(本测不验聚类)。"""

    def __init__(self):
        self._seen: dict[str, int] = {}

    def embed(self, texts):
        out = []
        for t in texts:
            i = self._seen.setdefault(t, len(self._seen))
            v = np.zeros(64, dtype=np.float32)
            v[i % 64] = 1.0
            out.append(v)
        return out


@pytest.mark.asyncio
async def test_clip_gate_end_to_end_in_engine(monkeypatch):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=3)
    src = SimpleNamespace(id="ai-news-pro", domain="ai", board_only=False, mixed=False,
                          enabled=True, weight=1.0)
    body = "这是一段足够长的真文章正文,超过空壳护栏的最小字符数,讲清了谁在什么时候做了什么事情以及为什么值得关注。"
    member_rows = [
        ("c1", "itA", src.id, "A 全新量子纠错芯片问世", body + "A", now, "ai", None),
        ("c2", "itB", src.id, "B 监管谈判有了裁决落地", body + "B", now, "ai", None),
        ("c3", "itC", src.id, "C 旧闻纯复述无任何新材料", body + "C", old, "ai", None),
        ("c4", "itD", src.id, "D 换家媒体再报一遍昨天的事", body + "D", now, "ai", None),
    ]
    last_date = (now - timedelta(days=1)).astimezone().strftime("%Y-%m-%d")
    # C 的 last_date 取"昨天":peak(3天前) < 昨天零点 → stale_material 确定性踢
    def _rec(days):
        return {"thread_id": f"thr_{days}", "days_prior": days, "last_date": last_date,
                "prev_text": "昨天的稿:谈判进入关键阶段", "linked_today": False}

    ledger = {"c2": _rec(2), "c3": _rec(1), "c4": _rec(3)}

    async def _fake_ledger(session, cluster_ids, *, today, window_days):
        assert set(cluster_ids) >= {"c1", "c2", "c3", "c4"}  # 胶水必须把全部成员簇送来对账
        return ledger

    judged: list[str] = []

    def _fake_judge_has_new(prev_text, headline, bd, settings):
        judged.append(headline[:1])
        assert prev_text == "昨天的稿:谈判进入关键阶段"  # 胶水必须把账本前情原样送进判官
        return (False, "复述") if headline.startswith("D") else (True, "有裁决")

    written: list[dict] = []

    async def _upsert(session, **kw):
        written.append(kw)

    async def _prune(session, *, interest_key, keep_item_ids):
        pass

    monkeypatch.setattr("pulsewire.config.load_sources", lambda: [src])
    monkeypatch.setattr("pulsewire.store.prune_rankings", _prune)
    monkeypatch.setattr("pulsewire.store.upsert_ranking", _upsert)
    monkeypatch.setattr(C, "load_clip_ledger", _fake_ledger)
    monkeypatch.setattr(C, "judge_has_new", _fake_judge_has_new)
    monkeypatch.setattr(E, "extract_subject", lambda title, **k: (title or "")[:60])
    monkeypatch.setattr(E, "make_board_classifier", lambda *a, **k: None)
    monkeypatch.setattr(E, "classify_mixed_events", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_water_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_topic_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "make_worthiness_judge", lambda *a, **k: None)
    monkeypatch.setattr(E, "filter_off_topic", lambda evs, j, **k: evs)
    monkeypatch.setattr(E, "filter_water", lambda evs, j, **k: evs)
    monkeypatch.setattr(E, "filter_unworthy", lambda evs, j, **k: evs)

    domains = [SimpleNamespace(key="ai", interest="人工智能", tags=[], enabled=True,
                               interest_key="int_ai", freshness_window_hours=144)]
    # 账本已打桩:today/记忆窗逻辑在 test_events_clip_memory 的真库往返里验,这里只验胶水。
    res = await E.run_event_rank(_settings(), domains=domains,
                                 run_id=f"daily_{datetime.now():%Y%m%d}",
                                 sessionmaker=lambda: _Session(member_rows),
                                 embedder=_OrthoEmbedder())

    # 端到端裁决:A 留(新鲜)、B 留(有新进展)、C 踢(材料全旧)、D 踢(判官多数"无新进展")
    kept_ids = {w["item_id"] for w in written}
    assert kept_ids == {"itA", "itB"}, kept_ids
    assert res["domains"]["ai"] == 2

    # C 材料全旧 = 确定性踢,判官一票都不许烧;D 走判官(votes=3,'无新进展'需 2 票,提前停)
    assert "C" not in judged
    assert judged.count("D") == 2
    # B 留:投到"踢再也凑不齐多数"即停——votes=3 下 2 票"有新进展"后剩 1 票凑不齐 2,停
    assert judged.count("B") == 2

    # 随行数据:B 的 meta.clip 完整落进 rankings;A 干净无标记
    meta_by_item = {w["item_id"]: w.get("meta") for w in written}
    clip = (meta_by_item["itB"] or {}).get("clip")
    assert clip and clip["days_prior"] == 2 and clip["last_date"] == last_date
    assert clip["prev_text"] == "昨天的稿:谈判进入关键阶段"
    assert meta_by_item["itA"] is None
