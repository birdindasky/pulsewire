"""事件线归线引擎(step 3):把今日入选簇归到跨天事件线。

每簇:A 抽主体 → 查在追线里同主体的候选 → 候选非空则 B 判官定接哪条/新开 → 写 thread_clusters + 更新线。
判定可重放(thread_clusters 留 link_reason/subject/confidence,见 --rebuild step 4)。

容错:单簇 A/B 失败只降级或跳过该簇,不中断;整站失败由 pipeline 的 _stage_threads 吞掉,不拖垮日报。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.obs.alert import alert_failure
from pulsewire.store import (
    create_thread,
    get_active_threads,
    get_items_by_ids,
    get_sessionmaker,
    get_summaries,
    link_cluster_to_thread,
    linked_cluster_ids,
    mark_dormant_threads,
    touch_thread,
)
from pulsewire.store.repo import _progress_date
from pulsewire.threads.judge import judge_line
from pulsewire.threads.recall import gather_candidates
from pulsewire.threads.subject import extract_subject, match_subject

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pulsewire.config import Settings

log = get_logger()


async def thread_domain(
    session: AsyncSession, settings: Settings, *, interest_key: str, run_id: str | None, now: datetime
) -> dict:
    """对一个领域今日入选簇归线(在调用方事务内)。返回 {new, linked, skipped, dormant}。"""
    cfg = settings.threads
    subj_memo: dict[str, object] = {}  # 主体短语 → embedding(语义召回用,本域内只算一次)
    summaries = await get_summaries(session, interest_key=interest_key)
    # 按 cluster 去重:一簇只归一次(同一簇可能有多条 item summary),用首条代表;ghboard cluster_id 空,跳过
    by_cluster: dict[str, object] = {}
    for s in summaries:
        if s.cluster_id and s.cluster_id not in by_cluster:
            by_cluster[s.cluster_id] = s
    cands = list(by_cluster.values())

    # 落痕用:进展日期(run 当天)+ 各代表簇原文 url/source(挂线时冻结,免日后 summaries 删了取不到)
    tz = ZoneInfo(settings.app.timezone)
    prog_date = _progress_date(run_id, now, tz)
    items = {it.item_id: it for it in await get_items_by_ids(session, [s.item_id for s in cands])}

    already = await linked_cluster_ids(session, [s.cluster_id for s in cands]) if cands else set()
    new = linked = skipped = subj_failed = 0
    for s in cands:
        if s.cluster_id in already:  # 幂等:重跑/已挂的簇不重复处理
            skipped += 1
            continue
        it = items.get(s.item_id)
        prov = dict(headline=s.headline, url=(it.url if it else None),
                    source=(it.source if it else None), progress_date=prog_date)
        # A:抽事件主体
        try:
            subject = extract_subject(
                s.headline, summary=s.tldr_rendered, domain=interest_key, settings=settings
            )
        except PermanentLLMError:
            raise  # 没钱/凭证失效:熔断整跑(在线 threads 是日报阶段,续跑时可能首个撞永久错)
        except Exception as exc:
            subj_failed += 1  # 计数:抽主体失败率高=「在追」静默退化,run_threads 据此告警(2026-06-15 二③)
            log.warning("threads.subject.failed", cluster=s.cluster_id, error=str(exc))
            continue  # 抽不出主体就不归线,下次再来
        # A:缩候选(在追线里同主体的:词法 ∪ 语义召回)
        active = await get_active_threads(session, domain=interest_key)
        close = gather_candidates(subject, active, settings, subj_memo)
        chosen, reason, conf = None, "new", 1.0
        if close:
            # B:判官定接哪条/新开;失败降级为只信 A(挂最匹配候选)
            try:
                idx, conf = judge_line(
                    headline=s.headline, tldr=s.tldr_rendered, subject=subject,
                    candidates=[(t.name, t.summary) for t in close], settings=settings,
                )
                if idx is not None:
                    chosen, reason = close[idx], "judge"
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,绝不降级放行
            except Exception as exc:
                log.warning("threads.judge.failed", cluster=s.cluster_id, error=str(exc))
                best = match_subject(subject, [t.subject for t in close], cfg.match_threshold)
                chosen = next((t for t in close if t.subject == best), None) if best else None
                reason, conf = "subject", 1.0
        if chosen is not None:
            await link_cluster_to_thread(
                session, thread_id=chosen.thread_id, cluster_id=s.cluster_id, run_id=run_id,
                subject=subject, link_reason=reason, confidence=conf, **prov,
            )
            await touch_thread(
                session, thread_id=chosen.thread_id, seen_at=now,
                summary=s.tldr_rendered, name=s.headline, heat_delta=1,  # 线名随最新簇刷新(二②)
            )
            linked += 1
        else:
            tid = f"thr_{uuid.uuid4().hex[:16]}"
            await create_thread(
                session, thread_id=tid, name=s.headline, subject=subject, domain=interest_key,
                summary=s.tldr_rendered, seen_at=now, heat=1,
            )
            await link_cluster_to_thread(
                session, thread_id=tid, cluster_id=s.cluster_id, run_id=run_id,
                subject=subject, link_reason="new", confidence=conf, **prov,
            )
            new += 1
        already.add(s.cluster_id)

    before = now - timedelta(days=cfg.dormant_after_days)
    dormant = await mark_dormant_threads(session, domain=interest_key, before=before)
    return {"new": new, "linked": linked, "skipped": skipped, "dormant": dormant,
            "subj_failed": subj_failed}


async def run_threads(
    settings: Settings, *, interest_keys: list[str], run_id: str | None = None, sessionmaker=None
) -> dict:
    """对各领域归线(每领域一个事务)。pipeline 的 _stage_threads 调它。"""
    sm = sessionmaker or get_sessionmaker()
    now = datetime.now(timezone.utc)
    agg = {"new": 0, "linked": 0, "skipped": 0, "dormant": 0, "subj_failed": 0}
    for ik in interest_keys:
        async with sm() as session:
            async with session.begin():
                r = await thread_domain(session, settings, interest_key=ik, run_id=run_id, now=now)
        for k in agg:
            agg[k] += r[k]
    log.info("threads.done", **agg, domains=len(interest_keys))
    # 抽主体失败可见性:失败率高 = 「在追」静默退化(2026-06-15 二③),发告警别让它无声无息
    attempted = agg["new"] + agg["linked"] + agg["subj_failed"]
    ratio = settings.threads.subject_fail_alert_ratio
    if attempted and agg["subj_failed"] / attempted >= ratio:
        await alert_failure(
            settings, run_id=run_id or "—", stage="threads:subject_extraction",
            error=(f"抽主体失败 {agg['subj_failed']}/{attempted}(≥{ratio:.0%}),"
                   f"「在追」可能漏更新;多半 flash 抽风,白天健康时可 `pulsewire threads --rebuild` 重刷"),
            error_type="HighSubjectFailureRate",
        )
    return agg
