"""仓储层:对核心表的读写。失败要冒泡,不静默吞。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .ids import make_item_id
from .tables import (
    Cluster,
    Delivery,
    Digest,
    Embedding,
    Item,
    ItemTimeline,
    Judgment,
    Ranking,
    Run,
    Summary,
    Thread,
    ThreadCluster,
)


async def upsert_item(
    session: AsyncSession,
    *,
    source: str,
    url: str,
    title: str,
    content: str | None = None,
    normalized_url: str | None = None,
    content_fingerprint: str | None = None,
    item_id: str | None = None,
    cluster_id: str | None = None,
    lang: str | None = None,
    category: str | None = None,
    region: str | None = None,
    published_at: datetime | None = None,
    facts: dict | None = None,
) -> str:
    """写入条目(item_id 缺省时由 规范化URL+内容指纹 生成);已存在则更新可变字段。返回 item_id。"""
    from .ids import content_fingerprint as _fp
    from .ids import normalize_url as _norm

    item_id = item_id or make_item_id(url, title, content or "")
    normalized_url = normalized_url or _norm(url)
    content_fingerprint = content_fingerprint or _fp(title, content or "")

    values = dict(
        item_id=item_id,
        source=source,
        url=url,
        normalized_url=normalized_url,
        title=title,
        content=content,
        content_fingerprint=content_fingerprint,
        cluster_id=cluster_id,
        lang=lang,
        category=category,
        region=region,
        published_at=published_at,
        facts=facts,
    )
    stmt = pg_insert(Item).values(**values)
    # 同一 item_id 再入库:更新可变字段(簇归属、分类等)。
    # facts 改为 **按键合并**(jsonb ||,新值同键覆盖):旧版整块覆盖会把 enrich 写进去的
    # facts.fulltext / facts.enriched 在隔天重抓同条目时抹掉 → 全文富化天天白抓重来
    # (2026-07 P1;条目还在 feed 窗口内就会被重抓,低频源一挂几周)。
    # hn/github 每次抓带新鲜数值,excluded 同键覆盖 → 行为不变。
    # ⚠️ 用 jsonb_typeof 判"双方都是对象才 ||":Python None 经 JSONB 默认序列化是 jsonb 'null'
    # (非 SQL NULL),对象 || 'null' 在 PG 里按数组拼接 → facts 变 list(实测踩雷);
    # typeof 对 SQL NULL 返 NULL、对 jsonb 'null' 返 'null',两种"空"都会落到保留旧值分支。
    from sqlalchemy import case, func

    _excluded_is_obj = func.jsonb_typeof(stmt.excluded.facts) == "object"
    _existing_is_obj = func.jsonb_typeof(Item.facts) == "object"
    merged_facts = case(
        (_excluded_is_obj & _existing_is_obj, Item.facts.op("||")(stmt.excluded.facts)),
        (_excluded_is_obj, stmt.excluded.facts),  # 旧值缺/非对象 → 直接取新对象
        else_=Item.facts,  # 新值缺/非对象 → 保留旧 facts(不整块置空)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Item.item_id],
        set_={
            "cluster_id": stmt.excluded.cluster_id,
            "category": stmt.excluded.category,
            "facts": merged_facts,
            "content": stmt.excluded.content,
        },
    )
    await session.execute(stmt)
    return item_id


async def add_item_timeline(
    session: AsyncSession, *, item_id: str, run_id: str | None = None,
    trigger_type: str | None = None, rank: int | None = None, stars: int | None = None,
) -> None:
    """追加一条时间轴快照(observed_at 由库 now() 填)。GitHub 热榜每跑记 star+rank,供日后算增速。

    纯追加(不 upsert):同一 item 跨天多行 = 它的轨迹,正是增速 delta 的数据来源。
    """
    session.add(ItemTimeline(
        item_id=item_id, run_id=run_id, trigger_type=trigger_type, rank=rank, stars=stars,
    ))


async def get_latest_timeline_stars(
    session: AsyncSession, item_ids: list[str]
) -> dict[str, tuple[int, datetime]]:
    """批量取每个 item 最近一份 star 快照 → {item_id: (stars, observed_at)}。

    用于 GitHub 热榜算**近期涨速** delta:(当前 stars − 上一份快照 stars) / 隔了几天。
    带上 observed_at 才能按真实间隔归一(漏跑几天不会把涨速虚高)。无快照的 item 不在返回里。
    本跑写入自己的快照之前调用,故"最近一份"即上一跑的快照。
    """
    if not item_ids:
        return {}
    from sqlalchemy import bindparam
    from sqlalchemy import text as sa_text

    stmt = sa_text(
        """
        SELECT DISTINCT ON (item_id) item_id, stars, observed_at
        FROM item_timeline
        WHERE item_id IN :ids AND stars IS NOT NULL
        ORDER BY item_id, observed_at DESC, id DESC
        """
    ).bindparams(bindparam("ids", expanding=True))
    rows = (await session.execute(stmt, {"ids": list(item_ids)})).all()
    return {r.item_id: (int(r.stars), r.observed_at) for r in rows}


async def get_item(session: AsyncSession, item_id: str) -> Item | None:
    return await session.get(Item, item_id)


async def create_run(
    session: AsyncSession, *, trigger_type: str, run_id: str | None = None, meta: dict | None = None
) -> str:
    """开一次 run,返回 run_id。"""
    run_id = run_id or uuid.uuid4().hex
    session.add(Run(run_id=run_id, trigger_type=trigger_type, status="running", meta=meta))
    await session.flush()
    return run_id


async def finish_run(
    session: AsyncSession, run_id: str, *, status: str, stage: str | None = None, error: str | None = None
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"run 不存在:{run_id}")
    run.status = status
    run.stage = stage
    run.error = error
    run.finished_at = datetime.now(timezone.utc)


async def get_run(session: AsyncSession, run_id: str) -> Run | None:
    """取一次 run 的检查点状态(不存在返回 None)。"""
    return await session.get(Run, run_id)


async def set_run_stage(session: AsyncSession, run_id: str, stage: str) -> None:
    """推进 run 的检查点:记录最后完成的阶段(保持 status=running,不算结束)。

    断点续跑据此跳过已完成阶段。各站本身 upsert/幂等,续跑不会重复产数据。
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"run 不存在:{run_id}")
    run.stage = stage


async def record_delivery(
    session: AsyncSession,
    *,
    cluster_id: str,
    channel: str,
    trigger_type: str,
    run_id: str | None = None,
    status: str = "sent",
) -> bool:
    """登记一次投递。唯一键 cluster_id+channel+trigger_type 已存在则不插入。

    返回 True=本次新登记(应投递);False=重复(应跳过,挡重复推)。
    """
    stmt = (
        pg_insert(Delivery)
        .values(
            cluster_id=cluster_id,
            channel=channel,
            trigger_type=trigger_type,
            run_id=run_id,
            status=status,
        )
        .on_conflict_do_nothing(constraint="uq_delivery_idempotency")
        .returning(Delivery.id)
    )
    result = await session.execute(stmt)
    return result.first() is not None


async def has_delivery(
    session: AsyncSession, *, cluster_id: str, channel: str, trigger_type: str
) -> bool:
    """是否已成功投递过(幂等读:挡重复推)。cluster_id 槽位日报用 interest_key:date。"""
    stmt = select(Delivery.id).where(
        Delivery.cluster_id == cluster_id,
        Delivery.channel == channel,
        Delivery.trigger_type == trigger_type,
        Delivery.status == "sent",
    ).limit(1)
    return (await session.execute(stmt)).first() is not None


async def count_items(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Item))).scalar_one()


async def get_items(
    session: AsyncSession, *, since: datetime | None = None, limit: int | None = None
) -> list[Item]:
    """取条目(可按发布时间下限过滤),按发布时间降序。富化/排序遍历用。"""
    stmt = select(Item)
    if since is not None:
        stmt = stmt.where(Item.published_at >= since)
    stmt = stmt.order_by(Item.published_at.desc().nulls_last(), Item.item_id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def update_item_facts(session: AsyncSession, *, item_id: str, facts: dict) -> None:
    """整列覆盖写回 item.facts(富化把派生事实合并进去后调用)。"""
    await session.execute(update(Item).where(Item.item_id == item_id).values(facts=facts))


async def get_fulltext_candidates(
    session: AsyncSession,
    *,
    source_ids: list[str],
    fetched_since: datetime,
    max_content_chars: int,
    limit: int,
) -> list[Item]:
    """按源全文富化的候选条目(2026-07 P1):flagged 源 + 近期抓到 + 正文瘦 + 还没抓过全文。

    过滤全压进 SQL(便宜),新抓的优先;limit = 每 run 全文抓取硬顶(有界成本)。
    "还没抓过全文" 对 facts 为 SQL NULL / jsonb 'null' / 缺 fulltext 键 三种形态都成立。
    """
    from sqlalchemy import func, or_

    if not source_ids:
        return []
    stmt = (
        select(Item)
        .where(
            Item.source.in_(source_ids),
            Item.fetched_at >= fetched_since,
            func.coalesce(func.length(Item.content), 0) < max_content_chars,
            or_(Item.facts.is_(None), ~Item.facts.has_key("fulltext")),
        )
        .order_by(Item.fetched_at.desc(), Item.item_id.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# 去重 / 聚类(阶段 3)                                                          #
# --------------------------------------------------------------------------- #
async def get_unclustered_items(
    session: AsyncSession, *, since: datetime | None = None, limit: int | None = None
) -> list[Item]:
    """取尚未归簇的条目,按发布时间升序(首条派生 cluster_id,跨天稳定)。"""
    stmt = select(Item).where(Item.cluster_id.is_(None))
    if since is not None:
        stmt = stmt.where(Item.published_at >= since)
    stmt = stmt.order_by(Item.published_at.asc().nulls_last(), Item.item_id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_recent_items_by_sources(
    session: AsyncSession, *, sources: list[str], since: datetime, limit: int = 60
) -> list[Item]:
    """取指定源在近期(published_at>=since 或无日期)的条目,按发布时间降序。

    白名单直通用:把高价值源的近期条目拉进精排候选池(不依赖语义召回排名)。
    """
    if not sources:
        return []
    stmt = (
        select(Item)
        .where(Item.source.in_(sources))
        .where((Item.published_at >= since) | (Item.published_at.is_(None)))
        .order_by(Item.published_at.desc().nulls_last(), Item.item_id.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_recent_embeddings(
    session: AsyncSession, *, since: datetime
) -> list[tuple[str, str, list[float]]]:
    """取近期(published_at>=since)条目的 (item_id, source, 向量)。

    事件热度用:近窗全量向量做"多少个不同源在报相似内容"的计数。
    无日期条目不参与热度(无法判断是否"正在发生")。
    """
    stmt = (
        select(Item.item_id, Item.source, Embedding.embedding)
        .join(Embedding, Embedding.item_id == Item.item_id)
        .where(Item.published_at >= since)
    )
    rows = (await session.execute(stmt)).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def get_source_item_stats(
    session: AsyncSession,
) -> list[tuple[str, int, "datetime | None"]]:
    """每个 source 在 items 表里的 (source, 条目数, 最近 published_at),按条目数降序。源治理体检用。"""
    stmt = (
        select(Item.source, func.count(), func.max(Item.published_at))
        .group_by(Item.source)
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def delete_orphan_items(session: AsyncSession, registry_ids: list[str]) -> int:
    """删掉 source 不在注册表里的孤儿条目(已下线源的残留)。返回删除数。

    级联:items 的依赖(embeddings/summaries/rankings/item_timeline)外键均 ON DELETE CASCADE,
    随之清掉;不碰 threads/thread_clusters(在追线读耐久落痕,与 items 解耦)。
    **安全闸**:registry_ids 为空 → 不删(防注册表加载异常时把全库清空)。源治理用,destructive。
    """
    if not registry_ids:
        return 0
    from sqlalchemy import delete as _delete

    res = await session.execute(_delete(Item).where(Item.source.not_in(registry_ids)))
    return res.rowcount or 0


async def get_embeddings_by_ids(
    session: AsyncSession, item_ids: list[str]
) -> dict[str, list[float]]:
    """取指定条目的向量 {item_id: 向量}(选稿同事件去重用,覆盖整个候选集,不受时间窗限制)。

    rank 选稿去重要对齐召回窗(720h):只按 36h 热度窗取向量会让窗外候选无向量而绕过去重、
    同事件重复刷屏(2026-06-15 二①)。无 embedding 的条目不在返回里(调用方按缺失处理)。
    """
    if not item_ids:
        return {}
    stmt = select(Embedding.item_id, Embedding.embedding).where(Embedding.item_id.in_(item_ids))
    rows = (await session.execute(stmt)).all()
    return {r[0]: r[1] for r in rows}


async def upsert_embedding(
    session: AsyncSession, *, item_id: str, vector: list[float], model: str
) -> None:
    stmt = pg_insert(Embedding).values(item_id=item_id, embedding=vector, model=model)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Embedding.item_id],
        set_={"embedding": stmt.excluded.embedding, "model": stmt.excluded.model},
    )
    await session.execute(stmt)


async def find_fingerprint_cluster(
    session: AsyncSession, *, fingerprint: str, exclude_item_id: str
) -> str | None:
    """二级:内容指纹完全相同且已归簇的条目 → 复用其 cluster_id。"""
    stmt = (
        select(Item.cluster_id)
        .where(
            Item.content_fingerprint == fingerprint,
            Item.cluster_id.is_not(None),
            Item.item_id != exclude_item_id,
        )
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    return row[0] if row else None


async def find_similar_cluster(
    session: AsyncSession,
    *,
    vector: list[float],
    threshold: float,
    since: datetime | None = None,
    exclude_item_id: str,
) -> tuple[str, float] | None:
    """三级:pgvector 余弦最近邻里,已归簇、相似度 ≥ threshold、在窗口内的条目 → 返回 (cluster_id, 相似度)。"""
    distance = Embedding.embedding.cosine_distance(vector)
    similarity = (1 - distance).label("sim")
    stmt = (
        select(Item.cluster_id, similarity)
        .join(Embedding, Embedding.item_id == Item.item_id)
        .where(
            Item.cluster_id.is_not(None),
            Item.item_id != exclude_item_id,
            (1 - distance) >= threshold,
        )
    )
    if since is not None:
        stmt = stmt.where(Item.published_at >= since)
    stmt = stmt.order_by(distance.asc()).limit(1)
    row = (await session.execute(stmt)).first()
    return (row[0], float(row[1])) if row else None


async def recall_by_vector(
    session: AsyncSession,
    *,
    vector: list[float],
    limit: int,
    since: datetime | None = None,
) -> list[tuple[Item, float]]:
    """兴趣向量的 pgvector 余弦近邻召回(复用去重向量)。返回 [(Item, 相似度)],相似度降序。"""
    from sqlalchemy import text as sa_text

    distance = Embedding.embedding.cosine_distance(vector)
    similarity = (1 - distance).label("sim")
    stmt = select(Item, similarity).join(Embedding, Embedding.item_id == Item.item_id)
    if since is not None:
        # HNSW 先取近邻再过滤:默认 ef_search=40,时间过滤后会"饿死"(召回 120 实际只剩个位数)。
        # 修法(pgvector 0.8+):加大初始近邻 + 迭代扫描——结果不足 limit 时自动继续向外搜。
        # SET LOCAL 只影响当前事务,不污染连接池里的其他会话。
        await session.execute(sa_text("SET LOCAL hnsw.ef_search = 400"))
        await session.execute(sa_text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        # 近 N 小时内 或 无发布时间(GitHub 等无日期源不武断排除,由 rank 老项限额兜底)
        stmt = stmt.where((Item.published_at >= since) | (Item.published_at.is_(None)))
    stmt = stmt.order_by(distance.asc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    # relaxed_order 迭代扫描可能返回轻微乱序 → Python 端按相似度重排,保住"降序"契约
    out = [(row[0], float(row[1])) for row in rows]
    out.sort(key=lambda t: t[1], reverse=True)
    return out


async def recall_cards_by_vector(
    session: AsyncSession,
    *,
    vector: list[float],
    limit: int,
    relevance_floor: float = 0.0,
    since: datetime | None = None,
) -> list[tuple[Summary, float]]:
    """语义问答召回:summaries.card_vec 余弦近邻,返回 [(Summary, 相似度)] 降序。

    查的是**已发布的卡**(summaries),不是 item 标题向量(recall_by_vector 那张),join 路径不同。
    过滤:card_vec 已落 + produced_by='pulsewire'(排除旧系统遗留,闭 codex M1)。
    ⚠️ **软删卡(pruned_at)照样召回**:pulsewire 软删=移出"今日报",但它们正是历史档案主体
    (实测 409/485 是软删的);问历史就是要问它们,绝不能排除。
    relevance_floor:余弦低于此=不相关,不返回(防硬凑无关卡喂 LLM 编)。since:可选时间窗。
    """
    from sqlalchemy import text as sa_text

    distance = Summary.card_vec.cosine_distance(vector)
    similarity = (1 - distance).label("sim")
    stmt = (
        select(Summary, similarity)
        .where(Summary.card_vec.isnot(None))
        .where(Summary.produced_by == "pulsewire")
    )
    if since is not None:
        # 同 recall_by_vector:HNSW 时间过滤会饿死,放大初始近邻 + 迭代扫描
        await session.execute(sa_text("SET LOCAL hnsw.ef_search = 400"))
        await session.execute(sa_text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
        stmt = stmt.where(Summary.created_at >= since)
    stmt = stmt.order_by(distance.asc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    out = [(row[0], float(row[1])) for row in rows if float(row[1]) >= relevance_floor]
    out.sort(key=lambda t: t[1], reverse=True)
    return out


async def upsert_ranking(
    session: AsyncSession,
    *,
    interest_key: str,
    interest: str,
    tags: list[str] | None,
    item_id: str,
    cluster_id: str | None,
    recall_score: float,
    rule_score: float,
    rerank_score: float | None,
    final_score: float,
    rank: int,
    provider: str,
    run_id: str | None = None,
    meta: dict | None = None,
) -> None:
    """写入/覆盖一条精排结果(唯一键 interest_key+item_id,重跑幂等)。

    meta:闸判定的下游随行数据(目前只有已剪记忆 clip);冲突更新时**无条件覆盖**——
    同条目今天没标记(None)就要把昨天残留的旧标记冲掉,不能让旧 clip 冒充今天。
    """
    stmt = pg_insert(Ranking).values(
        interest_key=interest_key,
        interest=interest,
        tags={"tags": tags} if tags else None,
        item_id=item_id,
        cluster_id=cluster_id,
        recall_score=recall_score,
        rule_score=rule_score,
        rerank_score=rerank_score,
        final_score=final_score,
        rank=rank,
        provider=provider,
        run_id=run_id,
        meta=meta,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_ranking_interest_item",
        set_={
            "interest": stmt.excluded.interest,
            "tags": stmt.excluded.tags,
            "cluster_id": stmt.excluded.cluster_id,
            "recall_score": stmt.excluded.recall_score,
            "rule_score": stmt.excluded.rule_score,
            "rerank_score": stmt.excluded.rerank_score,
            "final_score": stmt.excluded.final_score,
            "rank": stmt.excluded.rank,
            "provider": stmt.excluded.provider,
            "run_id": stmt.excluded.run_id,
            "meta": stmt.excluded.meta,  # 无条件覆盖:今天没标记(None)必须冲掉昨天残留的旧 clip
            "decided_at": func.now(),
        },
    )
    await session.execute(stmt)


async def get_items_by_ids(session: AsyncSession, item_ids: list[str]) -> list[Item]:
    """按 id 批量取条目(总结用)。"""
    if not item_ids:
        return []
    stmt = select(Item).where(Item.item_id.in_(item_ids))
    return list((await session.execute(stmt)).scalars().all())


async def get_rankings(session: AsyncSession, *, interest_key: str) -> list[Ranking]:
    """取某兴趣的精排结果,按名次升序(总结的输入)。"""
    stmt = (
        select(Ranking)
        .where(Ranking.interest_key == interest_key)
        .order_by(Ranking.rank.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_summary(
    session: AsyncSession,
    *,
    interest_key: str,
    item_id: str,
    cluster_id: str | None,
    headline: str,
    tldr_raw: str,
    tldr_rendered: str,
    insight_raw: str,
    insight_rendered: str,
    status: str,
    used_source_ids: list[str],
    unresolved: list[str],
    suspect: list[str],
    backend: str,
    model: str | None,
    run_id: str | None = None,
    card_vec: list[float] | None = None,
) -> None:
    """写入/覆盖一条总结(唯一键 interest_key+item_id,重跑幂等)。

    card_vec(v2 主线B②):本轮成稿当场算的卡向量,供语义问答召回;同时打 produced_by='pulsewire'
    (recall_cards_by_vector 的硬过滤,不打则召不回)。embed 失败传 None → 新卡 card_vec 留空、
    旧卡向量保留不动(交 `pulsewire embed-cards` 幂等兜底),绝不拖垮日报落库。
    """
    stmt = pg_insert(Summary).values(
        interest_key=interest_key, item_id=item_id, cluster_id=cluster_id,
        headline=headline, tldr_raw=tldr_raw, tldr_rendered=tldr_rendered,
        insight_raw=insight_raw, insight_rendered=insight_rendered,
        status=status,
        used_source_ids={"ids": used_source_ids} if used_source_ids else None,
        unresolved={"tokens": unresolved} if unresolved else None,
        suspect={"numbers": suspect} if suspect else None,
        backend=backend, model=model, run_id=run_id,
        card_vec=card_vec, produced_by="pulsewire",
    )
    set_ = {
        "cluster_id": stmt.excluded.cluster_id,
        "headline": stmt.excluded.headline,
        "tldr_raw": stmt.excluded.tldr_raw,
        "tldr_rendered": stmt.excluded.tldr_rendered,
        "insight_raw": stmt.excluded.insight_raw,
        "insight_rendered": stmt.excluded.insight_rendered,
        "status": stmt.excluded.status,
        "used_source_ids": stmt.excluded.used_source_ids,
        "unresolved": stmt.excluded.unresolved,
        "suspect": stmt.excluded.suspect,
        "backend": stmt.excluded.backend,
        "model": stmt.excluded.model,
        "run_id": stmt.excluded.run_id,
        "created_at": func.now(),
        "pruned_at": None,  # 重新产出同条目 → 复活(撤销软删,2026-06-15 二⑦)
        "produced_by": "pulsewire",
    }
    # card_vec 仅在本轮成功算出时覆盖;失败(None)则不动旧向量,免把已有好向量洗成 NULL。
    if card_vec is not None:
        set_["card_vec"] = stmt.excluded.card_vec
    stmt = stmt.on_conflict_do_update(constraint="uq_summary_interest_item", set_=set_)
    await session.execute(stmt)


async def get_summaries(
    session: AsyncSession, *, interest_key: str, run_id: str | None = None
) -> list[Summary]:
    """取某兴趣的全部【未软删】条目总结(出图/交付用)。软删行(pruned_at 非空)不出。

    run_id 非空 → 只取本次 run 产的稿(治 f04:某域今日 summarize 失败时,别拿上次
    残留的旧稿冒充今天出图/交付)。缺省 None = 不限 run(在追归线/历史召回等按 interest_key 全取)。
    """
    stmt = (
        select(Summary)
        .where(Summary.interest_key == interest_key)
        .where(Summary.pruned_at.is_(None))
    )
    if run_id is not None:
        stmt = stmt.where(Summary.run_id == run_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_digest(
    session: AsyncSession, *, interest_key: str, run_id: str | None = None
) -> Digest | None:
    """取某兴趣的日报概述(单行,每次 run 覆盖)。

    run_id 非空且现存概述非本次 run 产 → 返 None(治 f04:概述这轮没写成时,别拿昨天的冒充今天)。
    """
    row = await session.get(Digest, interest_key)
    if run_id is not None and row is not None and row.run_id != run_id:
        return None
    return row


async def upsert_digest(
    session: AsyncSession,
    *,
    interest_key: str,
    digest: str,
    backend: str,
    model: str | None,
    run_id: str | None = None,
) -> None:
    """写入/覆盖某兴趣的日报概述。"""
    stmt = pg_insert(Digest).values(
        interest_key=interest_key, digest=digest, backend=backend, model=model, run_id=run_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Digest.interest_key],
        set_={
            "digest": stmt.excluded.digest,
            "backend": stmt.excluded.backend,
            "model": stmt.excluded.model,
            "run_id": stmt.excluded.run_id,
            "created_at": func.now(),
        },
    )
    await session.execute(stmt)


async def prune_rankings(
    session: AsyncSession, *, interest_key: str, keep_item_ids: list[str]
) -> int:
    """删掉某兴趣下不在最新结果集里的旧排名行(重跑时清理被挤出 top-N 的陈旧行)。返回删除数。"""
    from sqlalchemy import delete

    stmt = delete(Ranking).where(Ranking.interest_key == interest_key)
    if keep_item_ids:
        stmt = stmt.where(Ranking.item_id.not_in(keep_item_ids))
    result = await session.execute(stmt)
    return result.rowcount or 0


async def prune_summaries(
    session: AsyncSession, *, interest_key: str, keep_item_ids: list[str]
) -> int:
    """软删某兴趣下不在本次产出里的旧总结行(打 pruned_at,不物理删)。返回软删数。

    分块总结时某块重试耗尽会跳过其条目(不产新总结);若这些条目上一轮有旧总结,
    不清就会被 deliver/render 当本轮内容上线(冒充)。本次成功产出的条目=keep,其余软删。
    keep 为空(全块失败)时不删——交由上层冒泡,不静默清空。
    软删(2026-06-15 二⑦):打 pruned_at 保留行,丢数据可追溯/可恢复;get_summaries 过滤掉,
    重新产出同条目时 upsert 复活(置回 NULL)。只标尚未软删的行(幂等,不刷已删行的时间戳)。
    """
    if not keep_item_ids:
        return 0
    stmt = (
        update(Summary)
        .where(Summary.interest_key == interest_key)
        .where(Summary.item_id.not_in(keep_item_ids))
        .where(Summary.pruned_at.is_(None))
        .values(pruned_at=func.now())
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def create_cluster(
    session: AsyncSession,
    *,
    cluster_id: str,
    first_item_id: str,
    title: str | None,
    seen_at: datetime | None,
) -> None:
    stmt = pg_insert(Cluster).values(
        cluster_id=cluster_id,
        first_item_id=first_item_id,
        title=title,
        source_count=1,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=[Cluster.cluster_id])
    await session.execute(stmt)


async def assign_cluster(session: AsyncSession, *, item_id: str, cluster_id: str) -> None:
    await session.execute(
        update(Item).where(Item.item_id == item_id).values(cluster_id=cluster_id)
    )


async def refresh_cluster(session: AsyncSession, cluster_id: str) -> int:
    """重算簇的 source_count(不同源数,大事判定用)与首末出现时间。返回 source_count。"""
    count_sub = (
        select(func.count(func.distinct(Item.source)))
        .where(Item.cluster_id == cluster_id)
        .scalar_subquery()
    )
    first_sub = (
        select(func.min(Item.published_at)).where(Item.cluster_id == cluster_id).scalar_subquery()
    )
    last_sub = (
        select(func.max(Item.published_at)).where(Item.cluster_id == cluster_id).scalar_subquery()
    )
    await session.execute(
        update(Cluster)
        .where(Cluster.cluster_id == cluster_id)
        .values(source_count=count_sub, first_seen_at=first_sub, last_seen_at=last_sub)
    )
    return (
        await session.execute(
            select(Cluster.source_count).where(Cluster.cluster_id == cluster_id)
        )
    ).scalar_one()


# ---------- 事件线(threads / thread_clusters)step 3 ---------- #
async def get_active_threads(session: AsyncSession, *, domain: str) -> list[Thread]:
    """取某领域在追(active)的事件线,供 A 层匹配候选 + B 判官上下文。"""
    stmt = select(Thread).where(Thread.domain == domain, Thread.status == "active")
    return list((await session.execute(stmt)).scalars().all())


async def linked_cluster_ids(session: AsyncSession, cluster_ids: list[str]) -> set[str]:
    """这些簇里哪些已挂过线(任一条线)——归线幂等用,已挂的跳过不重复处理。"""
    if not cluster_ids:
        return set()
    stmt = select(ThreadCluster.cluster_id).where(ThreadCluster.cluster_id.in_(cluster_ids))
    return set((await session.execute(stmt)).scalars().all())


async def create_thread(
    session: AsyncSession, *, thread_id: str, name: str | None, subject: str, domain: str,
    summary: str | None, seen_at: datetime, heat: int = 1,
) -> str:
    """新开一条事件线。first/last_seen_at 同设为 seen_at。"""
    session.add(Thread(
        thread_id=thread_id, name=name, subject=subject, domain=domain, status="active",
        summary=summary, heat=heat, first_seen_at=seen_at, last_seen_at=seen_at,
    ))
    return thread_id


async def link_cluster_to_thread(
    session: AsyncSession, *, thread_id: str, cluster_id: str, run_id: str | None,
    subject: str | None, link_reason: str, confidence: float | None,
    headline: str | None = None, url: str | None = None, source: str | None = None,
    progress_date: str | None = None,
) -> None:
    """把簇挂到线(写 thread_clusters,兼判定日志 + 耐久落痕)。

    headline/url/source/progress_date = 当天进展落痕(时间轴从这读,不随 summaries 删除而丢失)。
    ON CONFLICT DO NOTHING:同簇同线重复挂载静默跳过,不让一条坏插入回滚整域事务(防御性幂等)。
    """
    stmt = pg_insert(ThreadCluster).values(
        thread_id=thread_id, cluster_id=cluster_id, run_id=run_id,
        subject=subject, link_reason=link_reason, confidence=confidence,
        headline=headline, url=url, source=source, progress_date=progress_date,
    ).on_conflict_do_nothing(constraint="uq_thread_cluster")
    await session.execute(stmt)


async def touch_thread(
    session: AsyncSession, *, thread_id: str, seen_at: datetime, summary: str | None,
    heat_delta: int, name: str | None = None,
) -> None:
    """簇并入既有线后:刷新 last_seen_at / summary(现状一句话)/ heat 累加,复活为 active。

    name 非空时一并把线名刷成最新簇 headline(2026-06-15 二②:原线名建线即定死最老 headline、
    现状却随最新簇刷新 → 标题对不上正文;现在标题/现状同来自最新簇)。
    """
    values: dict = dict(
        last_seen_at=seen_at, summary=summary, status="active", heat=Thread.heat + heat_delta,
    )
    if name is not None:
        values["name"] = name
    await session.execute(
        update(Thread).where(Thread.thread_id == thread_id).values(**values)
    )


async def mark_dormant_threads(session: AsyncSession, *, domain: str, before: datetime) -> int:
    """某领域 last_seen_at 早于 before 的 active 线 → dormant(前端折叠)。返回置休条数。"""
    res = await session.execute(
        update(Thread)
        .where(Thread.domain == domain, Thread.status == "active", Thread.last_seen_at < before)
        .values(status="dormant")
    )
    return res.rowcount or 0


def _progress_date(run_id: str | None, linked_at: datetime | None, tz) -> str:
    """一个挂载点的「进展日期」(YYYY-MM-DD)。

    优先用 run_id(`daily_YYYYMMDD` = 该簇出现在哪天的日报),这才是"这条进展发生在哪天"的真相;
    种子跑里同日簇都落同一天、未来每天跑各加一天的点。run_id 缺/异常时退回 linked_at 的本地日期。
    """
    rid = run_id or ""
    if rid.startswith("daily_") and len(rid) >= 14 and rid[6:14].isdigit():
        ymd = rid[6:14]
        return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    if linked_at is not None:
        return linked_at.astimezone(tz).strftime("%Y-%m-%d")
    return ""


async def clear_threads(session: AsyncSession) -> tuple[int, int]:
    """清空 threads + thread_clusters(--rebuild 重算前)。线是派生数据,清了可重建。

    先删挂载(thread_clusters,FK 子表)再删线;返回 (删除挂载数, 删除线数)。
    """
    from sqlalchemy import delete as _delete

    nlinks = (await session.execute(_delete(ThreadCluster))).rowcount or 0
    nthreads = (await session.execute(_delete(Thread))).rowcount or 0
    return nlinks, nthreads


async def get_threads_for_display(
    session: AsyncSession, *, min_days: int = 2, limit: int = 60, tz_name: str = "Asia/Shanghai",
) -> list[dict]:
    """「在追」视图数据:active 线 + 每线跨天进展时间轴(每点 date/headline/url/source,新到旧)。

    每点的文案/链接/日期从 thread_clusters 的耐久落痕读(headline/url/source/progress_date,挂线时冻结,
    不随 summaries 删除而丢失);落痕缺失的旧行退回 clusters.title + 由 run_id/linked_at 推日期。
    露出门槛:线须跨 >= min_days 个不同进展日期(防单日小新闻刷屏)。线按 heat 降序、last_seen 降序。
    只读不写。空返回 = 暂无跨天线(正常,非错误)。
    """
    from collections import defaultdict
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)

    threads = list((await session.execute(
        select(Thread)
        .where(Thread.status == "active")
        .order_by(Thread.heat.desc(), Thread.last_seen_at.desc())
    )).scalars().all())
    if not threads:
        return []
    tids = [t.thread_id for t in threads]

    links = list((await session.execute(
        select(
            ThreadCluster.thread_id, ThreadCluster.cluster_id, ThreadCluster.run_id,
            ThreadCluster.linked_at, ThreadCluster.headline, ThreadCluster.url,
            ThreadCluster.source, ThreadCluster.progress_date,
        ).where(ThreadCluster.thread_id.in_(tids))
    )).all())

    # 退路:落痕缺 headline 的旧行,用 clusters.title 兜底(durable)
    need_title = [link.cluster_id for link in links if not link.headline]
    title_by_cluster: dict[str, str] = {}
    if need_title:
        rows = (await session.execute(
            select(Cluster.cluster_id, Cluster.title).where(Cluster.cluster_id.in_(need_title))
        )).all()
        title_by_cluster = {r.cluster_id: r.title for r in rows}

    points: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        headline = link.headline or title_by_cluster.get(link.cluster_id)
        if not headline:
            continue  # 无任何文案可展示,跳过该点
        date = link.progress_date or _progress_date(link.run_id, link.linked_at, tz)
        points[link.thread_id].append({
            "date": date, "headline": headline, "url": link.url, "source": link.source,
        })

    out: list[dict] = []
    for t in threads:
        pts = points.get(t.thread_id, [])
        days = {p["date"] for p in pts if p["date"]}
        if len(days) < min_days:
            continue
        pts.sort(key=lambda p: p["date"], reverse=True)  # 新到旧
        out.append({
            "thread_id": t.thread_id,
            "name": t.name or t.subject or "(未命名事件线)",
            "domain": t.domain or "",
            "summary": t.summary or "",
            "heat": t.heat,
            "days": len(days),
            "timeline": pts,
        })
        if len(out) >= limit:
            break
    return out


async def get_active_thread_cluster_map(session: AsyncSession) -> dict[str, str]:
    """active 事件线的 cluster_id → thread_id 映射(日报「持续关注」徽标用)。

    与 get_threads_for_display 的天数门槛配合:本函数只给"簇属于哪条 active 线",
    天数(露出与否)由调用方拿已过门槛的 threads 交叉判定。一个簇正常只挂一条线;
    若意外多挂,后者覆盖(徽标只需任一所属线)。只读。
    """
    rows = (await session.execute(
        select(ThreadCluster.cluster_id, ThreadCluster.thread_id)
        .join(Thread, Thread.thread_id == ThreadCluster.thread_id)
        .where(Thread.status == "active")
    )).all()
    return {r.cluster_id: r.thread_id for r in rows}


# --------------------------------------------------------------------------- #
# 判决缓存(S1):判官裁决按 (item_hash, judge_name, prompt_hash) 落库复用            #
# --------------------------------------------------------------------------- #
async def get_cached_judgments(
    session: AsyncSession, *, judge_name: str, prompt_hash: str, item_hashes: list[str]
) -> dict[str, dict]:
    """按 (judge_name, prompt_hash) + 一批 item_hash 拉已有裁决 → {item_hash: verdict}。

    预载:判官层跑之前一次查好,命中的条目直接读裁决不调 LLM(治 f20 逐日重判白烧钱)。
    """
    if not item_hashes:
        return {}
    stmt = select(Judgment.item_hash, Judgment.verdict).where(
        Judgment.judge_name == judge_name,
        Judgment.prompt_hash == prompt_hash,
        Judgment.item_hash.in_(list(item_hashes)),
    )
    return {h: v for h, v in (await session.execute(stmt)).all()}


async def upsert_judgments(session: AsyncSession, rows: list[dict]) -> int:
    """批量写入判决缓存(item_hash/judge_name/prompt_hash/verdict);冲突=同键已存 → 跳过。

    rows: [{"item_hash","judge_name","prompt_hash","verdict"}]。裁决对同键稳定,故 do_nothing。
    """
    if not rows:
        return 0
    stmt = pg_insert(Judgment).values(rows).on_conflict_do_nothing(constraint="uq_judgment_key")
    await session.execute(stmt)
    return len(rows)
