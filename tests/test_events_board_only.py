"""回归:board_only 项目榜源(GitHub 热榜专属)绝不进新闻板事件的成员/代表/排名。

2026-06-30 真实事故:board_only 源 `github-search-ai-rag` 的 repo "MemTensor/MemOS" 搭车进
混簇后被选成代表、再被 board_classifier 拽进 AI 新闻板(run_20260630 日志 board_classifier.rerouted
headline=MemTensor/MemOS)。修法=step2 取成员的 SQL 把 board_only 源一并排除(与停用源同列)。

本测用假会话跑通 run_event_rank,钉两件事:
1) 取成员的 SQL 的 WHERE 用 `NOT IN` 排除了 board_only 源(机制层,真正的命门)。
2) 含 board_only item 的混簇里,写进 rankings 的只有合法新闻 item、绝无 board_only(端到端)。
不需要数据库:用记录语句的假会话喂罐装数据,LLM 判官/分类器全 monkeypatch 掉。
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


_NEWS_ID = "ai-news-pro"          # 普通专源(非 mixed/board_only):合法新闻
_GH_ID = "github-search-ai-rag"   # board_only 项目榜源(MemOS 那条)


def _src(sid, domain, *, board_only=False, mixed=False, enabled=True, weight=1.0):
    return SimpleNamespace(id=sid, domain=domain, board_only=board_only,
                           mixed=mixed, enabled=enabled, weight=weight)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """记录每条执行的 SQL;候选查询(单列)回一个簇,成员查询(多列)回 DB 过滤后的行。"""

    def __init__(self, captured, member_rows):
        self._captured = captured
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
        if len(list(stmt.selected_columns)) == 1:   # 候选/混簇池:选 cluster_id 单列
            self._captured.setdefault("candidate", []).append(stmt)
            return _Result([("c1",)])
        self._captured["member"] = stmt              # 成员查询:7 列
        return _Result(self._member_rows)


class _Embedder:
    def embed(self, texts):
        return [np.ones(8, dtype=np.float32) for _ in texts]


@pytest.mark.asyncio
async def test_board_only_excluded_from_news_members(monkeypatch):
    now = datetime.now(timezone.utc)
    sources = [_src(_NEWS_ID, "ai"), _src(_GH_ID, "ai", board_only=True)]
    # 真实 DB 会按 WHERE 过滤掉 board_only,故成员查询只回合法新闻行(模拟过滤后结果)。
    # 末位 None = Item.facts(2026-07 P1 成员查询加了 facts 列做全文读侧桥)。
    member_rows = [(
        "c1", "it-news", _NEWS_ID, "OpenAI 发布新模型",
        "OpenAI 今日宣布推出新一代模型，在推理基准上大幅提升、对开发者延迟更低，全球可用。",
        now, "ai", None,
    )]
    captured: dict = {}
    written: list[dict] = []

    def _sm():
        return _Session(captured, member_rows)

    async def _prune(session, *, interest_key, keep_item_ids):
        pass

    async def _upsert(session, **kw):
        written.append(kw)

    # 确定性:用假源清单 + 关掉所有 LLM 判官/分类器(本测只验选稿骨架,不验判官)
    monkeypatch.setattr("pulsewire.config.load_sources", lambda: sources)
    monkeypatch.setattr("pulsewire.store.prune_rankings", _prune)
    monkeypatch.setattr("pulsewire.store.upsert_ranking", _upsert)
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
    res = await E.run_event_rank(_cache_off(get_settings()), domains=domains, run_id="t",
                                 sessionmaker=_sm, embedder=_Embedder())

    # 1) 命门:成员查询的 WHERE 必须用 NOT IN 排除 board_only 源
    stmt = captured.get("member")
    assert stmt is not None, "成员查询没跑到"
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "NOT IN" in sql.upper(), f"成员查询没有 NOT IN 排除:{sql}"
    notin = sql.upper().index("NOT IN")
    assert _GH_ID in sql[notin:notin + 300], f"board_only 源没被 NOT IN 排除:{sql}"

    # 2) 端到端:写进 rankings 的只有合法新闻 item,board_only 一个都没有
    item_ids = [w["item_id"] for w in written]
    assert item_ids == ["it-news"], item_ids
    assert res["domains"]["ai"] == 1
