"""兴趣分类引擎:自然语言兴趣 → 标签 → embedding 召回 → 规则粗排 → LLM 精排 + 新鲜度门 + 限额。

流程:
1. 把"兴趣 + 标签"算成向量(复用去重的 embedder)。
2. pgvector 余弦近邻召回粗筛(复用去重向量,recall_limit 条)
   + 白名单直通(高价值源近期条目)+ 热点直通(多源同报的事件代表,不走兴趣相似度)。
3. **新鲜度门**:按各源 `freshness_hours` 丢掉过期条目(逐源不同)。
4. 规则粗排:终分粗算 = w·(召回相似度 / 源权重 / 新鲜度 / 大事信号[簇源数与热度取大])。
5. **LLM 主编选题**(deepseek 后端):模型综合新闻价值(重大性/多源热度/一手优先/实质内容)
   + 兴趣相关性打分,终分 = blend·LLM + (1-blend)·规则分。
   rule 后端:跳过 LLM,终分=规则分(无 key 也能跑)。
6. **限额**:各类限额 + 单源限额 + 老项限额,贪心取前 final_limit。
候选条目都带富化事实(value+source_id),可回源对账。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.store import (
    get_sessionmaker,
    prune_rankings,
    recall_by_vector,
    upsert_ranking,
)

from .rerank import llm_rerank

if TYPE_CHECKING:
    from pulsewire.config import RankCfg, Settings, Source
    from pulsewire.store.tables import Item

log = get_logger()


@dataclass(slots=True)
class Candidate:
    item_id: str
    title: str
    source: str
    category: str | None
    cluster_id: str | None
    published_at: datetime | None
    source_count: int  # 簇内不同源数(大事信号);未归簇=1
    source_weight: float
    freshness_hours: int
    enriched: list[dict]  # 富化事实(value+source_id),可回源对账
    recall_sim: float
    whitelisted: bool = False
    heat: int = 1  # 事件热度:近窗内多少个不同源在报相似内容(1=只有自己)
    tracking: bool = False  # 该簇是否属于一条多天 active 在追线(持续发酵的大事 → 规则分加分)
    rule_score: float = 0.0
    rerank_score: float | None = None
    final_score: float = 0.0


def interest_key(interest: str, tags: list[str] | None) -> str:
    """由兴趣 + 标签派生稳定 key(不让模型编;同一兴趣重跑幂等覆盖)。"""
    payload = f"{interest.strip().lower()}|{','.join(sorted(t.lower() for t in (tags or [])))}"
    return "int_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _age_hours(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return float("inf")
    return (now - published_at).total_seconds() / 3600.0


def passes_freshness(cand: Candidate, now: datetime) -> bool:
    """新鲜度门:发布时间在该源 freshness_hours 之内才保留。

    无发布时间(GitHub 趋势/部分 feed 不给日期)→ 不武断判旧、放行;
    age=inf 会让它在 apply_quotas 里算作"老项",受 old_item_limit 兜底封顶,不会刷屏。
    """
    if cand.published_at is None:
        return True
    return _age_hours(cand.published_at, now) <= cand.freshness_hours


def rule_score(cand: Candidate, weights: RankCfg, now: datetime, event_min_sources: int) -> float:
    """规则粗排分(纯函数):召回相似度 / 源权重 / 新鲜度 / 大事信号 / 持续关注 的加权和。"""
    w = weights.weights
    age = _age_hours(cand.published_at, now)
    recency = max(0.0, 1.0 - age / cand.freshness_hours) if cand.freshness_hours else 0.0
    # 大事信号取簇内源数与事件热度的较大者(热度补齐"同事件不同措辞不合簇"的盲区)
    convergence = max(cand.source_count, cand.heat)
    event = min(convergence / event_min_sources, 1.0) if event_min_sources else 0.0
    return (
        w.recall * cand.recall_sim
        + w.source_weight * cand.source_weight
        + w.recency * recency
        + w.event * event
        + w.thread * (1.0 if cand.tracking else 0.0)  # 持续关注:多天在追的发酵大事加分(step6-B)
    )


def apply_quotas(
    ranked: list[Candidate],
    cfg: RankCfg,
    now: datetime,
    vectors: dict[str, "object"] | None = None,
    same_event=None,
) -> list[Candidate]:
    """限额(纯函数):各类限额 + 单源限额 + 老项限额 + 同事件去重,按终分降序贪心取前 final_limit。

    vectors:{item_id: 归一化向量}(可缺省/部分缺省)。提供时,与已选条目相似度
    ≥ cfg.select_sim_dedup 的候选视为同一事件跳过——头条会被 N 个源重复报道
    (镜像源/同文多 feed),不抑制的话同一事件能霸占半个日报。
    same_event:可选回调 (cand_a, cand_b)->bool。提供时,余弦落在
    [event_dedup_min_sim, select_sim_dedup) 中等相似带的候选,再问它是否同一件事,
    是→折叠(方案 B,补词法折叠看不出的"同事件不同角度")。缺省=纯词法现状。
    回调里锁 LLM/IO,本函数保持纯(测试注入假回调)。
    """
    import numpy as np

    kept: list[Candidate] = []
    kept_vecs: list = []
    kept_vec_cands: list[Candidate] = []  # 与 kept_vecs 同序,供 same_event 复判取对象
    per_cat: dict[str, int] = {}
    per_src: dict[str, int] = {}
    old_count = 0
    for cand in sorted(ranked, key=lambda c: c.final_score, reverse=True):
        cat = cand.category or "_none"
        if per_cat.get(cat, 0) >= cfg.per_category_limit:
            continue
        if per_src.get(cand.source, 0) >= cfg.per_source_limit:
            continue
        is_old = _age_hours(cand.published_at, now) > cfg.old_item_age_hours
        if is_old and old_count >= cfg.old_item_limit:
            continue
        vec = vectors.get(cand.item_id) if vectors else None
        if vec is not None and kept_vecs:
            sims = np.stack(kept_vecs) @ np.asarray(vec)
            if float(np.max(sims)) >= cfg.select_sim_dedup:
                continue  # 与已选条目同一事件(高相似)→ 跳过(无向量的候选不抑制,放行)
            # 中等相似带:语义判官复判是否同一件事(注入回调;无则退回纯词法,不折叠)
            if same_event is not None:
                folded = False
                for j in range(len(sims)):
                    if cfg.event_dedup_min_sim <= float(sims[j]) < cfg.select_sim_dedup:
                        if same_event(cand, kept_vec_cands[j]):
                            folded = True
                            break
                if folded:
                    continue
        kept.append(cand)
        if vec is not None:
            kept_vecs.append(np.asarray(vec))
            kept_vec_cands.append(cand)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        per_src[cand.source] = per_src.get(cand.source, 0) + 1
        if is_old:
            old_count += 1
        if len(kept) >= cfg.final_limit:
            break
    return kept


def filter_candidates_by_domain(
    cands: list[Candidate], sources: dict, domain: str | None
) -> list[Candidate]:
    """只留源 domain==domain 的候选(纯函数)。domain=None 不过滤;源不在注册表=丢(领域未知)。

    召回/白名单/热点直通都是全局的,多领域同库时靠它把候选夹回本领域——
    geo 头条多源同报会经热点直通混进 AI 候选池(反之亦然),这里夹掉。
    """
    if domain is None:
        return list(cands)
    return [c for c in cands
            if (s := sources.get(c.source)) is not None and s.domain == domain]


def _build_candidate(item: Item, sim: float, src: Source | None, source_count: int) -> Candidate:
    facts = item.facts or {}
    return Candidate(
        item_id=item.item_id,
        title=item.title,
        source=item.source,
        category=item.category,
        cluster_id=item.cluster_id,
        published_at=item.published_at,
        source_count=source_count,
        source_weight=src.weight if src else 0.5,
        freshness_hours=src.freshness_hours if src else 24,
        enriched=facts.get("enriched", []),
        recall_sim=sim,
        whitelisted=bool(src and src.whitelisted),
    )


async def run_rank(
    settings: Settings,
    *,
    interest: str,
    tags: list[str] | None = None,
    limit: int | None = None,
    domain: str | None = None,
    embedder=None,
    sessionmaker=None,
    run_id: str | None = None,
) -> dict:
    """跑一次兴趣分类(召回→规则→精排→门→限额),落库 rankings 并返回汇总。

    domain 非 None 时只保留该领域(源 domain==domain)的候选——召回/白名单/热点直通都是
    全局的,多领域同库时靠它把候选夹回本领域,防 AI 头条漏进 bio/geo(反之亦然)。None=不过滤。
    """
    from pulsewire.config import load_sources
    from pulsewire.dedup import get_embedder
    from pulsewire.store.tables import Cluster

    from sqlalchemy import select

    cfg = settings.rank
    tags = tags or []
    key = interest_key(interest, tags)
    now = datetime.now(timezone.utc)

    # 1) 兴趣向量(兴趣文本 + 标签拼一起)
    embedder = embedder or get_embedder(settings)
    query_text = interest if not tags else f"{interest} {' '.join(tags)}"
    vector = embedder.embed([query_text])[0]

    sources = {s.id: s for s in load_sources()}
    sm = sessionmaker or get_sessionmaker()

    async with sm() as session:
        async with session.begin():
            # 2) 召回(近 recall_recency_hours 内或无日期的相似项 → 召回池本身就偏新鲜)
            recall_since = now - timedelta(hours=cfg.recall_recency_hours)
            recalled = await recall_by_vector(
                session, vector=vector, limit=cfg.recall_limit, since=recall_since
            )
            if not recalled:
                return {"interest_key": key, "recalled": 0, "kept": 0, "provider": cfg.rerank_provider}

            # 簇 source_count(大事信号)
            cluster_ids = {it.cluster_id for it, _ in recalled if it.cluster_id}
            counts: dict[str, int] = {}
            if cluster_ids:
                rows = (
                    await session.execute(
                        select(Cluster.cluster_id, Cluster.source_count).where(
                            Cluster.cluster_id.in_(cluster_ids)
                        )
                    )
                ).all()
                counts = {cid: sc for cid, sc in rows}

            cands = [
                _build_candidate(
                    it, sim, sources.get(it.source),
                    counts.get(it.cluster_id, 1) if it.cluster_id else 1,
                )
                for it, sim in recalled
            ]

            # 2.5) 白名单直通:把高价值源(实验室官方/大佬访谈)的近期条目补进候选池,
            #      即使它们没被语义召回排进前列;给未召回到的一个相似度地板,让其在精排里有竞争力。
            wl_added = 0
            # 白名单按 domain 取:原来全局取所有白名单源(都是 AI 高价值源),bio/geo rank 时这些
            # 条目后面会被领域夹回(2.7)全丢掉(whitelisted_kept=0),白占召回预算。这里就按 domain 过滤,
            # 让 bio/geo 只补本领域白名单源(暂无则 wl_added=0,不浪费);domain=None(custom 单兴趣)取全部。
            wl_sources = [s.id for s in sources.values()
                          if s.whitelisted and (domain is None or s.domain == domain)]
            if wl_sources and cfg.whitelist_recent_limit > 0:
                from pulsewire.store import get_recent_items_by_sources

                have = {c.item_id for c in cands}
                recalled_sim = {it.item_id: sim for it, sim in recalled}
                wl_items = await get_recent_items_by_sources(
                    session, sources=wl_sources, since=recall_since, limit=cfg.whitelist_recent_limit
                )
                for it in wl_items:
                    if it.item_id in have:
                        continue
                    sim = recalled_sim.get(it.item_id, cfg.whitelist_recall_floor)
                    cands.append(
                        _build_candidate(
                            it, sim, sources.get(it.source),
                            counts.get(it.cluster_id, 1) if it.cluster_id else 1,
                        )
                    )
                    have.add(it.item_id)
                    wl_added += 1

            # 2.6) 事件热度 + 热点直通:近窗全量向量算"多少个不同源在报相似内容"。
            #      头条对兴趣相似度天然免疫(新闻标题 vs 兴趣句 sim 仅 ~0.28,排千名外),
            #      多源同报才是它的信号——热点代表不走兴趣相似度,直接进候选池。
            hot_added = 0
            heat_map: dict[str, int] = {}
            sel_vecs: dict[str, object] = {}  # {item_id: 归一化向量},选稿同事件去重用
            if cfg.heat_top_reps > 0:
                import numpy as np

                from pulsewire.store import get_items_by_ids, get_recent_embeddings

                from .heat import compute_heat, pick_hot_reps

                heat_since = now - timedelta(hours=cfg.heat_window_hours)
                rows = await get_recent_embeddings(session, since=heat_since)
                if rows:
                    h_ids = [r[0] for r in rows]
                    h_srcs = [r[1] for r in rows]
                    h_vecs = [r[2] for r in rows]
                    heats = compute_heat(h_vecs, h_srcs, threshold=cfg.heat_sim_threshold)
                    heat_map = dict(zip(h_ids, heats))
                    nm = np.asarray(h_vecs, dtype=np.float32)
                    nrm = np.linalg.norm(nm, axis=1, keepdims=True)
                    nrm[nrm == 0] = 1.0
                    nm = nm / nrm
                    sel_vecs = {hid: nm[i] for i, hid in enumerate(h_ids)}
                    reps = pick_hot_reps(
                        h_ids, h_vecs, heats,
                        threshold=cfg.heat_sim_threshold,
                        min_sources=cfg.heat_min_sources,
                        top_n=cfg.heat_top_reps,
                    )
                    have = {c.item_id for c in cands}
                    new_ids = [rid for rid in reps if rid not in have]
                    if new_ids:
                        # 热点代表的召回分用"真实兴趣相似度"(不给地板,诚实;主编分+大事信号抬它)
                        qv = np.asarray(vector, dtype=np.float32)
                        qv = qv / (np.linalg.norm(qv) or 1.0)
                        sim_by_id: dict[str, float] = {}
                        for rid in new_ids:
                            v = np.asarray(h_vecs[h_ids.index(rid)], dtype=np.float32)
                            sim_by_id[rid] = float(qv @ (v / (np.linalg.norm(v) or 1.0)))
                        for it in await get_items_by_ids(session, new_ids):
                            cands.append(
                                _build_candidate(
                                    it, sim_by_id.get(it.item_id, 0.0), sources.get(it.source),
                                    counts.get(it.cluster_id, 1) if it.cluster_id else 1,
                                )
                            )
                            hot_added += 1
            # 全员挂热度(规则分的大事信号 + 主编提示词都要用)
            for c in cands:
                c.heat = heat_map.get(c.item_id, 1)

            # 2.7) 领域夹回:召回/白名单/热点都是全局的,多领域同库时只留本领域候选,
            #      防跨领域串味(geo 头条多源同报会经热点直通混进 AI 池,反之亦然)。
            domain_dropped = 0
            if domain is not None:
                before_dom = len(cands)
                cands = filter_candidates_by_domain(cands, sources, domain)
                domain_dropped = before_dom - len(cands)
                if not cands:
                    log.warning("rank.empty_domain", interest_key=key, domain=domain,
                                dropped=domain_dropped)
                    return {"interest_key": key, "domain": domain, "recalled": len(recalled),
                            "kept": 0, "domain_dropped": domain_dropped,
                            "provider": cfg.rerank_provider}

            # 3) 新鲜度门
            before = len(cands)  # 召回 + 白名单 + 热点直通合并后的候选数
            cands = [c for c in cands if passes_freshness(c, now)]
            dropped = before - len(cands)
            if not cands:
                log.warning("rank.all_stale", interest_key=key, dropped=dropped)
                return {"interest_key": key, "recalled": before, "kept": 0,
                        "dropped_stale": dropped, "provider": cfg.rerank_provider}

            # 3.5) 持续关注信号:cluster 已属于一条达门槛的多天 active 在追线 → 规则分加分。
            #      时序:rank 在 threads 站之前,今天的簇还没归线 → 这里命中的是往期已归线且仍存活的
            #      簇(持续发酵的故事);新簇要等当晚 threads 站归线后,从次日跑起生效。失败降级为不加分。
            try:
                from pulsewire.store import get_active_thread_cluster_map, get_threads_for_display
                disp = await get_threads_for_display(
                    session, min_days=settings.threads.min_days, tz_name=settings.app.timezone,
                )
                ok_tids = {t["thread_id"] for t in disp}
                cmap = await get_active_thread_cluster_map(session)
                tracked_cids = {cid for cid, tid in cmap.items() if tid in ok_tids}
                for c in cands:
                    c.tracking = bool(c.cluster_id and c.cluster_id in tracked_cids)
            except Exception as exc:  # noqa: BLE001 — 增强信号,失败不拖垮精排
                log.warning("rank.tracking.failed", error=str(exc), error_type=type(exc).__name__)

            # 4) 规则粗排
            for c in cands:
                c.rule_score = rule_score(c, cfg, now, settings.event.min_sources)

            # 5) LLM 精排(deepseek)或纯规则(rule)
            provider = cfg.rerank_provider
            if provider == "deepseek":
                rel = llm_rerank(interest, tags, cands, settings)  # {item_id: 相关度0-1}
                for c in cands:
                    c.rerank_score = rel.get(c.item_id, 0.0)
                    c.final_score = cfg.rerank_blend * c.rerank_score + (1 - cfg.rerank_blend) * c.rule_score
            else:
                for c in cands:
                    c.final_score = c.rule_score

            # 6) 限额 + 同事件去重(头条被 N 源重复报道,日报里只出一条)
            # 去重向量须覆盖整个候选集:sel_vecs 只来自 36h 热度窗,召回窗(720h)内、热度窗外的
            # 候选无向量会绕过去重直接放行 → 同事件重复刷屏(2026-06-15 二①)。补齐缺失候选的向量。
            dedup_vecs: dict[str, object] = dict(sel_vecs)
            missing = [c.item_id for c in cands if c.item_id not in dedup_vecs]
            if missing:
                import numpy as np

                from pulsewire.store import get_embeddings_by_ids

                raw = await get_embeddings_by_ids(session, missing)
                for iid, v in raw.items():
                    arr = np.asarray(v, dtype=np.float32)
                    nrm = float(np.linalg.norm(arr))
                    dedup_vecs[iid] = arr / nrm if nrm else arr  # 归一化,与 sel_vecs 同口径(余弦)
            eff_limit = limit if limit is not None else cfg.final_limit
            cfg_eff = cfg.model_copy(update={"final_limit": eff_limit})
            # 方案 B:中等相似带语义同事件复判(判官保守、失败不折叠;成本闸/缓存在工厂里)
            same_event = None
            if cfg.event_dedup_judge:
                from .event_judge import make_same_event_judge

                same_event = make_same_event_judge(settings)
            kept = apply_quotas(cands, cfg_eff, now, vectors=dedup_vecs, same_event=same_event)

            # 6.5) 内容领域分类(三③):按内容判,确信不属本域的剔掉(纠正"领域跟着源走"错放)。
            #      只判已选中条目(省钱);丢弃超 classify_max_drop_ratio=疑分类器抽风→全留;LLM 失败→全留。
            classify_dropped = 0
            if cfg.content_classify and domain is not None and kept:
                catalog = [(d.key, d.interest) for d in settings.run.domains]
                if domain in {k for k, _ in catalog} and len(catalog) > 1:
                    try:
                        from .classify import classify_item_domains, resolve_drops

                        verdict = classify_item_domains(kept, catalog, settings)
                        drop_ids, over = resolve_drops(
                            [c.item_id for c in kept], verdict, domain, cfg.classify_max_drop_ratio)
                        if over:
                            log.warning("rank.classify.over_drop", interest_key=key, domain=domain,
                                        of=len(kept), note="疑分类器抽风,全留")
                        elif drop_ids:
                            kept = [c for c in kept if c.item_id not in drop_ids]
                            classify_dropped = len(drop_ids)
                            log.info("rank.classify.dropped", interest_key=key, domain=domain,
                                     dropped=classify_dropped, kept=len(kept))
                    except Exception as exc:  # noqa: BLE001 — 分类是增强,失败保留全部不拖垮日报
                        log.warning("rank.classify.failed", interest_key=key,
                                    error=str(exc), error_type=type(exc).__name__)

            # 落库:先清掉本兴趣被挤出 top-N 的陈旧行(重跑幂等,不留旧后端/旧名次)
            await prune_rankings(session, interest_key=key, keep_item_ids=[c.item_id for c in kept])
            for rank, c in enumerate(kept, start=1):
                await upsert_ranking(
                    session,
                    interest_key=key, interest=interest, tags=tags,
                    item_id=c.item_id, cluster_id=c.cluster_id,
                    recall_score=c.recall_sim, rule_score=c.rule_score,
                    rerank_score=c.rerank_score, final_score=c.final_score,
                    rank=rank, provider=provider, run_id=run_id,
                )

    return {
        "interest_key": key,
        "domain": domain,
        "recalled": len(recalled),
        "whitelisted_added": wl_added,
        "hot_added": hot_added,
        "domain_dropped": domain_dropped,
        "candidates": before,
        "after_freshness": len(cands),
        "dropped_stale": dropped,
        "classify_dropped": classify_dropped,
        "kept": len(kept),
        "whitelisted_kept": sum(1 for c in kept if c.whitelisted),
        "provider": provider,
        "top": [
            {"rank": i, "title": c.title, "source": c.source, "heat": c.heat,
             "final": round(c.final_score, 3), "recall": round(c.recall_sim, 3),
             "enriched": c.enriched}
            for i, c in enumerate(kept, start=1)
        ],
    }
