"""流水线编排器:把各站串成一次完整 run + 检查点断点续跑 + 失败多通道告警。[阶段 7]

设计:
- 顺序:fetch → dedup → enrich → rank → summarize → render → deliver。
- 检查点:每阶段完成把"最后完成的阶段"写进 `runs.stage`(独立小事务);
  崩了用同 run_id 再跑,从最后完成阶段的下一站续跑。各站 upsert/幂等,续跑不重复产数据。
- run_id:默认 `<trigger>_<YYYYMMDD>`(应用时区)——launchd 每日一跑天然幂等;
  同 run_id 已 succeeded → 直接跳过(双触发/手动重跑安全),--force 可重跑全部。
- 失败语义:任一站抛错 → 记 runs.status=failed + 记录 + 多通道告警,再冒泡;绝不静默产空日报。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pulsewire.config import Settings
from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.obs.alert import alert_failure
from pulsewire.obs.meter import meter_snapshot, reset_meter
from pulsewire.rank import interest_key as make_interest_key
from pulsewire.store import (
    create_run,
    finish_run,
    get_run,
    get_sessionmaker,
    set_run_stage,
)


@dataclass(slots=True)
class DomainSpec:
    """一次 run 里的一个领域(= 一份独立兴趣)。custom 单兴趣模式 domain=None(rank 不按领域过滤)。"""

    key: str
    label: str
    interest: str
    tags: list[str]
    interest_key: str
    required: bool
    domain: str | None  # rank 领域过滤值;custom 单兴趣 = None
    # 该领域新鲜窗覆盖(小时);None=用 event_pool 全局 48h。events 引擎 step7 按域取窗(见 docs/DESIGN.md §2.7)。
    # ⚠️ 必须从 DomainCfg 带过来:run_event_rank 收的是 DomainSpec(非 DomainCfg),漏带=按域放宽在生产静默失效(48h)。
    freshness_window_hours: int | None = None


@dataclass(slots=True)
class RunContext:
    interest: str  # 主领域兴趣(= 第一个 required;run meta / deliver 标题用)
    tags: list[str]
    interest_key: str  # 主领域 interest_key(= 投递幂等键)
    fulltext: bool
    trigger_type: str
    run_id: str
    domains: list[DomainSpec]  # 本次 run 的全部领域(rank→…→render 各跑一遍)


# --------------------------------------------------------------------------- #
# 各站 runner:吃 (settings, ctx),返回指标 dict(条数/耗时由编排层补)            #
# --------------------------------------------------------------------------- #
async def _stage_fetch(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.config import load_sources
    from pulsewire.fetch import fetch_and_store

    return await fetch_and_store(load_sources(), settings)


async def _stage_dedup(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.dedup import run_dedup

    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            return await run_dedup(session, settings)


async def _stage_enrich(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.enrich import run_enrich

    return await run_enrich(settings, fulltext=True if ctx.fulltext else None)


async def _for_domains(
    ctx: RunContext, settings: Settings, stage: str, runner, domains=None,
    concurrent: bool = False,
) -> dict:
    """对每个领域跑 runner(d) + 领域级隔离:

    required 领域(主报 AI)失败 → 冒泡(整跑失败);次领域(bio/geo)失败 → 记录 + 多通道告警
    + 跳过,不拖垮主报(守"绝不静默产空"同时让次领域可独立失败)。返回 {domains:[每领域指标]}。

    concurrent=True:各领域并发跑(LLM 重的 summarize 用,域间无依赖、各开独立 session)。
    失败语义、结果汇总顺序均与串行版一致(按领域原序);required 失败照样冒泡整跑失败。
    """
    log = get_logger()
    doms = list(domains if domains is not None else ctx.domains)
    per: list[dict] = []

    async def _record(d, res, exc):
        if exc is not None:
            if d.required:
                raise exc
            err = f"{type(exc).__name__}: {exc}"
            log.error("pipeline.domain.failed", run_id=ctx.run_id, stage=stage,
                      domain=d.key, error=str(exc), error_type=type(exc).__name__)
            await alert_failure(settings, run_id=ctx.run_id, stage=f"{stage}:{d.key}",
                                error=str(exc), error_type=type(exc).__name__)
            per.append({"domain": d.key, "status": "skipped", "error": err})
            return
        per.append({"domain": d.key, **{k: v for k, v in res.items()
                                        if k not in ("top", "digest", "items", "results")}})

    if concurrent:
        async def _run_one(d):
            try:
                return d, (await runner(d) or {}), None
            except Exception as exc:  # noqa: BLE001 — 失败语义在 _record 按 required 处理
                return d, None, exc
        for d, res, exc in await asyncio.gather(*(_run_one(d) for d in doms)):
            await _record(d, res, exc)  # 按领域原序汇总;required 异常在此冒泡
    else:
        for d in doms:
            try:
                res = await runner(d) or {}
            except Exception as exc:  # noqa: BLE001
                await _record(d, None, exc)  # required → 立即冒泡(不跑后续)
                continue
            await _record(d, res, None)
    return {"domains": per}


async def _domains_with_rankings(ctx: RunContext) -> list[DomainSpec]:
    """只保留已产出 rankings 的领域(rank 后用):空领域跳过下游,免触发"无内容"异常 + 误告警。"""
    from pulsewire.store import get_rankings

    sm = get_sessionmaker()
    active: list[DomainSpec] = []
    async with sm() as session:
        for d in ctx.domains:
            if await get_rankings(session, interest_key=d.interest_key):
                active.append(d)
    return active


async def _stage_rank_events(settings: Settings, ctx: RunContext) -> dict:
    """选稿引擎 v2(全局事件池):一次全局聚类+打分+分配,写各域 live rankings(同 legacy 表口径,
    下游零改动)。主/次域失败语义同 legacy(闭 codex F2):主域 AI kept=0 → 冒泡;次域 kept=0 → 跳过。
    """
    from pulsewire.events.engine import run_event_rank

    log = get_logger()
    res = await run_event_rank(settings, domains=ctx.domains, run_id=ctx.run_id, shadow=False)
    kept = res.get("domains", {})
    per: list[dict] = []
    for d in ctx.domains:
        n = kept.get(d.key, 0)
        if d.required and n == 0:
            raise RuntimeError(
                f"主报领域 {d.key} 事件引擎 0 条入选(events={res.get('events')} "
                f"merges={res.get('merges')} judge_capped={res.get('judge_capped')})"
            )
        per.append({"domain": d.key, "kept": n, "provider": "events",
                    "events": res.get("events"), "merges": res.get("merges")})
        if n == 0:
            log.warning("pipeline.domain.skipped", run_id=ctx.run_id, stage="rank",
                        domain=d.key, note="事件引擎该域 0 条,下游自动跳过")
    return {"domains": per}


async def _stage_rank(settings: Settings, ctx: RunContext) -> dict:
    if settings.rank.engine == "events":  # 选稿引擎 v2(默认 legacy;A/B+签字后才切)
        return await _stage_rank_events(settings, ctx)
    from pulsewire.rank import run_rank

    async def _run(d: DomainSpec) -> dict:
        res = await run_rank(settings, interest=d.interest, tags=d.tags,
                             limit=None, domain=d.domain, run_id=ctx.run_id)
        # required 领域(主报)排到 0 条 = 整跑该失败,在 rank 站就抛(别拖到 deliver "无内容"才炸,
        # 失败点偏晚难定位)。次领域 kept=0 不抛——下游 _domains_with_rankings 会自动跳过它。
        if d.required and res.get("kept", 0) == 0:
            raise RuntimeError(f"主报领域 {d.key} rank 后 0 条入选(interest_key={d.interest_key});"
                               f"recalled={res.get('recalled')} domain_dropped={res.get('domain_dropped')}")
        return res

    return await _for_domains(ctx, settings, "rank", _run)


async def _stage_transcript(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.transcript import run_transcript

    active = await _domains_with_rankings(ctx)

    async def _run(d: DomainSpec) -> dict:
        return await run_transcript(settings, interest_key=d.interest_key, run_id=ctx.run_id)

    return await _for_domains(ctx, settings, "transcript", _run, domains=active)


async def _stage_summarize(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.summarize import run_summarize

    active = await _domains_with_rankings(ctx)

    async def _run(d: DomainSpec) -> dict:
        return await run_summarize(settings, interest_key=d.interest_key, run_id=ctx.run_id)

    # 域间并发:三板块写稿无依赖、各开独立 session,并发省下"AI 写完才轮 bio"的串行等待。
    return await _for_domains(ctx, settings, "summarize", _run, domains=active, concurrent=True)


async def _stage_github_board(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.github_board import run_github_board

    summary = await run_github_board(settings, run_id=ctx.run_id, trigger_type=ctx.trigger_type)
    summary.pop("items", None)  # 大对象不进检查点日志
    return summary


async def _domains_with_summaries(ctx: RunContext) -> list[DomainSpec]:
    """只保留【本次 run】已产出 summaries 的领域(render/deliver 用):某域今日 summarize
    失败/跳过 → 不出图/不交付(而非拿上次残留旧稿冒充今天,f04);免重复告警。"""
    from pulsewire.store import get_summaries

    sm = get_sessionmaker()
    active: list[DomainSpec] = []
    async with sm() as session:
        for d in ctx.domains:
            if await get_summaries(session, interest_key=d.interest_key, run_id=ctx.run_id):
                active.append(d)
    return active


async def _stage_threads(settings: Settings, ctx: RunContext) -> dict:
    """事件线归线站:把今日入选簇归到跨天事件线(在 summarize 之后)。

    增强功能,**失败只告警、不拖垮主交付**——自吞异常返回 error 字段,不向 pipeline 抛。
    """
    if not settings.threads.enabled:
        return {"enabled": False}
    from pulsewire.threads import run_threads

    interest_keys = [d.interest_key for d in ctx.domains]
    try:
        return await run_threads(settings, interest_keys=interest_keys, run_id=ctx.run_id)
    except Exception as exc:  # noqa: BLE001 — 事件线绝不能拖垮日报
        get_logger().error(
            "threads.stage.failed", run_id=ctx.run_id,
            error=str(exc), error_type=type(exc).__name__,
        )
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _stage_render(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.render import run_render

    active = await _domains_with_summaries(ctx)

    async def _run(d: DomainSpec) -> dict:
        return await run_render(settings, interest_key=d.interest_key, category=d.label, run_id=ctx.run_id)

    return await _for_domains(ctx, settings, "render", _run, domains=active)


async def _stage_deliver(settings: Settings, ctx: RunContext) -> dict:
    from pulsewire.deliver import run_deliver

    active = await _domains_with_summaries(ctx)
    domains = [{"key": d.key, "label": d.label, "interest_key": d.interest_key} for d in active]
    summary = await run_deliver(
        settings, interest_key=ctx.interest_key, title=ctx.interest,
        run_id=ctx.run_id, domains=domains, trigger_type=ctx.trigger_type,
    )
    summary.pop("results", None)
    return summary


# 顺序即流水线;名字也是 runs.stage 的检查点值
STAGES: list[tuple[str, object]] = [
    ("fetch", _stage_fetch),
    ("dedup", _stage_dedup),
    ("enrich", _stage_enrich),
    ("rank", _stage_rank),
    ("transcript", _stage_transcript),
    ("summarize", _stage_summarize),
    ("github_board", _stage_github_board),
    ("threads", _stage_threads),
    ("render", _stage_render),
    ("deliver", _stage_deliver),
]
STAGE_NAMES: list[str] = [n for n, _ in STAGES]


def default_run_id(settings: Settings, trigger_type: str) -> str:
    """默认 run_id = `<trigger>_<YYYYMMDD>`(应用时区)。同日同触发 = 同 run_id(幂等/续跑)。"""
    tz = ZoneInfo(settings.app.timezone)
    return f"{trigger_type}_{datetime.now(tz):%Y%m%d}"


def _start_index(completed_stage: str | None) -> int:
    """据"最后完成的阶段"算续跑起点(下一站)。"""
    if not completed_stage or completed_stage not in STAGE_NAMES:
        return 0
    return STAGE_NAMES.index(completed_stage) + 1


def _build_domains(
    settings: Settings, *, explicit_interest: str | None, explicit_tags: list[str] | None
) -> list[DomainSpec]:
    """构建本次 run 的领域列表(见 run_pipeline 注释)。"""
    if explicit_interest:  # CLI 显式单兴趣:不按领域过滤(domain=None)
        t = list(explicit_tags) if explicit_tags is not None else []
        return [DomainSpec(
            key="custom", label=explicit_interest, interest=explicit_interest, tags=t,
            interest_key=make_interest_key(explicit_interest, t), required=True, domain=None,
        )]
    cfg_domains = [d for d in settings.run.domains if d.enabled]
    if cfg_domains:
        return [DomainSpec(
            key=d.key, label=d.label, interest=d.interest, tags=list(d.tags),
            interest_key=make_interest_key(d.interest, list(d.tags)),
            required=d.required, domain=d.key,
            freshness_window_hours=d.freshness_window_hours,  # 带过来,否则按域放宽生产静默失效
        ) for d in cfg_domains]
    # config 没配 domains → 回退单兴趣(run.interest),不按领域过滤
    t = list(settings.run.tags)
    return [DomainSpec(
        key="ai", label="AI", interest=settings.run.interest, tags=t,
        interest_key=make_interest_key(settings.run.interest, t), required=True, domain=None,
    )]


async def run_pipeline(
    settings: Settings,
    *,
    interest: str | None = None,
    tags: list[str] | None = None,
    run_id: str | None = None,
    trigger_type: str | None = None,
    resume: bool = True,
    force: bool = False,
    fulltext: bool | None = None,
    sessionmaker=None,
    stages: list[tuple[str, object]] | None = None,
) -> dict:
    """跑一次完整流水线。返回 {run_id, status, skipped, stages:[{stage, seconds, ...}]}。

    失败时:写 runs.status=failed(stage=最后完成阶段)+ 多通道告警,再冒泡异常。
    sessionmaker / stages 仅供测试注入;生产默认全局 sessionmaker + STAGES。
    """
    log = get_logger()
    stages = stages if stages is not None else STAGES
    trigger_type = trigger_type or settings.run.trigger_type
    fulltext = settings.run.fulltext if fulltext is None else fulltext
    run_id = run_id or default_run_id(settings, trigger_type)

    # 领域列表:CLI 显式传兴趣 = 单 custom 领域(rank 不按领域过滤);否则用 config.run.domains;
    # config 没配 domains 则回退单兴趣(run.interest)。主领域(首个 required)= run meta / 投递键。
    domains = _build_domains(settings, explicit_interest=interest, explicit_tags=tags)
    primary = next((d for d in domains if d.required), domains[0])
    interest, tags, key = primary.interest, primary.tags, primary.interest_key
    ctx = RunContext(
        interest=interest, tags=tags, interest_key=key,
        fulltext=fulltext, trigger_type=trigger_type, run_id=run_id, domains=domains,
    )
    sm = sessionmaker or get_sessionmaker()

    # 1) 决定从哪开始:新建 / 续跑 / 已成功跳过
    skip = False
    completed_stage: str | None = None
    async with sm() as session:
        async with session.begin():
            existing = await get_run(session, run_id)
            if existing is None:
                await create_run(
                    session, trigger_type=trigger_type, run_id=run_id,
                    meta={"interest": interest, "tags": tags, "interest_key": key,
                          "domains": [d.key for d in ctx.domains]},
                )
            elif existing.status == "succeeded" and not force:
                skip = True
            else:
                # 续跑失败/未完成的 run(或 force 重跑):回到 running,清结束态
                completed_stage = None if force else (existing.stage if resume else None)
                existing.status = "running"
                existing.error = None
                existing.finished_at = None
                existing.stage = completed_stage

    if skip:
        log.info("run.already_done", run_id=run_id, note="同 run_id 已成功;--force 可重跑全部")
        return {"run_id": run_id, "status": "succeeded", "skipped": True, "stages": []}

    # 1.5) 开跑前 DeepSeek 余额预检(f16 / 治 2026-07-02 E1):余额确切低于阈值 → 不跑 + 告警,
    #      在烧任何一站前掐断"欠费→判官全 fail-open 出毒日报→每5分钟全量重试风暴"整条链。
    #      best-effort:查不到余额放行(见 preflight.py);拦下写 failed(stage=None,下次充值后从头重跑)。
    if settings.run.preflight_balance_enabled and settings.summarize.backend == "api":
        from pulsewire.preflight import balance_below_floor, check_deepseek_balance
        balance = await check_deepseek_balance(settings)
        if balance_below_floor(balance, settings.run.preflight_min_balance):
            floor = settings.run.preflight_min_balance
            msg = (
                f"DeepSeek 余额 {balance} 低于阈值 {floor}——今天不跑,先充值。"
                "(拦在开跑前,避免烧空 402 引发判官全放行的毒日报 + 每5分钟全量重试风暴)"
            )
            log.error("preflight.balance.too_low", run_id=run_id, balance=balance, floor=floor)
            await alert_failure(
                settings, run_id=run_id, stage="preflight", error=msg,
                error_type="InsufficientBalance",
            )
            try:
                async with sm() as session:
                    async with session.begin():
                        await finish_run(session, run_id, status="failed", stage=None, error=msg)
            except Exception as db_exc:  # noqa: BLE001 — 写库失败不能盖住预检拦截
                log.error(
                    "preflight.finish_run.failed", run_id=run_id,
                    error=str(db_exc), error_type=type(db_exc).__name__,
                )
            return {
                "run_id": run_id, "status": "failed", "skipped": False,
                "aborted": "preflight_balance", "stages": [],
            }

    start_idx = _start_index(completed_stage)
    reset_meter()  # 计量清零:本次 run 的 LLM token 用量从头累计(基线/省 token 度量)
    log.info(
        "run.start", run_id=run_id, trigger_type=trigger_type, interest=interest,
        interest_key=key, resume_from=STAGE_NAMES[start_idx] if start_idx < len(STAGE_NAMES) else "—",
        skipping=STAGE_NAMES[:start_idx] or "—",
    )

    # 2) 逐站跑 + 检查点(+ 整跑总超时看门狗:单站不得超过剩余总预算,防异常拖死整跑)
    stage_metrics: list[dict] = []
    deadline = time.monotonic() + settings.run.total_timeout_minutes * 60.0
    for name, runner in stages[start_idx:]:
        t0 = time.monotonic()
        log.info("pipeline.stage.start", run_id=run_id, stage=name)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"整跑超总预算 {settings.run.total_timeout_minutes} 分钟(看门狗),停在 {name} 站前"
                )
            # 超时冒泡走下面同一套失败处理(告警 + 写 failed + 续跑检查点);wait_for 在 await 点取消该站
            metrics = await asyncio.wait_for(runner(settings, ctx), timeout=remaining)
        except Exception as exc:
            elapsed = round(time.monotonic() - t0, 2)
            err = f"{type(exc).__name__}: {exc}"
            # 永久错(没钱/凭证失效)= 判官分类熔断(f01),给用户看得懂的告警文案;其余原样。
            alert_err = (
                f"LLM 供应商永久错,已熔断(多半没钱/凭证失效,快充值或换 key):{exc}"
                if isinstance(exc, PermanentLLMError) else str(exc)
            )
            log.error(
                "pipeline.stage.failed", run_id=run_id, stage=name, seconds=elapsed,
                error=str(exc), error_type=type(exc).__name__,
            )
            # 告警必须先发、且不依赖 DB:2026-06-15 静默开天窗事故根因=告警曾排在 finish_run
            # 写库之后,postgres 断连时写库二次抛错把告警挤掉(alert.sent=0)。告警走公网 httpx
            # 本不需要 DB;alert_failure 本身 best-effort 不抛。写检查点单独包 try,写库挂了
            # 也只记日志、不掩盖原始失败、不挡住下面的 raise。
            await alert_failure(
                settings, run_id=run_id, stage=name, error=alert_err, error_type=type(exc).__name__
            )
            # 检查点:stage=最后完成阶段(下次同 run_id 从此续跑);状态 failed
            try:
                async with sm() as session:
                    async with session.begin():
                        await finish_run(session, run_id, status="failed", stage=completed_stage, error=err)
            except Exception as db_exc:  # noqa: BLE001 — 写库失败不能盖住原始 stage 失败
                log.error(
                    "pipeline.finish_run.failed", run_id=run_id,
                    error=str(db_exc), error_type=type(db_exc).__name__,
                )
            raise

        elapsed = round(time.monotonic() - t0, 2)
        # 检查点推进(独立小事务):记录最后完成的阶段
        async with sm() as session:
            async with session.begin():
                await set_run_stage(session, run_id, name)
        completed_stage = name
        clean = {k: v for k, v in (metrics or {}).items() if k not in ("top", "digest", "results")}
        log.info("pipeline.stage.done", run_id=run_id, stage=name, seconds=elapsed, **clean)
        stage_metrics.append({"stage": name, "seconds": elapsed, **clean})

    # 3) 收尾
    async with sm() as session:
        async with session.begin():
            await finish_run(session, run_id, status="succeeded", stage=STAGE_NAMES[-1])
    total = round(sum(s["seconds"] for s in stage_metrics), 2)
    log.info("run.done", run_id=run_id, status="succeeded", stages=len(stage_metrics), seconds=total)
    # LLM token 用量总账(基线/省 token 度量):一行总计 + 每 (stage,model) 明细。
    snap = meter_snapshot()
    log.info(
        "run.llm_usage", run_id=run_id, total_calls=snap["total_calls"],
        prompt_tokens=snap["total_prompt_tokens"], completion_tokens=snap["total_completion_tokens"],
        cached_tokens=snap["total_cached_tokens"],
        uncached_prompt_tokens=snap["total_uncached_prompt_tokens"],
        total_tokens=snap["total_tokens"], cache_hit_rate=snap["cache_hit_rate"],
        errors=snap["total_errors"],
    )
    for r in snap["rows"]:
        log.info("run.llm_usage.stage", run_id=run_id, **r)
    return {"run_id": run_id, "status": "succeeded", "skipped": False,
            "stages": stage_metrics, "llm_usage": snap}
