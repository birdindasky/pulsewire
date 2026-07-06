"""pulsewire 主入口。

阶段 0 只提供:
  - validate-config : 校验 config.yaml + sources.yaml(不连数据库)
  - healthcheck     : 校验配置 + 连接 PostgreSQL(docker compose 验收用)
  - fetch           : 并发抓取所有启用源并落库(阶段 2)
  - dedup           : 三级去重 + 跨源同事件合并归簇(阶段 3)
  - enrich          : 给条目挂 value+source_id(HN/GitHub 数字,可选正文)(阶段 4)
  - rank            : 兴趣分类(召回→精排+新鲜度门+限额)(阶段 4)
  - summarize       : 统一总结 + 结构化对账(每数字回源)(阶段 5)
  - render          : 把对账后总结出成 D 风格 PNG 日报图(阶段 6)
  - deliver         : 把日报推到飞书/微信/网页App,投递幂等挡重复(阶段 6)
  - run             : 串完整流水线 fetch→…→deliver + 检查点续跑 + 失败告警(阶段 7)
  - schedule        : 生成 launchd 每日调度 plist + 安装说明(阶段 7)
  - threads         : 事件线维护;--rebuild 从归档重放重建跨天事件线(事件线 step 4)
  - sentinel        : 交付哨兵——查今天日报送达没有,没有就告警(独立 launchd 07:30,二⑥)

用法:
  pulsewire <command>
  pulsewire enrich [--fulltext]
  pulsewire rank "<自然语言兴趣>" [--tags=a,b] [--limit=N]
  pulsewire summarize "<自然语言兴趣>" [--tags=a,b]   # 兴趣需先 rank 过
  pulsewire render "<自然语言兴趣>" [--tags=a,b]       # 兴趣需先 summarize 过
  pulsewire deliver "<自然语言兴趣>" [--tags=a,b]      # 兴趣需先 render 过
  pulsewire run ["<兴趣>"] [--tags=a,b] [--run-id=ID] [--trigger=daily] [--no-resume] [--force] [--fulltext]
  pulsewire schedule [--hour=8] [--minute=30]
  pulsewire threads --rebuild [--days=N]   # 从归档重放重建跨天事件线(默认全部天)
"""

from __future__ import annotations

import asyncio
import sys
import time

from pulsewire.config import get_settings, load_sources
from pulsewire.obs import configure_logging, get_logger

COMMANDS = (
    "validate-config", "healthcheck", "fetch", "dedup", "enrich", "rank",
    "transcript", "summarize", "render", "deliver", "run", "schedule", "threads",
    "sentinel", "audit-sources", "ask", "embed-cards",
)


def cmd_validate_config(log) -> int:
    settings = get_settings()
    sources = load_sources()
    enabled = [s for s in sources if s.enabled]
    log.info(
        "config.ok",
        environment=settings.app.environment,
        timezone=settings.app.timezone,
        db_host=settings.database.host,
        db_name=settings.database.name,
        sources_total=len(sources),
        sources_enabled=len(enabled),
        event_min_sources=settings.event.min_sources,
        summarize_model=settings.summarize.model,
    )
    log.info("config.validate.passed")
    return 0


async def _healthcheck(log) -> int:
    from pulsewire.store import ping_database

    cmd_validate_config(log)
    log.info("db.connecting")
    info = await ping_database()
    log.info("db.ok", **info)
    if not info.get("pgvector_available"):
        log.warning("db.pgvector_missing", hint="需使用 pgvector/pgvector 镜像")
    log.info("healthcheck.passed")
    return 0


async def _fetch(log) -> int:
    from pulsewire.fetch import fetch_and_store

    settings = get_settings()
    sources = load_sources()
    enabled = [s for s in sources if s.enabled]
    log.info("fetch.start", sources_enabled=len(enabled))
    t0 = time.monotonic()
    summary = await fetch_and_store(sources, settings)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("fetch.done", **summary)
    return 0


async def _dedup(log) -> int:
    from pulsewire.dedup import run_dedup
    from pulsewire.store import get_sessionmaker

    settings = get_settings()
    log.info(
        "dedup.start",
        provider=settings.dedup.embedding.provider,
        model=settings.dedup.embedding.model,
        threshold=settings.dedup.embedding.similarity_threshold,
    )
    t0 = time.monotonic()
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            summary = await run_dedup(session, settings)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("dedup.done", **summary)
    return 0


async def _enrich(log, argv: list[str]) -> int:
    from pulsewire.enrich import run_enrich

    settings = get_settings()
    fulltext = "--fulltext" in argv
    log.info("enrich.start", fulltext=fulltext or settings.enrich.fulltext)
    t0 = time.monotonic()
    summary = await run_enrich(settings, fulltext=True if fulltext else None)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("enrich.done", **summary)
    return 0


def _parse_rank_args(argv: list[str]) -> tuple[str, list[str], int | None, str | None]:
    interest_parts: list[str] = []
    tags: list[str] = []
    limit: int | None = None
    domain: str | None = None
    for arg in argv:
        if arg.startswith("--tags="):
            tags = [t.strip() for t in arg[len("--tags="):].split(",") if t.strip()]
        elif arg.startswith("--limit="):
            limit = int(arg[len("--limit="):])
        elif arg.startswith("--domain="):
            domain = arg[len("--domain="):].strip() or None
        else:
            interest_parts.append(arg)
    return " ".join(interest_parts).strip(), tags, limit, domain


def _resolve_rank_domain(settings, interest: str, tags: list[str], domain: str | None) -> str | None:
    """决定本次手动 rank 的领域过滤值。

    显式 --domain 优先;否则按 config.run.domains 的兴趣+tags 精确匹配,命中则套用其 key
    (防手动单跑某领域兴趣时,候选不过滤污染 rankings)。都不命中 = None(不过滤,旧行为)。
    """
    if domain is not None:
        return domain
    for d in settings.run.domains:
        if d.enabled and d.interest == interest and list(d.tags) == list(tags):
            return d.key
    return None


async def _rank(log, argv: list[str]) -> int:
    from pulsewire.rank import run_rank

    settings = get_settings()
    interest, tags, limit, domain = _parse_rank_args(argv)
    if not interest:
        log.error("rank.no_interest",
                  hint='用法:pulsewire rank "<自然语言兴趣>" [--tags=a,b] [--limit=N] [--domain=ai|bio|geo]')
        return 2
    domain = _resolve_rank_domain(settings, interest, tags, domain)
    log.info("rank.start", interest=interest, tags=tags, domain=domain,
             provider=settings.rank.rerank_provider)
    t0 = time.monotonic()
    summary = await run_rank(settings, interest=interest, tags=tags, limit=limit, domain=domain)
    top = summary.pop("top", [])
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("rank.done", **summary)
    for row in top:
        nums = " ".join(f"{e['label']}={e['value']}({e['source_id']})" for e in row.get("enriched", []))
        log.info("rank.item", rank=row["rank"], final=row["final"], recall=row["recall"],
                 source=row["source"], title=row["title"], facts=nums or "—")
    return 0


async def _transcript(log, argv: list[str]) -> int:
    from pulsewire.rank import interest_key as make_interest_key
    from pulsewire.transcript import run_transcript

    settings = get_settings()
    interest, tags, _, _ = _parse_rank_args(argv)
    if not interest:
        log.error("transcript.no_interest", hint='用法:pulsewire transcript "<兴趣>" [--tags=a,b](需先 rank 过)')
        return 2
    key = make_interest_key(interest, tags)
    log.info("transcript.start", interest=interest, interest_key=key)
    t0 = time.monotonic()
    summary = await run_transcript(settings, interest_key=key)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("transcript.done", **summary)
    return 0


async def _summarize(log, argv: list[str]) -> int:
    from pulsewire.rank import interest_key as make_interest_key
    from pulsewire.summarize import run_summarize

    settings = get_settings()
    interest, tags, _, _ = _parse_rank_args(argv)
    if not interest:
        log.error("summarize.no_interest", hint='用法:pulsewire summarize "<兴趣>" [--tags=a,b](需先 rank 过)')
        return 2
    key = make_interest_key(interest, tags)
    log.info("summarize.start", interest=interest, interest_key=key, backend=settings.summarize.backend)
    t0 = time.monotonic()
    summary = await run_summarize(settings, interest_key=key)
    digest = summary.pop("digest", "")
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("summarize.done", **summary)
    if digest:
        log.info("summarize.digest", text=digest)
    return 0


async def _render(log, argv: list[str]) -> int:
    from pulsewire.rank import interest_key as make_interest_key
    from pulsewire.render import run_render

    settings = get_settings()
    interest, tags, _, _ = _parse_rank_args(argv)
    if not interest:
        log.error("render.no_interest", hint='用法:pulsewire render "<兴趣>" [--tags=a,b](需先 summarize 过)')
        return 2
    key = make_interest_key(interest, tags)
    log.info("render.start", interest=interest, interest_key=key)
    t0 = time.monotonic()
    summary = await run_render(settings, interest_key=key, category=interest)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("render.done", **summary)
    return 0


async def _deliver(log, argv: list[str]) -> int:
    from pulsewire.deliver import run_deliver
    from pulsewire.rank import interest_key as make_interest_key

    settings = get_settings()
    interest, tags, _, _ = _parse_rank_args(argv)
    if not interest:
        log.error("deliver.no_interest", hint='用法:pulsewire deliver "<兴趣>" [--tags=a,b](需先 render 过)')
        return 2
    key = make_interest_key(interest, tags)
    log.info("deliver.start", interest=interest, interest_key=key)
    t0 = time.monotonic()
    summary = await run_deliver(settings, interest_key=key, title=interest,
                                trigger_type=settings.run.trigger_type)
    results = summary.pop("results", [])
    summary["seconds"] = round(time.monotonic() - t0, 2)
    log.info("deliver.done", **summary)
    for r in results:
        log.info("deliver.channel.result", **r)
    return 0


async def _run(log, argv: list[str]) -> int:
    """跑一次完整流水线(fetch→…→deliver)+ 检查点续跑 + 失败告警。

    用法:pulsewire run ["<兴趣>"] [--tags=a,b] [--run-id=ID] [--trigger=daily|event]
                       [--no-resume] [--force] [--fulltext]
    兴趣/标签缺省时取 config.yaml 的 run.interest / run.tags(v1 单一兴趣)。
    """
    from pulsewire.pipeline import run_pipeline

    settings = get_settings()
    interest_parts: list[str] = []
    tags: list[str] | None = None
    run_id: str | None = None
    trigger: str | None = None
    resume = True
    force = False
    fulltext: bool | None = None
    for arg in argv:
        if arg.startswith("--tags="):
            tags = [t.strip() for t in arg[len("--tags="):].split(",") if t.strip()]
        elif arg.startswith("--run-id="):
            run_id = arg[len("--run-id="):].strip() or None
        elif arg.startswith("--trigger="):
            trigger = arg[len("--trigger="):].strip() or None
        elif arg == "--no-resume":
            resume = False
        elif arg == "--force":
            force = True
        elif arg == "--fulltext":
            fulltext = True
        else:
            interest_parts.append(arg)
    interest = " ".join(interest_parts).strip() or None

    result = await run_pipeline(
        settings, interest=interest, tags=tags, run_id=run_id,
        trigger_type=trigger, resume=resume, force=force, fulltext=fulltext,
    )
    return 0 if result.get("status") == "succeeded" else 1


async def _threads(log, argv: list[str]) -> int:
    """事件线维护。目前:--rebuild 从归档日报重放,重建跨天事件线。

    用法:pulsewire threads --rebuild [--days=N]
    --days=N 只重放最近 N 天归档(默认全部);重建后网页/桌面 app「在追」即刻显现跨天线。
    清空 threads/thread_clusters 后按日重建(线是派生数据,可反复重算)。
    """
    if "--rebuild" not in argv:
        log.error("threads.no_action", hint='用法:pulsewire threads --rebuild [--days=N]')
        return 2
    days: int | None = None
    for arg in argv:
        if arg.startswith("--days="):
            days = int(arg[len("--days="):])

    from pulsewire.threads.rebuild import rebuild_from_archive

    settings = get_settings()
    log.info("threads.rebuild.start", days=days or "all")
    t0 = time.monotonic()
    agg = await rebuild_from_archive(settings, days=days)
    agg["seconds"] = round(time.monotonic() - t0, 2)
    log.info("threads.rebuild.summary", **agg)
    return 0


async def _audit_sources(log, argv: list[str]) -> int:
    """源治理体检(三①):报告孤儿源(items 表有、注册表无)+ 停用源残留 + 注册表分布。

    用法:pulsewire audit-sources [--cleanup]
    默认只读不删;--cleanup 删掉孤儿源残留条目(级联清依赖,不碰 threads)= destructive,需显式加。
    """
    from collections import Counter

    from pulsewire.store import delete_orphan_items, get_sessionmaker, get_source_item_stats

    registry = {s.id: s for s in load_sources()}
    enabled = {sid for sid, s in registry.items() if s.enabled}
    by_dom = Counter(s.domain for s in registry.values())
    log.info("audit.registry", total=len(registry), enabled=len(enabled),
             disabled=len(registry) - len(enabled), by_domain=dict(by_dom))

    sm = get_sessionmaker()
    async with sm() as session:
        stats = await get_source_item_stats(session)

    orphans = [(s, c, last) for s, c, last in stats if s not in registry]
    disabled_resid = [(s, c, last) for s, c, last in stats if s in registry and s not in enabled]
    log.info("audit.orphans.summary", orphan_sources=len(orphans),
             orphan_items=sum(c for _, c, _ in orphans))
    for s, c, last in orphans:  # 已按条目数降序
        log.info("audit.orphan", source=s, items=c, last_published=str(last))
    log.info("audit.disabled_residual.summary", sources=len(disabled_resid),
             items=sum(c for _, c, _ in disabled_resid))
    for s, c, last in disabled_resid:
        log.info("audit.disabled_residual", source=s, items=c, last_published=str(last))
    if "--cleanup" in argv:
        async with sm() as session:
            async with session.begin():
                deleted = await delete_orphan_items(session, list(registry))
        log.info("audit.cleanup.done", deleted_items=deleted,
                 note="已删孤儿源残留条目(级联清 embeddings/summaries/rankings/timeline);threads 未动")
    else:
        log.info("audit.done", note="只读体检,未删;--cleanup 才清孤儿(destructive)")
    return 0


async def _sentinel(log, argv: list[str]) -> int:
    """交付哨兵(二⑥):查今天日报到底送达没有,没送达就多通道告警。独立 launchd 07:30 跑。

    用法:pulsewire sentinel [--channel=feishu]
    只读交付收据文件 + 发告警,不依赖 Docker/DB(日报跑完会关 Docker,哨兵不该再拉起)。
    """
    from pulsewire.obs.sentinel import check_delivery_sentinel

    settings = get_settings()
    channel = "feishu"
    for arg in argv:
        if arg.startswith("--channel="):
            channel = arg[len("--channel="):].strip() or "feishu"
    r = await check_delivery_sentinel(settings, channel=channel)
    log.info("sentinel.done", **r)
    return 0  # 哨兵自身永远成功退出(没送达=已告警,不是哨兵失败)


async def _ask(log, argv: list[str]) -> int:
    """语义问答翻历史(v2 主线B②):大白话问 → 引用式回答。

    用法:pulsewire ask "最近中东局势怎么样" [--json]
    --json:输出结构化 JSON(给 Electron 接线用);默认人话打印。
    """
    import json as _json

    from pulsewire.qa.engine import answer

    as_json = "--json" in argv
    question = " ".join(a for a in argv if not a.startswith("--")).strip()
    if not question:
        log.error("ask.no_question", hint='用法:pulsewire ask "你的问题"')
        return 2

    res = await answer(question)
    if as_json:
        print(_json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    # 人话打印
    print(f"\n问:{question}\n")
    print(res.get("answer", ""))
    cards = res.get("cards") or []
    if cards:
        print("\n引用:")
        for c in cards:
            print(f"  [{c['n']}] ({c.get('date')}) {c['headline']}")
    return 0 if res.get("ok") else 1


async def _embed_cards(log, argv: list[str]) -> int:
    """回填/增量:给档案卡(summaries)算 card_vec(语义问答召回用)+ 标 produced_by。

    幂等:只处理 card_vec IS NULL 的卡,可反复跑(也可日报后增量补当天新卡)。本地向量、零 API 成本。
    用法:pulsewire embed-cards [--limit=N]
    """
    from sqlalchemy import select, update

    from pulsewire.config import get_settings
    from pulsewire.dedup.embedding import get_embedder
    from pulsewire.store import get_sessionmaker
    from pulsewire.store.tables import Summary

    settings = get_settings()
    limit = None
    for arg in argv:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    embedder = get_embedder(settings)
    sm = get_sessionmaker()
    async with sm() as session:
        stmt = select(Summary).where(Summary.card_vec.is_(None)).order_by(Summary.created_at)
        if limit:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars())
        if not rows:
            log.info("embed_cards.nothing", hint="所有卡都已有 card_vec")
            return 0
        dates = [r.created_at for r in rows if r.created_at]
        log.info("embed_cards.start", n=len(rows),
                 earliest=str(min(dates)) if dates else "?", latest=str(max(dates)) if dates else "?")
        # 先取成纯值(id+待 embed 文本):commit 后 ORM 对象会过期,别再碰它们。
        items = [
            (r.id, f"{r.headline}\n{r.tldr_rendered or ''}\n{r.insight_rendered or ''}".strip())
            for r in rows
        ]
        # 分批 embed + commit:进度可见(DB 实时涨)+ 被中断也不全丢。SELECT 已 autobegin 事务,
        # 直接 execute + commit(不能再 session.begin(),否则 "transaction already begun")。
        batch = 64
        done = 0
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            vecs = embedder.embed_passage([t for _id, t in chunk])  # 本地、零成本
            for (sid, _t), vec in zip(chunk, vecs):
                await session.execute(
                    update(Summary).where(Summary.id == sid)
                    .values(card_vec=vec, produced_by="pulsewire"))
            await session.commit()
            done += len(chunk)
            log.info("embed_cards.progress", done=done, total=len(items))
        log.info("embed_cards.done", n=done)
    return 0


def cmd_schedule(log, argv: list[str]) -> int:
    """生成 launchd 每日调度 plist + 包装脚本到 deploy/(不改系统状态,打印安装说明)。

    用法:pulsewire schedule [--hour=8] [--minute=30]
    """
    from pulsewire.schedule.launchd import generate

    usage = ("用法:pulsewire schedule [--hour=0..23] [--minute=0..59]\n"
             "生成 deploy/run_daily.sh + launchd plist(不改系统状态;"
             "覆盖已有且内容不同的文件前自动留 .bak-<时间戳> 备份)。")
    hour, minute = 8, 30
    for arg in argv:
        try:
            if arg.startswith("--hour="):
                hour = int(arg[len("--hour="):])
            elif arg.startswith("--minute="):
                minute = int(arg[len("--minute="):])
            else:
                # 不认识的参数(含 --help)绝不默默当默认值生成——打用法就走,别碰盘上文件
                print(usage)
                return 2
        except ValueError:
            print(usage)
            return 2
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.error("schedule.bad_time", hour=hour, minute=minute, hint="--hour=0..23 --minute=0..59")
        return 2
    info = generate(hour=hour, minute=minute)
    log.info("schedule.generated", plist=info["plist"], wrapper=info["wrapper"], uv=info["uv"],
             time=f"{hour:02d}:{minute:02d}")
    print("\n" + info["instructions"])
    return 0


def main() -> None:
    configure_logging()
    log = get_logger()
    command = sys.argv[1] if len(sys.argv) > 1 else "validate-config"
    rest = sys.argv[2:]

    if command not in COMMANDS:
        log.error("unknown.command", got=command, expected=list(COMMANDS))
        sys.exit(2)

    try:
        if command == "validate-config":
            code = cmd_validate_config(log)
        elif command == "healthcheck":
            code = asyncio.run(_healthcheck(log))
        elif command == "fetch":
            code = asyncio.run(_fetch(log))
        elif command == "dedup":
            code = asyncio.run(_dedup(log))
        elif command == "enrich":
            code = asyncio.run(_enrich(log, rest))
        elif command == "rank":
            code = asyncio.run(_rank(log, rest))
        elif command == "transcript":
            code = asyncio.run(_transcript(log, rest))
        elif command == "summarize":
            code = asyncio.run(_summarize(log, rest))
        elif command == "render":
            code = asyncio.run(_render(log, rest))
        elif command == "deliver":
            code = asyncio.run(_deliver(log, rest))
        elif command == "schedule":
            code = cmd_schedule(log, rest)
        elif command == "threads":
            code = asyncio.run(_threads(log, rest))
        elif command == "sentinel":
            code = asyncio.run(_sentinel(log, rest))
        elif command == "audit-sources":
            code = asyncio.run(_audit_sources(log, rest))
        elif command == "ask":
            code = asyncio.run(_ask(log, rest))
        elif command == "embed-cards":
            code = asyncio.run(_embed_cards(log, rest))
        else:  # run
            code = asyncio.run(_run(log, rest))
    except Exception as exc:  # 失败要冒泡 + 记录,不静默吞成空数据
        log.error("command.failed", command=command, error=str(exc), error_type=type(exc).__name__)
        sys.exit(1)

    sys.exit(code)


if __name__ == "__main__":
    main()
