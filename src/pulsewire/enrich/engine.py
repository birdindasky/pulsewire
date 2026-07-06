"""富化引擎:把入库事实规整成"带 source_id 的结构化事实",可选抓正文全文。

铁律(数字回源):每个数字都挂 `source_id = item_id:fact_type:field`(来自 `store.ids`),
**绝不让模型编造数字或来源**。阶段 5 的 verify 据此对账;模型给不出来源的数字一律不展示。

- HN:`items.facts.hn`(阶段 2 抓取时已存 points / num_comments)→ 规整成带 source_id 的事实。
- GitHub:`items.facts.github`(stars / forks)→ 同。
- 正文:trafilatura 抓全文(可选,走网络;默认关)。挂全文文本 + 字数(带 source_id)。
- 按源全文(2026-07 P1):sources.yaml `enrich: ["fulltext"]` 的源,对近期"瘦正文"条目
  回源抓全文写 `facts.fulltext`(有界成本:近期窗 + 每 run 硬顶 + 已有全文不重抓)。
  选稿侧(events)读 facts.fulltext 兜底瘦 content——治 summary-only 源过不了空壳护栏。
富化结果写回 `items.facts.enriched`(list)与 `items.facts.fulltext`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.store import (
    get_fulltext_candidates,
    get_items,
    get_sessionmaker,
    update_item_facts,
)
from pulsewire.store.ids import make_source_id

if TYPE_CHECKING:
    from pulsewire.config import Settings, Source

log = get_logger()


def extract_facts(item_id: str, facts: dict | None) -> list[dict]:
    """从入库事实派生带 source_id 的结构化富化事实(纯函数,可无库单测)。

    返回 list[{kind, label, value, unit, source_id}]。只规整**已入库**的数字,
    source_id 指回该数字在 `facts` 里的位置 → 可回源对账。
    """
    facts = facts or {}
    out: list[dict] = []

    hn = facts.get("hn") or {}
    if hn.get("points") is not None:
        out.append({
            "kind": "hn.points", "label": "HN points", "value": hn["points"],
            "unit": "points", "source_id": make_source_id(item_id, "hn", "points"),
        })
    if hn.get("num_comments") is not None:
        out.append({
            "kind": "hn.comments", "label": "HN 评论数", "value": hn["num_comments"],
            "unit": "comments", "source_id": make_source_id(item_id, "hn", "num_comments"),
        })

    gh = facts.get("github") or {}
    if gh.get("stars") is not None:
        out.append({
            "kind": "github.stars", "label": "GitHub stars", "value": gh["stars"],
            "unit": "stars", "source_id": make_source_id(item_id, "github", "stars"),
        })
    if gh.get("forks") is not None:
        out.append({
            "kind": "github.forks", "label": "GitHub forks", "value": gh["forks"],
            "unit": "forks", "source_id": make_source_id(item_id, "github", "forks"),
        })

    return out


async def fetch_fulltext(item, settings: Settings, *, client=None) -> dict | None:
    """用 trafilatura 抓正文全文(best-effort,失败返回 None 不拖垮整批)。

    供 enrich(全量可选 + 按源批量)与 transcript(精排后入选条目)复用。
    client:批量调用传入共享 FetchClient(省 300 次建连);缺省自建自关(向后兼容)。
    返回 {text, chars, source_id} 或 None。
    """
    import trafilatura  # 延迟导入:重依赖,开了 fulltext 才加载

    from pulsewire.fetch.client import FetchClient

    async def _get(c):
        resp = await c.get(item.url, use_conditional=False)
        if resp.not_modified or not resp.text:
            return None
        return resp.text

    try:
        if client is not None:
            html_text = await _get(client)
        else:
            async with FetchClient(settings) as own:
                html_text = await _get(own)
        if html_text is None:
            return None
        text = trafilatura.extract(html_text, include_comments=False, include_tables=False)
    except Exception as exc:  # 单条失败冒泡到日志,不静默吞、不拖垮整批
        log.warning("enrich.fulltext.failed", item=item.item_id, url=item.url, error=str(exc))
        return None
    if not text:
        return None
    text = text[: settings.enrich.fulltext_max_chars]
    return {
        "text": text,
        "chars": len(text),
        "source_id": make_source_id(item.item_id, "fulltext", "text"),
    }


@dataclass(slots=True)
class _FtCandidate:
    """全文候选的纯数据快照(不带 ORM 会话,抓取阶段安全跨事务用)。"""

    item_id: str
    url: str
    facts: dict | None


async def _fulltext_for_flagged_sources(settings: Settings, sm, sources: list[Source]) -> dict:
    """按源全文富化(2026-07 P1):enrich 含 "fulltext" 的启用源,近期瘦正文条目回源抓全文。

    有界成本:fetched_at 近期窗 + 每 run 硬顶(计**尝试**数)+ 只抓 content 短于阈值 +
    已有 facts.fulltext 不重抓。单条失败 log+skip(fetch_fulltext 内部兜),绝不 fail 整站。
    """
    cfg = settings.enrich
    flagged = [s.id for s in sources if s.enabled and "fulltext" in (s.enrich or [])]
    summary = {"fulltext_sources": len(flagged), "fulltext_candidates": 0, "fulltext_ok": 0}
    if not flagged:
        return summary

    since = datetime.now(timezone.utc) - timedelta(hours=cfg.fulltext_recency_hours)
    async with sm() as session:
        rows = await get_fulltext_candidates(
            session,
            source_ids=flagged,
            fetched_since=since,
            max_content_chars=cfg.fulltext_min_content_chars,
            limit=cfg.fulltext_max_per_run,
        )
        cands = [_FtCandidate(item_id=r.item_id, url=r.url, facts=dict(r.facts or {})) for r in rows]
    summary["fulltext_candidates"] = len(cands)
    if not cands:
        return summary

    from pulsewire.fetch.client import FetchClient

    sem = asyncio.Semaphore(cfg.fulltext_concurrency)
    fetched: dict[str, dict] = {}

    async def _one(c: _FtCandidate, client) -> None:
        async with sem:
            ft = await fetch_fulltext(c, settings, client=client)
        if ft is not None:
            fetched[c.item_id] = ft

    async with FetchClient(settings) as client:
        await asyncio.gather(*(_one(c, client) for c in cands))

    if fetched:
        async with sm() as session:
            async with session.begin():
                for c in cands:
                    ft = fetched.get(c.item_id)
                    if ft is None:
                        continue
                    new_facts = dict(c.facts)
                    new_facts["fulltext"] = ft
                    await update_item_facts(session, item_id=c.item_id, facts=new_facts)
    summary["fulltext_ok"] = len(fetched)
    log.info("enrich.fulltext.flagged", **summary)
    return summary


async def run_enrich(
    settings: Settings,
    *,
    since: datetime | None = None,
    fulltext: bool | None = None,
    limit: int | None = None,
    sessionmaker=None,
    sources: list[Source] | None = None,
) -> dict:
    """对条目做富化(挂 value+source_id),写回 facts。返回汇总 dict。

    sources:按源全文富化的源表(测试注入用);缺省读 sources.yaml。
    """
    do_fulltext = settings.enrich.fulltext if fulltext is None else fulltext
    sm = sessionmaker or get_sessionmaker()

    enriched_items = enriched_facts = fulltext_ok = 0
    async with sm() as session:
        async with session.begin():
            items = await get_items(session, since=since, limit=limit)
            for it in items:
                derived = extract_facts(it.item_id, it.facts)
                # 起底当前 facts(不丢原始 hn/github),叠加派生事实
                new_facts = dict(it.facts or {})
                if derived:
                    new_facts["enriched"] = derived
                    enriched_facts += len(derived)
                if do_fulltext:
                    ft = await fetch_fulltext(it, settings)
                    if ft is not None:
                        new_facts["fulltext"] = ft
                        fulltext_ok += 1
                if new_facts != (it.facts or {}):
                    await update_item_facts(session, item_id=it.item_id, facts=new_facts)
                    enriched_items += 1

    # 按源全文富化(全局开关关着也跑;全局开着就不必重复——全量路径已覆盖)
    flagged_summary: dict = {"fulltext_sources": 0, "fulltext_candidates": 0, "fulltext_ok": 0}
    if not do_fulltext:
        if sources is None:
            from pulsewire.config import load_sources

            sources = load_sources()
        flagged_summary = await _fulltext_for_flagged_sources(settings, sm, sources)

    return {
        "items_seen": len(items),
        "items_enriched": enriched_items,
        "facts_attached": enriched_facts,
        "fulltext_fetched": fulltext_ok,
        "fulltext_enabled": do_fulltext,
        **flagged_summary,
    }
