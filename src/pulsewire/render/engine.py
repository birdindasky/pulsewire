"""出图引擎:把某兴趣的对账后总结渲染成两张「暖白+软黑+粉」便签风 PNG。

- 详读长图 digest_<key>.png:1 列长卡,headline + tldr + 详细 insight。
- 速读卡  digest_<key>_overview.png:tldr 一句话清单。
只用 *_rendered(verify 替换真实数字后的成稿,**已对账**),绝不用含占位的 raw。
needs_review 条目标"待核实"徽标、不静默丢。无头 Chrome 截全页长图。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pulsewire.config import PROJECT_ROOT, source_label
from pulsewire.obs import get_logger
from pulsewire.store import (
    get_digest,
    get_items_by_ids,
    get_rankings,
    get_sessionmaker,
    get_summaries,
)

from .templates import build_html, build_midview_html, build_overview_html

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


async def _screenshot(html: str, out_path: Path, settings: Settings) -> None:
    """无头 Chrome 渲染 HTML 截全页长图。超时/失败自动重试,耗尽才冒泡(不静默产空图)。

    机器忙时(Docker+LLM+大模型挤内存)单次截图可能撑爆超时;整页重渲是幂等的,
    每次重试都换一个全新浏览器(弃掉可能卡死的旧实例)。重试次数/超时见 `render.retries`/`render.timeout_ms`。
    """
    from playwright.async_api import async_playwright

    width = settings.render.width
    timeout_ms = settings.render.timeout_ms
    attempts = settings.render.retries + 1
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with async_playwright() as pw:
                launch_kwargs = {"channel": "chrome"} if settings.render.use_system_chrome else {}
                browser = await pw.chromium.launch(**launch_kwargs)
                try:
                    page = await browser.new_page(viewport={"width": width, "height": 10})
                    page.set_default_timeout(timeout_ms)
                    page.set_default_navigation_timeout(timeout_ms)  # set_content 走导航超时
                    await page.set_content(html, wait_until="networkidle")
                    await page.wait_for_timeout(settings.render.settle_ms)
                    await page.screenshot(path=str(out_path), full_page=True)
                finally:
                    await browser.close()
            return
        except Exception as err:  # noqa: BLE001 — 超时/OOM/浏览器崩,整页重渲幂等可重试
            last_err = err
            if attempt < attempts:
                log.warning(
                    "render.screenshot.retry",
                    out=str(out_path),
                    attempt=attempt,
                    of=attempts,
                    error=str(err),
                )
            else:
                log.error(
                    "render.screenshot.failed",
                    out=str(out_path),
                    attempts=attempts,
                    error=str(err),
                )
    assert last_err is not None  # attempts>=1 必至少跑一轮;走到这里必有 last_err
    raise last_err


async def run_render(
    settings: Settings,
    *,
    interest_key: str,
    category: str = "每日精选",
    out_path: str | None = None,
    run_id: str | None = None,
    sessionmaker=None,
) -> dict:
    """渲染某兴趣的日报两张 PNG(详读长图 + 速读卡),返回 {path, overview_path, items, needs_review}。

    run_id 非空 → 只用本次 run 的稿+概述出图(治 f04:别把上次残留旧稿渲进今天的图)。
    """
    sm = sessionmaker or get_sessionmaker()
    async with sm() as session:
        rankings = await get_rankings(session, interest_key=interest_key)
        summaries = {s.item_id: s for s in await get_summaries(session, interest_key=interest_key, run_id=run_id)}
        digest_row = await get_digest(session, interest_key=interest_key, run_id=run_id)
        item_ids = [r.item_id for r in rankings]
        items_meta = {it.item_id: it for it in await get_items_by_ids(session, item_ids)}
        # 「追·第N天」红章(已剪记忆②):达露出门槛(≥min_days 天)的在追线,其簇上的剪报盖章。
        # render 在 threads 归线站之后跑,今日簇已挂线 → days 含今天,与「在追」视图口径一致。
        # 增强功能:查询失败=不盖章,绝不拖垮出图(与 deliver 侧同纪律)。
        tracked: dict[str, int] = {}
        try:
            from pulsewire.store import get_active_thread_cluster_map, get_threads_for_display

            threads = await get_threads_for_display(
                session, min_days=settings.threads.min_days, tz_name=settings.app.timezone)
            days_by_tid = {t["thread_id"]: t["days"] for t in threads}
            tracked = {cid: days_by_tid[tid]
                       for cid, tid in (await get_active_thread_cluster_map(session)).items()
                       if tid in days_by_tid}
        except Exception as exc:  # noqa: BLE001 — 章是增强,失败降级为不盖
            log.warning("render.threads.failed", error=str(exc), error_type=type(exc).__name__)

    if not summaries:
        raise RuntimeError(f"无总结可出图(interest_key={interest_key});请先跑 summarize")

    # 按精排名次排序,拼出图数据(只用对账后的 *_rendered)
    items: list[dict] = []
    needs_review = 0
    for r in rankings:
        s = summaries.get(r.item_id)
        meta = items_meta.get(r.item_id)
        if s is None or meta is None:
            continue
        if s.status != "ok":
            needs_review += 1
        items.append({
            "headline": s.headline,
            "tldr": s.tldr_rendered,
            "insight": s.insight_rendered,
            "source": source_label(meta.source),  # 机器 slug → 人类可读名(2026-06-15 一⑤)
            "url": meta.url,
            "needs_review": s.status != "ok",
            # 「追·第N天」红章(已剪记忆②;0/缺省=不盖,模板 falsy 短路)
            "tracking_days": tracked.get(r.cluster_id, 0) if r.cluster_id else 0,
        })

    tz = ZoneInfo(settings.app.timezone)
    now = datetime.now(tz)
    date_display = now.strftime("%Y · %m · %d")
    title = f"pulsewire · {category} · {now:%Y-%m-%d}"
    footer = f"pulsewire · 数字回源对账 · {now:%Y.%m.%d}"

    detail_html = build_html(
        title=title, date_display=date_display, category=category,
        digest=digest_row.digest if digest_row else "",
        items=items, footer_info=footer, width=settings.render.width,
    )
    overview_html = build_overview_html(
        title=title, date_display=date_display, category=category,
        items=items, footer_info=footer, width=settings.render.width,
    )
    midview_html = build_midview_html(
        title=title, date_display=date_display, category=category,
        items=items, footer_info=footer, width=settings.render.width,
    )

    out_dir = PROJECT_ROOT / settings.render.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    detail_path = out_path or str(out_dir / f"digest_{interest_key}.png")
    overview_path = str(out_dir / f"digest_{interest_key}_overview.png")
    midview_path = str(out_dir / f"digest_{interest_key}_mid.png")

    await _screenshot(detail_html, Path(detail_path), settings)
    await _screenshot(overview_html, Path(overview_path), settings)
    await _screenshot(midview_html, Path(midview_path), settings)
    log.info("render.done", path=detail_path, overview_path=overview_path,
             midview_path=midview_path, items=len(items), needs_review=needs_review)
    return {"path": detail_path, "overview_path": overview_path,
            "midview_path": midview_path, "items": len(items), "needs_review": needs_review}


async def render_overview_png(
    settings: Settings, *, items: list[dict], category: str, out_path: str,
    date_display: str, footer_info: str,
) -> str:
    """把一组 {headline,tldr,source,needs_review} 渲成一张速读清单 PNG(GitHub 热榜复用)。"""
    html = build_overview_html(
        title=f"pulsewire · {category}", date_display=date_display, category=category,
        items=items, footer_info=footer_info, width=settings.render.width,
    )
    await _screenshot(html, Path(out_path), settings)
    log.info("render.overview.done", path=out_path, items=len(items))
    return out_path


async def render_detail_png(
    settings: Settings, *, items: list[dict], category: str, out_path: str,
    date_display: str, footer_info: str, digest: str = "",
) -> str:
    """把一组带 insight 的条目渲成一张详读长图 PNG(GitHub 热榜飞书版专用):headline+repo+tldr+完整 insight。

    list 直渲、不经 DB(与新闻板走 DB 的 render_interest_png 不同);用 _DETAIL 模板出"详读长图"(最全):
    insight 全文、含 repo 链接。digest 留空 → 模板 `{% if digest %}` 自动不渲前言。
    """
    html = build_html(
        title=f"pulsewire · {category}", date_display=date_display, category=category,
        digest=digest, items=items, footer_info=footer_info, width=settings.render.width,
    )
    await _screenshot(html, Path(out_path), settings)
    log.info("render.detail.done", path=out_path, items=len(items))
    return out_path
