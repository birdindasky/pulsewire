"""交付编排:把对账后日报推到启用的各渠道,投递幂等挡重复推。

幂等键 = `cluster_id(=interest_key:date) + channel + trigger_type`(阶段1 已建唯一键):
- 同一天同渠道重跑 → has_delivery 命中 → skipped(不重复推)。
- 单渠道失败/跳过 → 记录原因、不拖垮其它渠道、不假装发成功(失败要冒泡)。
- 发成功才 record_delivery(status=sent);失败不记 → 下次可重试。
- **例外:webapp 豁免幂等**(见 `_ALWAYS_REWRITE`)。它只是本地文件,重写零副作用、
  不打扰任何人,该当天重跑就刷新;只有会推送打扰人的飞书/微信才遵守"一天一推"。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from zoneinfo import ZoneInfo

from pulsewire.config import PROJECT_ROOT, source_label
from pulsewire.obs import get_logger
from pulsewire.obs.sentinel import write_receipt
from pulsewire.store import (
    get_active_thread_cluster_map,
    get_digest,
    get_items_by_ids,
    get_rankings,
    get_sessionmaker,
    get_summaries,
    get_threads_for_display,
    has_delivery,
    record_delivery,
)

from . import feishu, feishu_app, wechat, webapp
from .base import DeliverPayload

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


class DomainSpec(TypedDict):
    """webapp 聚合用的领域描述(pipeline 传入)。三键必填,缺则 _build_payload 提前报清晰错误。"""

    key: str          # 领域键(ai/bio/geo);App 下拉标识
    label: str        # App 显示名
    interest_key: str  # 该领域 rankings/summaries 的 interest_key


def _require_domain_keys(domains: list[dict]) -> None:
    """校验每个 domain dict 含 key/label/interest_key,缺键即抛清晰错误(取代裸 KeyError)。"""
    for i, d in enumerate(domains):
        missing = [k for k in ("key", "label", "interest_key") if k not in d]
        if missing:
            raise ValueError(f"domain[{i}] 缺字段 {missing}(需 key/label/interest_key);实有={sorted(d)}")

_TRIGGER = "daily"  # 投递幂等键里 trigger_type 的默认值(run_deliver 未显式传入时用)
_SENDERS = {"feishu": feishu.send, "wechat": wechat.send, "webapp": webapp.send}
# 豁免"一天一推"幂等的渠道:本地文件,重写不打扰任何人,当天重跑应刷新。
_ALWAYS_REWRITE = {"webapp"}
_STALE_IMAGE_HOURS = 12.0  # 推图新鲜窗:mtime 超此=上次残留旧图,不推(f04 通道三)


def _logical_date(run_id: str | None, tz: ZoneInfo) -> str:
    """从 run_id(<trigger>_<YYYYMMDD>)取该 run 的逻辑日期 YYYY-MM-DD。

    治 f05:交付日期/收据/幂等键须锚到 run 的逻辑日,而非"交付那一刻的钟点"——否则晚间
    补课跨过午夜时 date_str 变成"明天",顶掉次日真日报的幂等槽 + 写错收据。
    解析不出(自定义/无 run_id)→ 退回当下本地日期(back-compat)。
    """
    if run_id:
        tail = run_id.rsplit("_", 1)[-1]
        if len(tail) == 8 and tail.isdigit():
            return f"{tail[:4]}-{tail[4:6]}-{tail[6:8]}"
    return datetime.now(tz).strftime("%Y-%m-%d")


def _fresh_image(path: Path) -> bool:
    """图是否本次 run 刚渲(mtime 在新鲜窗内)。治 f04 通道三:某域 render 失败时别把昨天
    的旧图当今天推。用"距今时长"而非"日期相等",对跨午夜交付稳健(render 距 deliver 只隔几分钟)。"""
    try:
        return (time.time() - path.stat().st_mtime) < _STALE_IMAGE_HOURS * 3600
    except OSError:
        return False


async def _build_items(
    session, interest_key: str, tracked: dict[str, dict] | None = None,
    run_id: str | None = None,
) -> tuple[list[dict], str]:
    """读某领域的精排+总结,拼成 webapp 条目列表 + 概述(只用对账后 *_rendered)。

    run_id 非空 → 只用本次 run 的稿+概述(治 f04:某域今日 summarize 失败时,别拿上次
    残留旧稿冒充今天交付)。tracked:{cluster_id: {thread_id, days, name}}——命中的卡片加
    「持续关注」徽标(达露出门槛的多天在追线)。缺省/未命中=不加,纯增强。
    """
    rankings = await get_rankings(session, interest_key=interest_key)
    summaries = {s.item_id: s for s in await get_summaries(session, interest_key=interest_key, run_id=run_id)}
    digest_row = await get_digest(session, interest_key=interest_key, run_id=run_id)
    items_meta = {it.item_id: it for it in await get_items_by_ids(session, [r.item_id for r in rankings])}

    items: list[dict] = []
    for r in rankings:
        s, meta = summaries.get(r.item_id), items_meta.get(r.item_id)
        if s is None or meta is None:
            continue
        item = {
            "id": meta.item_id, "headline": s.headline,
            "tldr": s.tldr_rendered, "insight": s.insight_rendered,
            "source": source_label(meta.source),  # 机器 slug → 人类可读名(2026-06-15 一⑤)
            "url": meta.url, "needs_review": s.status != "ok",
            "category": meta.category or "general",
        }
        tk = tracked.get(r.cluster_id) if tracked and r.cluster_id else None
        if tk:  # 持续关注徽标:这条属于一条多天在追线
            item["tracking_days"] = tk["days"]
            item["thread_id"] = tk["thread_id"]
        items.append(item)
    return items, (digest_row.digest if digest_row else "")


def _tracked_clusters(threads: list[dict], cluster_to_tid: dict[str, str]) -> dict[str, dict]:
    """合并 已过 min_days 门槛的 threads(带 days/name)+ 簇→线映射 → {cluster_id: {thread_id, days, name}}。

    只有「在追」里露得出的线(达门槛)才给徽标 → 日报徽标与在追视图口径一致。纯函数。
    """
    by_tid = {t["thread_id"]: t for t in threads}
    out: dict[str, dict] = {}
    for cid, tid in cluster_to_tid.items():
        t = by_tid.get(tid)
        if t:
            out[cid] = {"thread_id": tid, "days": t["days"], "name": t["name"]}
    return out


async def _build_payload(
    session, settings: Settings, interest_key: str, title: str,
    domains: list[DomainSpec] | None = None, run_id: str | None = None,
) -> DeliverPayload:
    domains = domains or [{"key": "ai", "label": title, "interest_key": interest_key}]
    _require_domain_keys(domains)  # 缺键提前报清晰错误,不在下面索引时炸裸 KeyError

    # 事件线「在追」(跨领域,查一次)+「持续关注」徽标映射:达露出门槛(跨 >=min_days 天)的 active 线。
    # 失败不拖垮交付(事件线是增强);空 = 暂无跨天线(正常)。徽标须在拼 items 前备好。
    try:
        threads = await get_threads_for_display(
            session, min_days=settings.threads.min_days, tz_name=settings.app.timezone,
        )
        tracked = _tracked_clusters(threads, await get_active_thread_cluster_map(session))
    except Exception as exc:  # noqa: BLE001 — 增强功能,失败降级为不展示
        log.warning("deliver.threads.failed", error=str(exc), error_type=type(exc).__name__)
        threads, tracked = [], {}

    # 主领域(= interest_key,通常 AI):items/digest 给 feishu/微信/render 用(back-compat)
    items, digest = await _build_items(session, interest_key, tracked, run_id=run_id)

    # 多领域聚合(webapp):每领域各拼一份 {key,label,digest,items};无 domains 时退化为单主领域
    domain_payloads: list[dict] = []
    for d in domains:
        d_items, d_digest = (
            (items, digest) if d["interest_key"] == interest_key
            else await _build_items(session, d["interest_key"], tracked, run_id=run_id)
        )
        if not d_items:
            continue  # 空领域不进 App 下拉(免出现"点进去没内容")
        domain_payloads.append({
            "key": d["key"], "label": d["label"], "digest": d_digest, "items": d_items,
        })

    # GitHub 开源热榜(独立伪兴趣 ghboard)
    github = await _build_github_board(session, run_id=run_id)

    # 线的 domain 存的是 interest_key,前端按短键/标签展示 → 在此映射(GitHub 用伪兴趣)
    if threads:
        from pulsewire.github_board import GH_INTEREST_KEY
        ik_to_dom = {d["interest_key"]: (d["key"], d["label"]) for d in domains}
        ik_to_dom.setdefault(GH_INTEREST_KEY, ("github", "GitHub"))
        for t in threads:
            key, label = ik_to_dom.get(t["domain"], (t["domain"], t["domain"]))
            t["domain"], t["domain_label"] = key, label

    tz = ZoneInfo(settings.app.timezone)
    date_str = _logical_date(run_id, tz)  # f05:锚到 run 逻辑日,非交付钟点(治跨午夜错位)
    out_dir = PROJECT_ROOT / settings.render.output_dir
    img = out_dir / f"digest_{interest_key}.png"
    overview = out_dir / f"digest_{interest_key}_overview.png"
    gh_img = out_dir / "digest_ghboard.png"

    # 飞书自建应用推图清单(有序):各领域**详读全文版** digest_<key>.png(主领域在前)+ 热榜图;只收实际存在的。
    # 2026-06-28 用户明确要最详细那一版(每条标题 + 完整全文解析),不要中详卡 → 飞书推 full 长图;
    # full 缺失时退 *_mid 中详卡、再退 *_overview 速读卡兜底(老产物兼容)。深度全文同时仍在网页 App。
    # 只推本次 run 刚渲的图(_fresh_image);某域 render 失败留下的昨天旧图不上车(f04 通道三)。
    image_paths: list[str] = []
    key_to_img: dict[str, str] = {}  # 板 key → 本板新鲜图(飞书折叠卡内嵌用)
    for d in domains:
        d_full = out_dir / f"digest_{d['interest_key']}.png"
        d_mid = out_dir / f"digest_{d['interest_key']}_mid.png"
        d_ov = out_dir / f"digest_{d['interest_key']}_overview.png"
        if _fresh_image(d_full):
            image_paths.append(str(d_full))
            key_to_img[d["key"]] = str(d_full)
        elif _fresh_image(d_mid):
            image_paths.append(str(d_mid))
            key_to_img[d["key"]] = str(d_mid)
        elif _fresh_image(d_ov):
            image_paths.append(str(d_ov))
            key_to_img[d["key"]] = str(d_ov)
    if _fresh_image(gh_img):
        image_paths.append(str(gh_img))
    # 飞书折叠卡(feishu_card v2)按版取图:把本板新鲜图路径附到 domain_payloads(webapp 写 data.json 前会剥掉)
    for dp in domain_payloads:
        if dp["key"] in key_to_img:
            dp["image_path"] = key_to_img[dp["key"]]

    return DeliverPayload(
        interest_key=interest_key, title=title, date_str=date_str,
        digest=digest,
        items=items,
        image_path=str(img) if _fresh_image(img) else None,
        overview_image_path=str(overview) if _fresh_image(overview) else None,
        github=github,
        github_image_path=str(gh_img) if _fresh_image(gh_img) else None,
        domains=domain_payloads,
        image_paths=image_paths,
        threads=threads,
    )


async def _build_github_board(session, run_id: str | None = None) -> list[dict]:
    """读 ghboard 伪兴趣的精排+总结,拼成热榜 items(带 stars)。run_id 非空 → 只用本次 run 的稿。"""
    from pulsewire.github_board import GH_INTEREST_KEY

    rankings = await get_rankings(session, interest_key=GH_INTEREST_KEY)
    if not rankings:
        return []
    summaries = {s.item_id: s for s in await get_summaries(session, interest_key=GH_INTEREST_KEY, run_id=run_id)}
    meta = {it.item_id: it for it in await get_items_by_ids(session, [r.item_id for r in rankings])}
    out: list[dict] = []
    for r in rankings:
        s, m = summaries.get(r.item_id), meta.get(r.item_id)
        if s is None or m is None:
            continue
        stars = ((m.facts or {}).get("github") or {}).get("stars")
        out.append({
            "id": m.item_id, "headline": s.headline, "tldr": s.tldr_rendered,
            "insight": s.insight_rendered, "source": source_label(m.source), "url": m.url,
            # 同 github_board/engine.py:star 是硬事实,热榜豁免"待核实"(2026-06-15 一④⑤)。
            "stars": stars, "needs_review": False,
        })
    return out


def _enabled_channels(settings: Settings) -> list[str]:
    d = settings.deliver
    out = []
    if d.feishu.enabled:
        out.append("feishu")
    if d.wechat.enabled:
        out.append("wechat")
    if d.webapp.enabled:
        out.append("webapp")
    return out


async def run_deliver(
    settings: Settings,
    *,
    interest_key: str,
    title: str = "每日精选",
    run_id: str | None = None,
    domains: list[DomainSpec] | None = None,
    trigger_type: str = _TRIGGER,
    sessionmaker=None,
) -> dict:
    """把日报推到各启用渠道,幂等挡重复。返回各渠道结果。

    domains:[{key,label,interest_key}] 多领域聚合进 webapp 一份 index.html(下拉切换);
    None=只发主领域(interest_key)。投递幂等键仍按主领域算,一天一份 webapp。
    trigger_type 进幂等键(daily/event 各占一槽),默认 daily;event 触发传 'event' 免撞 daily 槽。
    """
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            payload = await _build_payload(session, settings, interest_key, title, domains, run_id=run_id)
            if not payload.items:
                raise RuntimeError(f"无可交付内容(interest_key={interest_key});请先 summarize")

            deliver_key = f"{interest_key}:{payload.date_str}"
            results: list[dict] = []
            feishu_delivered_today = False  # 飞书今日确认送达(本次发成功 或 幂等跳过=今天已发过)
            for channel in _enabled_channels(settings):
                # webapp 豁免幂等:本地文件重写不打扰任何人,当天重跑要刷新页面+档案。
                always_rewrite = channel in _ALWAYS_REWRITE
                if not always_rewrite and await has_delivery(
                    session, cluster_id=deliver_key, channel=channel, trigger_type=trigger_type
                ):
                    if channel == "feishu":
                        feishu_delivered_today = True  # 幂等跳过=今天确已送达过
                    results.append({"channel": channel, "status": "skipped", "reason": "今日已推(幂等)"})
                    continue
                # 飞书单通道双形态:mode=app 走自建应用推图,否则 webhook 文字卡。
                if channel == "feishu" and settings.deliver.feishu.mode == "app":
                    res = await feishu_app.send(payload, settings)
                else:
                    res = await _SENDERS[channel](payload, settings)
                # 幂等通道发成功才记账(下次好挡重复);豁免通道不记——它不参与幂等,
                # 且 deliveries 唯一键会在当天重跑时冲突。
                if res.status == "sent" and not always_rewrite:
                    await record_delivery(
                        session, cluster_id=deliver_key, channel=channel,
                        trigger_type=trigger_type, run_id=run_id, status="sent",
                    )
                elif res.status != "sent":
                    log.warning("deliver.channel", channel=channel, status=res.status, reason=res.reason)
                if channel == "feishu" and res.status == "sent":
                    feishu_delivered_today = True
                results.append({"channel": channel, "status": res.status, "reason": res.reason})

    # 交付哨兵收据(2026-06-15 二⑥):飞书今日确认送达 → 写收据文件(本地日期),哨兵 07:30
    # 只读它判断"日报今天到底来没来",不依赖 DB/Docker(日报跑完会关 Docker)。
    if feishu_delivered_today:
        write_receipt("feishu", payload.date_str)

    sent = [r["channel"] for r in results if r["status"] == "sent"]
    return {
        "interest_key": interest_key,
        "deliver_key": deliver_key,
        "sent": sent,
        "results": results,
    }
