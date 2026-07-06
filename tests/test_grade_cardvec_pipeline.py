"""独立验收考官:card_vec 增量接进日报管道(v2 主线B②)。

由独立考官亲手编写,逐条验收原始需求 1–5:
1. run_summarize 写卡时当场算 card_vec + produced_by='pulsewire'。
2. 端到端:管道写的卡能被 recall_cards_by_vector 真召回(硬过滤 card_vec NOT NULL + produced_by)。
3. 管道算 card_vec 的文本拼法与 embed-cards 回填逐字一致(headline\ntldr_rendered\ninsight_rendered)。
4. embed 失败要降级:卡照写、card_vec 留 NULL,日报不崩。
5. 失败重跑(传 None)不得把已存在的好 card_vec 洗成 NULL。

需数据库;reqs 1-3 还需本地 embedding 模型(真跑加载 Qwen3/MLX,慢但能跑)。任一不可用则 skip。
LLM 一律 monkeypatch,不打真 API。测试自清理(integration 路径)/事务回滚(failure 路径)。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pulsewire.config import get_settings
from pulsewire.store import repo, upsert_item, upsert_ranking
from pulsewire.store.tables import Item, Ranking, Run, Summary

_IK = "int_gradecv"  # 本测试专用 interest_key(<=32 字符)
_RUN_ID = "daily_20260623"  # run_summarize 的 run_id;summaries.run_id 外键指向 runs,须先 seed
_SOURCES = ("gradecv-a", "gradecv-b", "gradecv-c")


# --------------------------------------------------------------------------- #
# 假 LLM:解析 user prompt 里的 item_id,给每条回一段合法 DigestOutput JSON。
# 不写任何裸数字 / 占位,verify_item 会判 ok,card_vec 文本=headline\ntldr\ninsight。
# --------------------------------------------------------------------------- #
_HEAD = {}  # item_id -> 期望 headline(测试可读回核对文本格式)


def _fake_complete(system, user, settings, *, stage="summarize"):
    ids = re.findall(r"item_id=(\S+)", user)
    items = []
    for iid in ids:
        h = f"标题-{iid[:8]}"
        t = f"一句话速读-{iid[:8]}"
        ins = f"这是一段详细的白话深度解读内容，针对条目 {iid[:8]} 展开说明。"
        _HEAD[iid] = (h, t, ins)
        items.append({"item_id": iid, "headline": h, "tldr": t, "insight": ins})
    return json.dumps({"digest": "今日概述（无数字）", "items": items}, ensure_ascii=False)


def _db_or_skip():
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    return settings, engine


async def _engine_alive(engine):
    try:
        conn = await engine.connect()
        await conn.close()
        return True
    except (OperationalError, InterfaceError, ConnectionError, OSError):
        return False


def _real_embedder_or_skip(settings, engine):
    from pulsewire.dedup import get_embedder

    try:
        emb = get_embedder(settings)
        emb.embed_passage(["warmup"])  # 触发模型加载
        return emb
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"本地 embedding 模型不可用，跳过：{exc}")


async def _seed_ranked_items(sm, n=2):
    """建 n 条 item + rankings(run_summarize 的输入)。返回 item_ids。"""
    now = datetime.now(timezone.utc)
    ids = []
    async with sm() as session:
        async with session.begin():
            # run_summarize 写 summaries 需父 runs 行(FK summaries_run_id_fkey);生产由
            # orchestrator 建,这里直接调 run_summarize 故手动 seed。幂等:已存在则不重复建。
            if await session.get(Run, _RUN_ID) is None:
                await repo.create_run(session, run_id=_RUN_ID, trigger_type="daily")
            for i in range(n):
                iid = await upsert_item(
                    session, source=_SOURCES[i % len(_SOURCES)],
                    url=f"https://grade.example/cv-{i}",
                    title=f"原标题 {i} OpenAI 中东 芯片",
                    content=f"这是第 {i} 条的正文内容，描述一件具体的科技新闻。",
                    published_at=now,
                )
                ids.append(iid)
                await upsert_ranking(
                    session, interest_key=_IK, interest="科技", tags=None,
                    item_id=iid, cluster_id=None, recall_score=0.5, rule_score=0.5,
                    rerank_score=0.5, final_score=0.9 - i * 0.1, rank=i + 1,
                    provider="rule",
                )
    return ids


async def _cleanup(sm, ids):
    async with sm() as session:
        async with session.begin():
            await session.execute(delete(Ranking).where(Ranking.interest_key == _IK))
            await session.execute(delete(Summary).where(Summary.interest_key == _IK))
            if ids:
                await session.execute(delete(Item).where(Item.item_id.in_(ids)))
            await session.execute(delete(Run).where(Run.run_id == _RUN_ID))


# =========================================================================== #
# reqs 1 / 2 / 3：端到端真跑（真 embedder，假 LLM）                            #
# =========================================================================== #
@pytest.mark.asyncio
async def test_pipeline_writes_cardvec_and_recall_hits(monkeypatch):
    """req1+req2+req3：run_summarize 写卡当场算 card_vec + produced_by='pulsewire'，
    且这些卡能被 recall_cards_by_vector 真召回，文本格式与 embed-cards 逐字一致。"""
    from pulsewire.summarize import engine as eng

    settings, engine = _db_or_skip()
    if not await _engine_alive(engine):
        await engine.dispose()
        pytest.skip("数据库不可用")
    emb = _real_embedder_or_skip(settings, engine)

    # 真 embedder + 假 LLM(只换 engine 命名空间里的 complete,模型照真跑)
    monkeypatch.setattr(eng, "complete", _fake_complete)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    try:
        ids = await _seed_ranked_items(sm, n=2)
        result = await eng.run_summarize(settings, interest_key=_IK, run_id="daily_20260623", sessionmaker=sm)
        assert result["summarized"] == 2, result

        # ---- req1：card_vec 落库 + produced_by='pulsewire' ----
        async with sm() as session:
            rows = list((await session.execute(
                select(Summary).where(Summary.interest_key == _IK)
            )).scalars().all())
        assert len(rows) == 2, "管道应为两条 ranking 各写一张卡"
        for s in rows:
            assert s.card_vec is not None, f"req1 FAIL: {s.item_id} card_vec 没写"
            assert len(s.card_vec) == 1024, f"card_vec 维度异常 {len(s.card_vec)}"
            assert s.produced_by == "pulsewire", f"req1 FAIL: produced_by={s.produced_by!r} 不是 pulsewire"

        # ---- req3：管道算向量的文本 == embed-cards(_embed_cards 行 451)的文本，逐字一致 ----
        # _embed_cards 文本拼法:f"{headline}\n{tldr_rendered or ''}\n{insight_rendered or ''}".strip()
        # 用同一 embedder 重嵌 embed-cards 文本,与库里 card_vec 比余弦相似度。
        # MLX/Metal 8bit GPU matmul 非比特级可复现,逐分量会有 ~1e-3 噪声(非格式问题),
        # 故用余弦≈1.0 作判据(这才是召回真正依赖的性质);文本逐字一致另有专门测试坐实。
        import math

        def _cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb)

        for s in rows:
            embed_cards_text = f"{s.headline}\n{s.tldr_rendered or ''}\n{s.insight_rendered or ''}".strip()
            reembed = emb.embed_passage([embed_cards_text])[0]
            assert len(reembed) == len(s.card_vec)
            cos = _cos(reembed, s.card_vec)
            assert cos > 0.9999, (
                f"req3 FAIL: {s.item_id} 管道 card_vec 与按 embed-cards 文本格式复嵌的向量不同向"
                f"(cos={cos});说明文本拼法不一致或没用 rendered 文本"
            )

        # ---- req2:端到端召回。用某条卡的 headline 当 query,recall 必须能命中管道写的卡 ----
        target = rows[0]
        qvec = emb.embed_query(target.headline)
        async with sm() as session:
            recalled = await repo.recall_cards_by_vector(session, vector=qvec, limit=10)
        recalled_ids = {s.item_id for s, _sim in recalled}
        assert target.item_id in recalled_ids, (
            f"req2 FAIL: 管道写的卡 {target.item_id} 没被 recall_cards_by_vector 召回;"
            f"召回到的={recalled_ids}(头号坑:produced_by/card_vec 没写对会被静默丢)"
        )
        # 召回的卡都满足硬过滤(card_vec 非空 + produced_by)
        for s, _ in recalled:
            assert s.card_vec is not None and s.produced_by == "pulsewire"
    finally:
        await _cleanup(sm, ids)
        await engine.dispose()


# =========================================================================== #
# req3 二次确认(独立、不依赖向量近似):直接比对两处文本拼法的源字符串       #
# =========================================================================== #
@pytest.mark.asyncio
async def test_cardvec_text_format_matches_embed_cards_exactly(monkeypatch):
    """req3 的字面证据:对同一条卡,engine 算 card_vec 用的文本 与 _embed_cards 用的文本
    必须逐字相同。这里用同一条 Summary 行,分别按两处源码的拼法生成字符串后断言相等。"""
    from pulsewire.summarize import engine as eng

    settings, engine = _db_or_skip()
    if not await _engine_alive(engine):
        await engine.dispose()
        pytest.skip("数据库不可用")

    # 这条不需真 embedder:把 embedder 换成只记录"被喂进去的文本"的间谍。
    captured = {}

    class _Spy:
        model_name = "spy"

        def embed_passage(self, texts):
            captured["texts"] = list(texts)
            return [[0.01] * 1024 for _ in texts]

        def embed(self, texts):
            return [[0.01] * 1024 for _ in texts]

        def embed_query(self, t):
            return [0.01] * 1024

    monkeypatch.setattr(eng, "complete", _fake_complete)
    monkeypatch.setattr(eng, "get_embedder", lambda s: _Spy())
    # 关掉标题护栏(它会再调 dedup.get_embedder,与本测试无关)
    monkeypatch.setattr(settings.summarize, "headline_coherence_check", False)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    try:
        ids = await _seed_ranked_items(sm, n=2)
        await eng.run_summarize(settings, interest_key=_IK, run_id="daily_20260623", sessionmaker=sm)

        # 管道实际喂给 embedder 的文本(间谍抓到的)
        pipeline_texts = sorted(captured["texts"])

        # 用库里落地的卡,按 _embed_cards(run.py:451)的拼法重建文本
        async with sm() as session:
            rows = list((await session.execute(
                select(Summary).where(Summary.interest_key == _IK)
            )).scalars().all())
        embed_cards_texts = sorted(
            f"{s.headline}\n{s.tldr_rendered or ''}\n{s.insight_rendered or ''}".strip()
            for s in rows
        )
        assert pipeline_texts == embed_cards_texts, (
            "req3 FAIL: 管道喂 embedder 的文本与 embed-cards 回填文本不是逐字一致\n"
            f"pipeline={pipeline_texts}\nembed_cards={embed_cards_texts}"
        )
    finally:
        await _cleanup(sm, ids)
        await engine.dispose()


# =========================================================================== #
# req4：embed 失败要降级（卡照写、card_vec NULL、日报不崩）                    #
# req5：失败重跑不得洗掉已有好向量                                            #
# 这两条不需真模型(用桩 embedder),走事务回滚的 db_session 即可。            #
# =========================================================================== #
@pytest_asyncio.fixture
async def integ_engine():
    settings = get_settings()
    engine = create_async_engine(settings.database.async_dsn, poolclass=NullPool)
    if not await _engine_alive(engine):
        await engine.dispose()
        pytest.skip("数据库不可用")
    yield settings, engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_embed_failure_degrades_card_vec_null_report_survives(monkeypatch, integ_engine):
    """req4:embedder 抛异常 → run_summarize 照常完成落库(卡照写、card_vec NULL),日报不崩。"""
    from pulsewire.summarize import engine as eng

    settings, engine = integ_engine

    class _Boom:
        model_name = "boom"

        def embed_passage(self, texts):
            raise RuntimeError("embedder 烤机挂了")

        def embed(self, texts):  # 标题护栏用;关掉护栏后用不到,留着也无妨
            raise RuntimeError("embedder 烤机挂了")

        def embed_query(self, t):
            raise RuntimeError("embedder 烤机挂了")

    monkeypatch.setattr(eng, "complete", _fake_complete)
    monkeypatch.setattr(eng, "get_embedder", lambda s: _Boom())
    monkeypatch.setattr(settings.summarize, "headline_coherence_check", False)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    try:
        ids = await _seed_ranked_items(sm, n=2)
        # 不得抛:embed 失败必须被吞成降级
        result = await eng.run_summarize(settings, interest_key=_IK, run_id="daily_20260623", sessionmaker=sm)
        assert result["summarized"] == 2, "req4 FAIL: 日报应照常完成(卡照写)"

        async with sm() as session:
            rows = list((await session.execute(
                select(Summary).where(Summary.interest_key == _IK)
            )).scalars().all())
        assert len(rows) == 2, "req4 FAIL: 卡没落库(embed 失败把整个落库拖崩了)"
        for s in rows:
            assert s.card_vec is None, f"req4 FAIL: embed 失败时 {s.item_id} card_vec 不应有值"
            # 卡内容仍正确落库
            assert s.headline and s.tldr_rendered and s.insight_rendered
    finally:
        await _cleanup(sm, ids)
        await engine.dispose()


@pytest.mark.asyncio
async def test_embed_failure_does_not_wash_existing_good_vector(monkeypatch, integ_engine):
    """req5:第一轮成功写好 card_vec;第二轮重跑 embed 失败(None)→ 旧 card_vec 必须保留,不被洗成 NULL。"""
    from pulsewire.summarize import engine as eng

    settings, engine = integ_engine

    good_vec = [0.123] * 1024

    class _OK:
        model_name = "ok"

        def embed_passage(self, texts):
            return [list(good_vec) for _ in texts]

        def embed(self, texts):
            return [list(good_vec) for _ in texts]

        def embed_query(self, t):
            return list(good_vec)

    class _Boom:
        model_name = "boom"

        def embed_passage(self, texts):
            raise RuntimeError("第二轮 embed 挂了")

        def embed(self, texts):
            raise RuntimeError("第二轮 embed 挂了")

        def embed_query(self, t):
            raise RuntimeError("第二轮 embed 挂了")

    monkeypatch.setattr(eng, "complete", _fake_complete)
    monkeypatch.setattr(settings.summarize, "headline_coherence_check", False)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids = []
    try:
        ids = await _seed_ranked_items(sm, n=2)

        # 第一轮:embed 成功 → card_vec 写入好向量
        monkeypatch.setattr(eng, "get_embedder", lambda s: _OK())
        await eng.run_summarize(settings, interest_key=_IK, run_id="daily_20260623", sessionmaker=sm)
        async with sm() as session:
            before = {s.item_id: s.card_vec for s in (await session.execute(
                select(Summary).where(Summary.interest_key == _IK))).scalars().all()}
        assert before and all(v is not None for v in before.values()), "前置失败:第一轮应写好 card_vec"

        # 第二轮:同条目重跑,但 embed 挂了(传 None) → 旧向量不能被洗掉
        monkeypatch.setattr(eng, "get_embedder", lambda s: _Boom())
        await eng.run_summarize(settings, interest_key=_IK, run_id="daily_20260623", sessionmaker=sm)
        async with sm() as session:
            after = {s.item_id: s.card_vec for s in (await session.execute(
                select(Summary).where(Summary.interest_key == _IK))).scalars().all()}

        for iid, vec in after.items():
            assert vec is not None, f"req5 FAIL: 重跑 embed 失败把 {iid} 的旧 card_vec 洗成 NULL 了"
            maxdiff = max(abs(a - b) for a, b in zip(vec, before[iid]))
            assert maxdiff < 1e-6, f"req5 FAIL: {iid} 旧向量被改动(maxdiff={maxdiff})"
    finally:
        await _cleanup(sm, ids)
        await engine.dispose()
