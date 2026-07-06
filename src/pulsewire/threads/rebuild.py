"""事件线 step 4:从归档日报重放历史,重建跨天事件线(`pulsewire threads --rebuild`)。

为什么不从数据库重放:summaries 每跑被删(只留本轮),库里只剩最新一天,变不出跨天线。
但 `web/archive/daily/*.json` 留了每天渲染好的日报(headline/tldr/url/source 俱全)——这才是耐久史料。

做法(见 docs/DESIGN.md §4):
1. 清空 threads/thread_clusters(线是派生数据,可反复重建)。
2. 按日期升序读归档日报;每条新闻当一个"簇"(合成 cluster_id),领域统一映射到 ai/bio/geo
   的 interest_key(与组织性日跑同命名空间,保证 06-15 起日跑能续接到重建的历史线上)。github 不归线。
3. 每天:A 抽主体(并行)→ 顺序 B 判官归线 → 把当天 headline/url/source/date 落痕进 thread_clusters。
4. dormant 扫描以"真实今天"为基准:超 N 天没新进展的线折叠(只剩近 N 天活跃的进「在追」)。

容错:单条 A/B 失败只跳过该条;整体失败冒泡。LLM 用 flash 省档。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pulsewire.config import PROJECT_ROOT
from pulsewire.obs import get_logger
from pulsewire.rank import interest_key as make_interest_key
from pulsewire.store import (
    clear_threads,
    create_cluster,
    create_thread,
    get_active_threads,
    get_sessionmaker,
    link_cluster_to_thread,
    mark_dormant_threads,
    touch_thread,
)
from pulsewire.threads.judge import judge_line
from pulsewire.threads.recall import gather_candidates
from pulsewire.threads.subject import extract_subject, match_subject

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _archive_domain_to_short(key: str, label: str) -> str | None:
    """归档领域(旧口径 tr0-3 或新 pulsewire ai/bio/geo)→ 统一短键;github/未知 → None(不归线)。"""
    s = f"{key} {label}".lower()
    if "github" in s or "开源" in s:
        return None  # 与组织性日跑一致:github 不归线
    if "生物" in s or "医疗" in s or "bio" in s:
        return "bio"
    if "国际" in s or "地缘" in s or "geo" in s:
        return "geo"
    if "ai" in s or "智能" in s or "大模型" in s or "模型" in s:
        return "ai"
    return None


def _load_archive_days(archive_dir: Path, days: int | None) -> list[tuple[str, dict]]:
    """读 daily/*.json,返回 [(date_str, data)] 按日期升序;days 指定则只取最近 N 天。"""
    out: list[tuple[str, dict]] = []
    for p in sorted((archive_dir / "daily").glob("*.json")):
        if not _DATE_RE.match(p.stem):
            continue
        try:
            out.append((p.stem, json.loads(p.read_text(encoding="utf-8"))))
        except Exception as exc:  # noqa: BLE001 — 单天坏档跳过,不拖垮重建
            log.warning("threads.rebuild.bad_archive", file=p.name, error=str(exc))
    out.sort(key=lambda x: x[0])
    if days and days > 0:
        out = out[-days:]
    return out


def _records_for_day(data: dict, short_to_ik: dict[str, str]) -> list[dict]:
    """把一天归档日报摊平成可归线记录:[{ik, headline, tldr, url, source, cluster_id}]。"""
    recs: list[dict] = []
    for dom in data.get("domains") or []:
        short = _archive_domain_to_short(dom.get("key", ""), dom.get("label", ""))
        ik = short_to_ik.get(short) if short else None
        if not ik:
            continue
        for it in dom.get("items") or []:
            headline = (it.get("headline") or "").strip()
            if not headline:
                continue
            recs.append({
                "ik": ik, "headline": headline, "tldr": (it.get("tldr") or "").strip(),
                "url": it.get("url"), "source": it.get("source"),
            })
    return recs


async def _extract_subjects(recs: list[dict], settings: Settings, *, concurrency: int = 8) -> list[str | None]:
    """并行抽主体(A 层,blocking LLM 丢线程池);单条失败 → None(跳过该条,不拖垮当天)。"""
    sem = asyncio.Semaphore(concurrency)

    async def one(r: dict) -> str | None:
        async with sem:
            try:
                return await asyncio.to_thread(
                    extract_subject, r["headline"], summary=r["tldr"] or None,
                    domain=r["ik"], settings=settings,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("threads.rebuild.subject_failed", headline=r["headline"][:48], error=str(exc))
                return None

    return await asyncio.gather(*[one(r) for r in recs])


async def _preflight_flash_health(
    archive_days: list, short_to_ik: dict, settings: Settings,
    *, sample: int = 6, max_fail_ratio: float = 0.5,
) -> None:
    """清表前 flash 健康预检:抽 sample 条真实主体,失败率≥max_fail_ratio → 抛(别清了又填不好)。

    2026-06-15 二⑦ 教训:两次重建都栽在 flash 抽风(返空/坏 JSON)——clear_threads 先独立提交、
    replay 大批失败 → 留下残缺种子。预检在动旧数据(清表)之前拦下,旧事件线分毫不动,flash 恢复后再跑。
    """
    recs: list[dict] = []
    for _date, data in archive_days:
        recs.extend(_records_for_day(data, short_to_ik))
        if len(recs) >= sample:
            break
    recs = recs[:sample]
    if not recs:
        return
    subjects = await _extract_subjects(recs, settings, concurrency=min(3, len(recs)))
    failed = sum(1 for s in subjects if not s)
    if failed / len(recs) >= max_fail_ratio:
        raise RuntimeError(
            f"flash 健康预检失败:抽样 {failed}/{len(recs)} 条主体抽取失败(疑似 flash 抽风);"
            f"已中止重建——未清表,旧事件线分毫未动。flash 恢复后再跑 `pulsewire threads --rebuild`。"
        )
    log.info("threads.rebuild.preflight_ok", sample=len(recs), failed=failed)


async def rebuild_from_archive(
    settings: Settings, *, archive_dir: Path | None = None, days: int | None = None, sessionmaker=None,
) -> dict:
    """从归档重放重建事件线。返回 {days, items, new, linked, skipped, dormant, threads}。"""
    archive_dir = archive_dir or (PROJECT_ROOT / "web" / "archive")
    cfg = settings.threads
    tz = ZoneInfo(settings.app.timezone)
    short_to_ik = {d.key: make_interest_key(d.interest, list(d.tags)) for d in settings.run.domains}

    archive_days = _load_archive_days(archive_dir, days)
    if not archive_days:
        raise RuntimeError(f"归档无可重放日报({archive_dir / 'daily'} 下无 *.json)")

    sm = sessionmaker or get_sessionmaker()
    agg = {"days": 0, "items": 0, "new": 0, "linked": 0, "skipped": 0, "dormant": 0}
    subj_memo: dict[str, object] = {}  # 主体短语 → embedding(语义召回用,跨天复用,每个主体只算一次)

    # flash 健康预检(2026-06-15 二⑦):清表前探一探,抽风就在动旧数据之前中止,绝不"清了又填不好"
    await _preflight_flash_health(archive_days, short_to_ik, settings)

    # 一次性清空(独立事务),再逐天重放(每天一事务,失败不全丢)
    async with sm() as session:
        async with session.begin():
            nlinks, nthreads = await clear_threads(session)
    log.info("threads.rebuild.cleared", links=nlinks, threads=nthreads)

    for date_str, data in archive_days:
        recs = _records_for_day(data, short_to_ik)
        if not recs:
            continue
        subjects = await _extract_subjects(recs, settings, concurrency=cfg.rebuild_concurrency)
        seen_at = datetime.fromisoformat(date_str).replace(hour=12, tzinfo=tz).astimezone(timezone.utc)
        day_new = day_linked = day_skipped = 0

        async with sm() as session:
            async with session.begin():
                seen_cids: set[str] = set()
                for r, subject in zip(recs, subjects):
                    if not subject:
                        continue
                    # 合成簇:同一天同 url/headline 去重(确定性 id,可重复重建)
                    seed = (r["url"] or r["headline"]).encode("utf-8")
                    cid = f"arch_{date_str}_{hashlib.sha1(seed).hexdigest()[:16]}"
                    if cid in seen_cids:
                        day_skipped += 1
                        continue
                    seen_cids.add(cid)
                    await create_cluster(
                        session, cluster_id=cid, first_item_id=f"archit_{uuid.uuid4().hex[:16]}",
                        title=r["headline"], seen_at=seen_at,
                    )
                    active = await get_active_threads(session, domain=r["ik"])
                    close = gather_candidates(subject, active, settings, subj_memo)
                    chosen, reason, conf = None, "new", 1.0
                    if close:
                        try:
                            idx, conf = judge_line(
                                headline=r["headline"], tldr=r["tldr"], subject=subject,
                                candidates=[(t.name, t.summary) for t in close], settings=settings,
                            )
                            if idx is not None:
                                chosen, reason = close[idx], "judge"
                        except Exception as exc:  # noqa: BLE001 — 降级只信 A
                            log.warning("threads.rebuild.judge_failed", cluster=cid, error=str(exc))
                            best = match_subject(subject, [t.subject for t in close], cfg.match_threshold)
                            chosen = next((t for t in close if t.subject == best), None) if best else None
                            reason, conf = "subject", 1.0
                    prov = dict(headline=r["headline"], url=r["url"], source=r["source"],
                                progress_date=date_str)
                    if chosen is not None:
                        await link_cluster_to_thread(
                            session, thread_id=chosen.thread_id, cluster_id=cid, run_id=None,
                            subject=subject, link_reason=reason, confidence=conf, **prov,
                        )
                        await touch_thread(
                            session, thread_id=chosen.thread_id, seen_at=seen_at,
                            summary=r["tldr"] or r["headline"], name=r["headline"], heat_delta=1,  # 线名随最新簇刷新(二②)
                        )
                        day_linked += 1
                    else:
                        tid = f"thr_{uuid.uuid4().hex[:16]}"
                        await create_thread(
                            session, thread_id=tid, name=r["headline"], subject=subject, domain=r["ik"],
                            summary=r["tldr"] or r["headline"], seen_at=seen_at, heat=1,
                        )
                        await link_cluster_to_thread(
                            session, thread_id=tid, cluster_id=cid, run_id=None,
                            subject=subject, link_reason="new", confidence=conf, **prov,
                        )
                        day_new += 1

        agg["days"] += 1
        agg["items"] += len(recs)
        agg["new"] += day_new
        agg["linked"] += day_linked
        agg["skipped"] += day_skipped
        log.info("threads.rebuild.day", date=date_str, items=len(recs),
                 new=day_new, linked=day_linked, skipped=day_skipped)

    # dormant 扫描以真实今天为基准:超 dormant_after_days 没新进展的线折叠
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    before = now - timedelta(days=cfg.dormant_after_days)
    async with sm() as session:
        async with session.begin():
            for ik in short_to_ik.values():
                agg["dormant"] += await mark_dormant_threads(session, domain=ik, before=before)

    log.info("threads.rebuild.done", **agg)
    return agg
