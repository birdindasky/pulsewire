"""GitHub 开源热榜引擎:选品 → 写 rankings(GH 伪兴趣)→ 复用 summarize → 出榜 PNG。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pulsewire.config import PROJECT_ROOT, load_sources, source_label
from pulsewire.obs import get_logger
from pulsewire.store import (
    add_item_timeline,
    get_items_by_ids,
    get_latest_timeline_stars,
    get_sessionmaker,
    get_summaries,
    prune_rankings,
    upsert_ranking,
)
from pulsewire.summarize import run_summarize

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

# 固定伪兴趣:热榜不走语义排序,但复用 rankings/summaries 机器,用一个稳定 key 区隔
GH_INTEREST_KEY = "ghboard"
GH_INTEREST = "GitHub 开源热榜"


def _repo_key(url: str) -> str | None:
    """从 github url 取 owner/repo 作去重键。"""
    if not url:
        return None
    parts = url.split("github.com/", 1)
    if len(parts) != 2:
        return None
    segs = [s for s in parts[1].split("/") if s]
    if len(segs) < 2:
        return None
    return f"{segs[0]}/{segs[1]}".lower()


def _repo_display(url: str) -> str | None:
    """从 github url 取**原始大小写** owner/repo 作展示(区别于去重键 _repo_key 的小写)。"""
    if not url:
        return None
    parts = url.split("github.com/", 1)
    if len(parts) != 2:
        return None
    segs = [s for s in parts[1].split("/") if s]
    return f"{segs[0]}/{segs[1]}" if len(segs) >= 2 else None


def _star_velocity(stars: int, created_at_raw: str | None, now: datetime) -> float:
    """涨速代理 = 当前星数 / 仓库天龄(stars per day since creation)。

    新仓冲高星 → 高涨速排前;老巨仓总星虽高但被年龄稀释 → 低涨速沉底。只用现有数据
    (star + created_at),**零热身、不需历史快照**,新冒头的仓天然靠前——直接治
    "GitHub 榜全是年度大名"。缺 created_at(旧抓未回填的条目)→ 返回 0 自然沉底,
    绝不误捧未知龄的仓。天龄下限 1 天,防新仓除以极小值炸出虚高。
    注:真·近期涨速(item_timeline 跨天 delta,治"老仓突然翻红")留 v2 细化;
    本函数纯无副作用,可独立单测。
    """
    if not created_at_raw:
        return 0.0
    try:
        cdt = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if cdt.tzinfo is None:
        cdt = cdt.replace(tzinfo=timezone.utc)
    age_days = max((now - cdt).total_seconds() / 86400.0, 1.0)
    return stars / age_days


def _recent_velocity(
    stars: int,
    prev: tuple[int, datetime] | None,
    created_at_raw: str | None,
    now: datetime,
) -> float:
    """近期涨速(stars/day),GitHub 榜 v2 主排序。治"老仓突然翻红"。

    有上一份快照(prev=(上次stars, 上次时间))→ **真近期涨速** = (当前 − 上次) / 隔的天数:
    老仓总星虽高但被一辈子天龄稀释、近期猛涨却能靠这个冒头;不涨的(delta≈0)自然沉底
    (顺带抑制"凑数老仓")。隔天下限 1.0 天:同日多跑(--force)不炸虚高、漏跑几天按真间隔归一。
    无上一份快照(冷启动:新进候选池、还没攒到历史)→ 退回一辈子均速 `_star_velocity`
    (星/天龄,与近期涨速**同单位 stars/day,可同列排序**),行为与 v1 一致、攒到第二份快照自动转真涨速。
    delta 可负(掉星)→ 负涨速沉底,合理。纯函数、无副作用,可独立单测。
    """
    if prev is not None:
        prev_stars, prev_at = prev
        if prev_at.tzinfo is None:
            prev_at = prev_at.replace(tzinfo=timezone.utc)
        delta_days = max((now - prev_at).total_seconds() / 86400.0, 1.0)
        return (stars - prev_stars) / delta_days
    return _star_velocity(stars, created_at_raw, now)


# repo 名里的通用词:不算"显著主题",不拿来判同主题(否则 *-agent / *-ai 全被误并)。
_GH_NAME_STOP = frozenset({
    "agent", "agents", "agentic", "ai", "llm", "llms", "gpt", "app", "apps", "tool", "tools",
    "framework", "studio", "desktop", "mobile", "web", "api", "sdk", "cli", "lib", "kit",
    "awesome", "open", "chat", "bot", "bots", "model", "models", "code", "coding", "assistant",
    "the", "for", "and", "with", "your", "data", "deep", "self", "auto", "smart", "easy",
})


def _name_token_overlap(repo_key: str, words: set[str]) -> set[str]:
    """repo **全部**显著名 token 都出现在词集里 + **出品方(owner)也有佐证** → 判同项目。

    跨板**同项目**去重用:repo 名出现在其它板成稿标题词集里 = 同一个项目两处登。
    **要求 repo 名全部命中(子集),不是任意一个**——否则 'deepseek-reasonix' 会因新闻里
    提到公司名 'deepseek' 被误剔(reasonix 才是它的显著标识、新闻里没有)。
    **再加 owner 佐证防撞名**(2026-06-26 hermes 误剔):repo 名命中后,若 owner 有显著
    token 却一个都不在词集里 → 判为仅撞名(如 fathah/hermes-desktop 撞上 Nous 的 Hermes)
    不剔;owner 含项目名的(omnigent-ai / langchain-ai)照常去重;owner 无显著 token 时退回
    只按 repo 名(老行为)。偏向"宁可偶尔漏剔重复,也不误杀无辜同名 repo"。纯函数,可单测。
    """
    toks = _name_tokens(repo_key)
    if not toks or not (toks <= words):
        return set()
    owner = repo_key.split("/", 1)[0] if "/" in repo_key else ""
    owner_toks = _name_tokens(owner)
    if owner_toks and not (owner_toks & words):
        return set()  # owner 有显著名却无一在词集 → 仅撞名,不剔
    return toks


def _name_tokens(repo_key: str) -> set[str]:
    """repo 名(owner/repo 的 repo 部分)的显著 token:去通用词、长度≥3。

    用于热榜主题多样性去重:hermes-agent / hermes-desktop / hermes-studio 共享 'hermes'
    → 判同主题,只留涨速最高那个。embedding 余弦在 repo 短名上分不开(实测同生态对仅 ~0.4,
    与无关对缠在一起),故按名 token 而非语义。无显著 token 的 repo(全通用词)永不被并。
    """
    name = repo_key.split("/", 1)[-1] if "/" in repo_key else repo_key
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3 and t not in _GH_NAME_STOP}


async def _ranked_candidates(
    session, settings: Settings, *, run_id: str | None = None
) -> list[tuple[str, int, str | None, str]]:
    """候选池:取带 stars 的 AI repo → owner/repo 去重 + 跨板块去重 → **按近期涨速排序**。
    返回排好序的 candidates [(item_id, stars, created_at, repo_key)](未做名 token 去重、未截 top-N)。

    排序键 = `_recent_velocity`:有上一份快照→真·近期涨速(Δstars/Δ天,治"老仓翻红");
    冷启动(还没历史)→退"星/天龄"一辈子均速(同单位 stars/day,治"全是年度大名")。同速按总
    stars 兜底。候选池先按总 stars 取前 200(够大,实际 AI repo 远不到 200),再按近期涨速重排。
    """
    from sqlalchemy import bindparam
    from sqlalchemy import text as sa_text

    cfg = settings.rank
    since = datetime.now(timezone.utc) - timedelta(days=cfg.github_board_recency_days)
    # 只认当前注册表里 enabled 的源:孤儿源(已从 sources.yaml 移除但 items 表还残留旧条目)
    # 的陈旧/假星(如 ECC 假 21.5 万星)不得上榜(2026-06-15 一⑥)。
    active_ids = sorted({s.id for s in load_sources() if s.enabled})
    # 候选池(留去重余量);只要 facts.github.stars 存在的 repo
    # 限定 AI 相关 github 源(ai/llm/agent),排除通用 trending 源里的 evergreen 巨头
    # (如 free-programming-books),让热榜真是"AI 开源",而非"史上最多星仓库"。
    # 新鲜度门:必须有 published_at 且在窗口内 —— 原 `OR published_at IS NULL` 会让孤儿源
    # NULL 时间的陈旧星绕过窗口顶第一(ECC),改成硬要求近期推送过(2026-06-15 一⑥)。
    stmt = sa_text(
        """
        SELECT item_id, url, (facts->'github'->>'stars')::bigint AS stars,
               facts->'github'->>'created_at' AS created_at
        FROM items
        WHERE facts->'github'->>'stars' IS NOT NULL
          AND published_at IS NOT NULL
          AND published_at >= :since
          AND source IN :active_ids
          AND (source ILIKE '%ai%' OR source ILIKE '%llm%' OR source ILIKE '%agent%')
        ORDER BY stars DESC
        LIMIT 200
        """
    ).bindparams(bindparam("active_ids", expanding=True))
    rows = (await session.execute(stmt, {"since": since, "active_ids": active_ids})).all()
    excluded = {r.strip().lower() for r in cfg.github_board_exclude if r.strip()}  # 排除名单(owner/repo 同口径)
    # 跨板块去重:已进 AI/bio/geo 日报(其它 interest_key 的 rankings)的 repo 不在 GH 榜重复刷
    # (2026-06-15 二⑤;rank 在 github_board 之前跑,这里能看到当日各领域已选)。
    # 限定本次 run_id:只看本次跑的各领域选品,防已下线领域的陈旧 rankings 永久压制 repo
    # (prune_rankings 只在该领域自己跑时按 interest_key 清,停用领域的旧行会残留)。run_id 缺省则不限定。
    cross_sql = (
        "SELECT DISTINCT i.url FROM rankings r JOIN items i ON i.item_id = r.item_id "
        "WHERE r.interest_key <> :gh AND i.url ILIKE '%github.com/%' "
        # 排除事件引擎影子榜(`<key>__shadow`):影子 A/B 跑时绝不让影子选稿污染 live GitHub 榜
        # 跨板块去重(闭 codex 复审 F1;叠加影子独立 run_id 双保险)。无 live key 以 'shadow' 结尾。
        "AND r.interest_key NOT LIKE '%shadow'"
    )
    cross_params: dict = {"gh": GH_INTEREST_KEY}
    if run_id is not None:
        cross_sql += " AND r.run_id = :run_id"
        cross_params["run_id"] = run_id
    cross_rows = (await session.execute(sa_text(cross_sql), cross_params)).all()
    cross_board = {k for (u,) in cross_rows if (k := _repo_key(u))}
    # 跨板**同项目**去重(治"新闻板讲某 repo + 热榜又列同 repo",如 Omnigent:AI 板成稿
    # 『Databricks 开源 Omnigent』vs 热榜 omnigent-ai/omnigent)。URL 法抓不到——AI 板那条源是
    # 播客(latent-space)、原始 item.title 是播客名不含 omnigent,只成稿 headline 含。故拿其它板
    # **成稿标题/tldr** 的词集,与 repo 显著名 token(_name_tokens 已去通用词)求交集。
    # summarize 在 github_board 之前跑(STAGES 顺序),成稿此刻已就绪。
    summ_sql = (
        "SELECT headline, tldr_rendered FROM summaries "
        "WHERE interest_key <> :gh AND interest_key NOT LIKE '%shadow'"
    )
    summ_params: dict = {"gh": GH_INTEREST_KEY}
    if run_id is not None:
        summ_sql += " AND run_id = :run_id"
        summ_params["run_id"] = run_id
    summ_rows = (await session.execute(sa_text(summ_sql), summ_params)).all()
    cross_text = " ".join(f"{h or ''} {t or ''}" for h, t in summ_rows).lower()
    cross_words = {w for w in re.split(r"[^a-z0-9]+", cross_text) if len(w) >= 3}
    seen: set[str] = set()
    candidates: list[tuple[str, int, str | None, str]] = []  # (item_id, stars, created_at, repo_key)
    for item_id, url, stars, created_at in rows:
        key = _repo_key(url)
        if key is None or key in seen or key in excluded or key in cross_board:
            continue
        cross_hit = _name_token_overlap(key, cross_words)  # repo 显著名出现在其它板成稿 → 同项目
        if cross_hit:
            log.info("github_board.cross_board_skip", repo=key, hit=sorted(cross_hit))
            continue
        seen.add(key)
        candidates.append((item_id, int(stars or 0), created_at, key))
    # 上一份 star 快照(本跑写入前查 → 即上一跑的);算真·近期涨速 delta(治老仓翻红)
    prev_snaps = await get_latest_timeline_stars(session, [c[0] for c in candidates])
    # 按**近期涨速**重排:有历史→真涨速(老仓近期猛涨能冒头),冷启动→退"星/天龄"
    # (同单位 stars/day,可同列排序);同速按总 stars 兜底
    now = datetime.now(timezone.utc)
    candidates.sort(
        key=lambda c: (_recent_velocity(c[1], prev_snaps.get(c[0]), c[2], now), c[1]),
        reverse=True,
    )
    return candidates


def _dedup_and_top(
    candidates: list[tuple[str, int, str | None, str]], limit: int
) -> list[tuple[str, int]]:
    """名 token 主题去重 + 取 top-N → kept [(item_id, stars)]。

    涨速从高到低贪心,跳过与已留 repo 共享显著名 token 的(同生态/同名变体刷屏,如
    hermes-agent/-desktop/-studio 一拆三 → 只留涨速最高那个)。无显著 token 的不并。
    """
    kept: list[tuple[str, int]] = []
    kept_tokens: set[str] = set()
    for item_id, stars, _created_at, key in candidates:
        toks = _name_tokens(key)
        if toks and (toks & kept_tokens):
            continue
        kept.append((item_id, stars))
        kept_tokens |= toks
        if len(kept) >= limit:
            break
    return kept


async def _select_trending(session, settings: Settings, *, run_id: str | None = None):
    """选 top-N 上榜 [(item_id, stars)]:候选池(近期涨速排序)→ 名 token 去重 + 截断。

    契约不变(返回 kept 列表),供单测;run_github_board 另调 `_ranked_candidates` 取全量候选记快照。
    """
    candidates = await _ranked_candidates(session, settings, run_id=run_id)
    return _dedup_and_top(candidates, settings.rank.github_board_limit)


async def run_github_board(
    settings: Settings, *, run_id: str | None = None, trigger_type: str | None = None,
    sessionmaker=None,
) -> dict:
    """跑一次开源热榜:选品 → 写 GH rankings + star 快照 → summarize → 出榜 PNG。返回汇总 dict。"""
    cfg = settings.rank
    if cfg.github_board_limit <= 0:
        return {"interest_key": GH_INTEREST_KEY, "repos": 0, "note": "热榜已关闭(github_board_limit=0)"}

    sm = sessionmaker or get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            candidates = await _ranked_candidates(session, settings, run_id=run_id)
            picked = _dedup_and_top(candidates, cfg.github_board_limit)
            if not picked:
                return {"interest_key": GH_INTEREST_KEY, "repos": 0, "note": "无带 stars 的 repo 可上榜"}
            # 写 rankings(rank 按近期涨速降序;分数用 stars 仅供排序,不参与 LLM)
            await prune_rankings(session, interest_key=GH_INTEREST_KEY,
                                 keep_item_ids=[iid for iid, _ in picked])
            picked_rank = {iid: r for r, (iid, _s) in enumerate(picked, start=1)}
            for rank, (item_id, stars) in enumerate(picked, start=1):
                await upsert_ranking(
                    session, interest_key=GH_INTEREST_KEY, interest=GH_INTEREST, tags=["github"],
                    item_id=item_id, cluster_id=None,
                    recall_score=0.0, rule_score=float(stars), rerank_score=None,
                    final_score=float(stars), rank=rank, provider="github_stars", run_id=run_id,
                )
            # star 快照(纯追加):给**整个候选池**记当跑 stars(上榜的带 rank、没上榜的 rank=None),
            # 攒跨天历史算近期涨速。扩到候选池(非只 top-N)→ 没上榜的老仓也有历史,日后翻红能算 delta。
            for item_id, stars, _ca, _k in candidates:
                await add_item_timeline(
                    session, item_id=item_id, run_id=run_id, trigger_type=trigger_type,
                    rank=picked_rank.get(item_id), stars=int(stars),
                )

    # 复用 summarize:给每个 repo 产 headline/tldr/insight(stars 走 {Fn} 占位,数字回源)
    summ = await run_summarize(settings, interest_key=GH_INTEREST_KEY, run_id=run_id,
                               sessionmaker=sm)

    # 拼榜数据 + 出速读榜 PNG
    async with sm() as session:
        from pulsewire.store import get_rankings

        rankings = await get_rankings(session, interest_key=GH_INTEREST_KEY)
        summaries = {s.item_id: s for s in await get_summaries(session, interest_key=GH_INTEREST_KEY)}
        meta = {it.item_id: it for it in await get_items_by_ids(session, [r.item_id for r in rankings])}

    items: list[dict] = []
    for r in rankings:
        s, m = summaries.get(r.item_id), meta.get(r.item_id)
        if s is None or m is None:
            continue
        stars = ((m.facts or {}).get("github") or {}).get("stars")
        items.append({
            "id": m.item_id, "headline": s.headline, "tldr": s.tldr_rendered,
            "insight": s.insight_rendered, "source": source_label(m.source), "url": m.url,
            "repo": _repo_display(m.url),  # 真实 owner/repo,让读者照着上 GitHub 找到本项目
            # GitHub 热榜核心断言=star 数(硬事实,已走数字回源对账),不标"待核实";
            # 只豁免展示层,不动通用 verify 闸门(真领域日报仍照常标)(2026-06-15 一④)。
            "stars": stars, "needs_review": False,
        })

    from pulsewire.render.engine import render_detail_png

    tz = ZoneInfo(settings.app.timezone)
    now = datetime.now(tz)
    out_dir = PROJECT_ROOT / settings.render.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"digest_{GH_INTEREST_KEY}.png")
    # 详读长图(用户 2026-06-30 选定:GitHub 板从中详换最全)——每条 headline+repo+完整 insight。
    await render_detail_png(
        settings, items=items, category=GH_INTEREST, out_path=out_path,
        date_display=now.strftime("%Y · %m · %d"),
        footer_info=f"pulsewire · GitHub 开源热榜 · {now:%Y.%m.%d}",
    )

    log.info("github_board.done", repos=len(items), path=out_path,
             needs_review=summ.get("needs_review", 0))
    return {"interest_key": GH_INTEREST_KEY, "repos": len(items), "path": out_path,
            "needs_review": summ.get("needs_review", 0), "items": items}
