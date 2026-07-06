"""events.engine —— run_event_rank:全局事件池选稿(见 docs/DESIGN.md §1)。

读近窗**簇**(非原始 item,成本可控)→ A+B 聚类成事件(events.cluster,Phase 1a 验过)→ 打分
(events.score:热度主轴+源族折叠+两道硬门)→ 每事件按相关度分一个板块(events.allocate)+ 复刻限额
→ **每域写同一张 rankings 表**(下游 transcript/summarize/render/deliver 零改动)。

仅 rank.engine=events 调;legacy 完全不碰。影子模式(shadow=True)写 `<interest_key>__shadow` key +
传 shadow run_id,绝不污染 live 榜(闭 codex F1)。LLM 硬闸 max_subject_clusters/max_judge_pairs_per_run
(闭 F3)。返回 per-domain status,由 _stage_rank 施加主/次域失败语义(闭 F2)。
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import numpy as np
from sqlalchemy import case, func, select

from pulsewire.events import score as S
from pulsewire.events.allocate import apply_event_quotas
from pulsewire.events.cleantext import body_chars_beyond_title, clean_text
from pulsewire.events.cluster import (
    CONTENT_TRUNCATE,
    judge_same_event,
    judge_same_event_verdict,
    same_event_item_hash,
    same_event_prompt_hash,
    surface_candidates,
)
from pulsewire.events.board_classifier import (
    classify_mixed_events,
    is_bypass_cluster,
    make_board_classifier,
)
from pulsewire.events.clip_memory import filter_already_clipped
from pulsewire.events.judge_cache import make_row
from pulsewire.events.magnitude_judge import filter_water, make_water_judge
from pulsewire.events.topic_judge import filter_off_topic, make_topic_judge
from pulsewire.events.worthiness_judge import filter_unworthy, make_worthiness_judge
from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.threads.subject import extract_subject

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()
_SUBJ_CONC = 16  # 主体抽取并发(2026-06-22 抬 6→10 省39%;2026-07-02 提速批再抬 10→16)
_JUDGE_CONC = 16  # 同事件判官并发(同上;2026-06-19 原串行=rank 25min 瓶颈)
# 抬并发安全性(2026-06-22 影子 A/B 锁定,deploy/perf/rank_ab.py):
# rank 非确定性(flash 偶发返空/降级答案→主体抖→选稿微漂),但实测此漂移与并发无关——
# 基线 conc=6 跑 8 遍 ai 条数即在 3-5 晃、临界条抛硬币(4/8),真铁稳条 8 跑 100% 保留;
# conc=10 跑 4 遍掉铁稳率 0/4、失败率 0.45%<基线 0.66%。即并发只改「调用多快回」不伤内容。
# 真风险=并发过高把判官压到返空/限流(失败率飙)→ A/B 门盯失败率<2%;实测 conc≤20 仍干净。
# 2026-07-02 按该纲抬 10→16(判官已统一 pro,较 flash 更扛),配套本日 rank_ab 补影子 A/B 样本;
# 注意 asyncio 默认线程池上限 = min(32, cpu+4),conc 实际吃到的并行度以此为顶。


def _norm(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a))
    return a / n if n else a


def member_clean_text(content: str | None, facts: dict | None) -> str:
    """成员"干净正文":clean_text(content) 与 facts.fulltext.text(清洗后)取更有料者。

    全文兜底(2026-07 P1,读侧桥):summary-only 源的 content 是一行摘要甚至空壳,
    enrich 按源抓的全文在 facts.fulltext——比 content 更有料就用它,让这些源过得了
    空壳护栏、当得了代表/判官素材。**读侧兜底而非回写 content**:content 与去重指纹
    由 feed 原始值派生、隔天重抓还会被 feed 值覆盖,回写=指纹与正文脱钩 + 成果被冲掉;
    读侧桥零副作用。facts 形态异常(None/list/缺键)一律安静回退 content。
    """
    clean = clean_text(content)
    ft = facts.get("fulltext") if isinstance(facts, dict) else None
    ft_text = ft.get("text") if isinstance(ft, dict) else None
    if ft_text and isinstance(ft_text, str):
        ft_clean = clean_text(ft_text) or ft_text
        if len(ft_clean) > len(clean):
            return ft_clean
    return clean


def _event_has_real_body(ev_members: list[dict], min_body_chars: int) -> bool:
    """事件是否有**任一**成员抓到了真文章正文(清洗后正文剔掉标题词 ≥ min_body_chars)。

    全为 False = 空壳事件(只剩标题/跳转链,如 google-news 跳转卡),不可靠 → 排除出榜。
    min_body_chars<=0 视为关闭护栏(恒 True)。量法见 cleantext.body_chars_beyond_title。
    """
    if min_body_chars <= 0:
        return True
    return any(
        body_chars_beyond_title(m["clean"], m["title"]) >= min_body_chars
        for m in ev_members
    )


class _UF:
    """并查集:把 A 候选且 B 判同的簇合成事件。"""

    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


async def _judge_and_merge(pairs, judge_one, uf, conc):
    """并发判官 + 按 ``pairs`` 原顺序串行合并(union 调用顺序 == pairs 顺序)。

    ``judge_one(cx, cy) -> bool`` 须无副作用、可并发;任一异常视为 False(保守不合,
    绝不误合)。并查集的分区与根选择只取决于 union 的调用顺序——此处顺序与「串行逐对判+合」
    完全相同,故合并结果与串行版逐字节一致,仅去掉「一对一对排队等 LLM」的浪费。
    返回合并次数;``uf`` 原地更新。判官跑由 ``conc`` 信号量限流。
    """
    sem = asyncio.Semaphore(conc)

    async def _one(cx, cy):
        async with sem:
            try:
                return await judge_one(cx, cy)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,绝不吞成保守票(2026-07-02 E1)
            except Exception:
                return False  # 判官失败=保守不合(绝不误合)

    verdicts = await asyncio.gather(*(_one(cx, cy) for cx, cy in pairs))
    merges = 0
    for (cx, cy), same in zip(pairs, verdicts):
        if same:
            uf.union(cx, cy)
            merges += 1
    return merges


async def run_event_rank(
    settings: Settings, *, domains, run_id: str | None = None,
    sessionmaker=None, embedder=None, shadow: bool = False,
) -> dict:
    """全局事件池选稿,写各域 rankings。返回 {"events": N, "domains": {key: kept}, ...}。"""
    from pulsewire.config import load_sources
    from pulsewire.dedup import get_embedder
    from pulsewire.rank import interest_key as make_interest_key
    from pulsewire.store import get_sessionmaker, prune_rankings, upsert_ranking
    from pulsewire.store.tables import Cluster, Item

    cfg = settings.rank
    ep = cfg.event_pool
    now = datetime.now(timezone.utc)
    embedder = embedder or get_embedder(settings)
    sources = {s.id: s for s in load_sources()}
    sm = sessionmaker or get_sessionmaker()
    active = [d for d in domains if getattr(d, "enabled", True)]

    # 判决缓存(S1,默认关):本轮所有判官(same_event/magnitude/worthiness/topic/board)新判的裁决
    # 汇进这一张共享单,选稿完一次性写回库,下轮同内容+同口径直接读、不调 LLM(治 f20 逐日重判白烧钱)。
    # None = 缓存关(judgment_cache_enabled=false),各判官零行为变化(不预载、不记录)。
    new_verdicts: list | None = [] if ep.judgment_cache_enabled else None

    # 各域兴趣向量(相关性闸 + argmax 分配用)
    dom_vec = {}
    for d in active:
        qt = d.interest if not d.tags else f"{d.interest} {' '.join(d.tags)}"
        dom_vec[d.key] = _norm(embedder.embed([qt])[0])

    recall_since = now - timedelta(hours=cfg.recall_recency_hours)
    fresh_recent = now - timedelta(hours=ep.velocity_window_recent_hours)
    fresh_base = now - timedelta(hours=ep.velocity_window_base_hours)

    async with sm() as session:
        # 1) 近窗簇:**按源领域均衡配额**抽主体预算(每域用自己源清单各取 top-(cap/域数) 簇)。
        #    防全局按 source_count 排偏袒多源的 geo、把 bio(新闻天生少多源)饿死(影子跑实测 bio 仅 4 条)。
        #    簇跨域时被多域选中无妨——set 去重,实际板块由成员源多数域定(step 6)。
        #    ⚠️ board_classifier 开启时(见 docs/DESIGN.md §2.6):综合源(s.mixed)从专源池摘出
        #    (不挤专源 per_dom_cap、不乱投),改走下面独立 mixed 池;关时退回当普通专源(零行为变化)。
        bc_on = ep.board_classifier_enabled
        per_dom_cap = max(1, ep.max_subject_clusters // max(len(active), 1))
        selected: set[str] = set()
        for d in active:
            # board_only 源(GitHub 项目榜)不进新闻选稿——只供 github_board(它独立从 items 取,不经此)。
            # ⚠️ s.enabled 过滤(2026-06-28 修):停用源(enabled=False)只挡未来 fetch,但停用前已入库的旧 items
            #    仍在 recall 窗口(默认 30 天)内;选稿不看 enabled → 停用源旧货照样被选(今早 github-trending
            #    的 5 个旧 repo 占满 AI 板=此 bug)。加 s.enabled 让停用立即对选稿生效。
            dsrc = [s.id for s in sources.values() if s.enabled and s.domain == d.key
                    and not s.board_only and not (bc_on and s.mixed)]
            if not dsrc:
                continue
            rows = (await session.execute(
                select(Item.cluster_id)
                .join(Cluster, Cluster.cluster_id == Item.cluster_id)
                .where(Item.fetched_at >= recall_since, Item.cluster_id.isnot(None),
                       Item.source.in_(dsrc))
                .group_by(Item.cluster_id)
                .order_by(func.max(Cluster.source_count).desc(), func.max(Item.fetched_at).desc())
                .limit(per_dom_cap)
            )).all()
            selected.update(cid for (cid,) in rows)
        # mixed 独立候选池(仅 board_classifier 开 + mixed_cap>0):综合源簇走旁路,不挤专源 per_dom_cap。
        #   ⚠️ 只取**纯 mixed cluster**(无专源 item 的旁路簇)——不能像专源池只按 source_count desc:
        #   纯 mixed 簇天然单源(实测近窗 13k 簇 source_count 中位=1),会被"综合源+专源混报的高 source_count
        #   簇"(那些含专源锚、本就走主路)占满名额 → mixed 池一个真旁路簇都取不到、board_classifier 形同虚设。
        #   故 SQL 层 having `sum(case 专源)==0` 滤掉含专源的簇,取的全是真旁路簇(纯 mixed 内多源报的优先)。
        if bc_on and ep.mixed_cap > 0:
            msrc = [s.id for s in sources.values() if s.enabled and s.mixed and not s.board_only]
            pro_ids = [s.id for s in sources.values()
                       if s.enabled and not s.board_only and not s.mixed]
            if msrc:
                mixed_subq = (
                    select(Item.cluster_id)
                    .where(Item.source.in_(msrc), Item.fetched_at >= recall_since,
                           Item.cluster_id.isnot(None))
                    .distinct()
                )
                rows = (await session.execute(
                    select(Item.cluster_id)
                    .join(Cluster, Cluster.cluster_id == Item.cluster_id)
                    .where(Item.cluster_id.in_(mixed_subq), Item.fetched_at >= recall_since)
                    .group_by(Item.cluster_id)
                    .having(func.sum(case((Item.source.in_(pro_ids), 1), else_=0)) == 0)
                    .order_by(func.max(Cluster.source_count).desc(), func.max(Item.fetched_at).desc())
                    .limit(ep.mixed_cap)
                )).all()
                selected.update(cid for (cid,) in rows)
        cluster_ids = list(selected)
        if not cluster_ids:
            return {"events": 0, "domains": {d.key: 0 for d in active}, "provider": "events"}

        # 2) 成员(item 级:源/标题/正文/时间)。排除停用源的 item(2026-06-28 修,第二道):停用源旧 item
        #    即便搭车进了某混簇,也不让它当代表/投票——否则 github-trending 旧 repo 仍会从「repo+真新闻」
        #    混簇里被选成代表,summarize 拿 repo 名编中文新闻标题=垃圾(今早 cheahjs/free-llm-api-resources
        #    漏网即此)。step1 挡候选池、这里挡 member,双保险彻底。全 member 皆停用的簇→后面 rep 自动跳过。
        #    + board_only 项目榜源(2026-06-30 修):GitHub 项目源只供 github_board,绝不能当新闻板事件的
        #    成员/代表——否则话题沾 AI 的 repo(如 MemOS,来源 GitHub Search)会被选成代表(rep=正文最长成员),
        #    再被 board_classifier 凭"内容像 AI"拽进 AI 新闻板(MemOS 乱入即此;step1/投票早排了 board_only,
        #    唯独 member/rep 没排=漏)。github_board 独立从 items 取,不经此路,排除不影响它。
        excluded_ids = [s.id for s in sources.values() if (not s.enabled) or s.board_only]
        member_where = [Item.cluster_id.in_(cluster_ids), Item.fetched_at >= recall_since]
        if excluded_ids:
            member_where.append(Item.source.notin_(excluded_ids))
        mrows = (await session.execute(
            select(Item.cluster_id, Item.item_id, Item.source, Item.title, Item.content,
                   Item.published_at, Item.category,
                   Item.facts["fulltext"])  # 投影只取全文桥所需子键(codex③:整块 facts 无界,github enrich 等大血块别进内存)
            .where(*member_where)
        )).all()

    members: dict[str, list] = {cid: [] for cid in cluster_ids}
    for cid, iid, src, title, content, pub, cat, ft in mrows:
        facts = {"fulltext": ft} if ft is not None else None  # 投影后拼回 member_clean_text 的入参形状
        members.setdefault(cid, []).append(
            {"item_id": iid, "cluster_id": cid, "source": src,
             "title": clean_text(title) or title,  # 部分源标题含生 HTML(<a href>),清洗;纯链接标题兜底留原
             "clean": member_clean_text(content, facts), "published_at": pub, "category": cat}
        )
    # 每簇代表 = 干净正文最长的成员(正文最全);兜底用首条
    rep: dict[str, dict] = {}
    for cid in cluster_ids:
        ms = members.get(cid) or []
        if not ms:
            continue
        rep[cid] = max(ms, key=lambda m: len(m["clean"]))
    cluster_ids = [c for c in cluster_ids if c in rep]

    # 2.5) 分「主路 / 旁路」(board_classifier 开启时;见 docs/DESIGN.md §2.6,闭 codex Fix1+Fix2):
    #   旁路 cluster = **有 mixed member 且无任何专源 member**(无专源锚、源标签不可信)→ 不进 union-find、
    #   单簇成事件打 is_mixed、交 board_classifier 读内容判板。判据精确(必须"含 mixed",闭 Fix2:全 board_only
    #   成员的簇 dom_ct 本就空、走主路自然丢,不误纳进判官)。主路 = 其余(含 ≥1 专源 member,照旧 union-find)。
    #   关时 bypass_cids 空、pro_cids=全部 → 完全等于现状(零回归)。
    bypass_cids: set[str] = set()
    if bc_on:
        for cid in cluster_ids:
            srcs_in = [m["source"] for m in members.get(cid, [])]
            if is_bypass_cluster(srcs_in, sources):
                bypass_cids.add(cid)
    pro_cids = [c for c in cluster_ids if c not in bypass_cids]

    # 3) 抽事件主体(并发,硬闸已在簇数;失败降级用标题)
    sem = asyncio.Semaphore(_SUBJ_CONC)
    subj: dict[str, str] = {}

    async def _do_subj(cid):
        r = rep[cid]
        async with sem:
            try:
                subj[cid] = await asyncio.to_thread(
                    extract_subject, r["title"], summary=r["clean"][:CONTENT_TRUNCATE],
                    domain=None, settings=settings)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,别静默把每条主体降级成标题截断(2026-07-02 E1)
            except Exception:
                subj[cid] = (r["title"] or "")[:60]

    await asyncio.gather(*(_do_subj(c) for c in cluster_ids))

    # 4) 主体短语向量
    svecs_list = await asyncio.to_thread(embedder.embed, [subj[c] for c in cluster_ids])
    svec = {c: _norm(v) for c, v in zip(cluster_ids, svecs_list)}

    # 5) A 候选 → B 判官(硬闸 max_judge_pairs_per_run;超限停合并保守留开)→ 并查集合并
    #    判官并发(信号量限流,≤flash 安全档),但合并仍按原枚举顺序串行——
    #    并查集分区与根选择只取决于 union 的调用顺序;顺序不变 → 结果与串行版逐字节一致,
    #    仅去掉「一对一对排队等 LLM」的浪费(2026-06-19:rank 25min 瓶颈即此处原串行)。
    # ⚠️ union-find 只对**主路**簇(pro_cids):旁路 mixed 簇不进(闭 codex Fix1——进了会撑大 pairs、
    #    提前打爆 max_judge_pairs_per_run 截断专源合并判官、伤专源质量)。关时 pro_cids=全部 → 同现状。
    cand = surface_candidates(pro_cids, subj, svec)
    uf = _UF(pro_cids)
    capped = False

    # 先按原顺序枚举候选对,硬闸截断(截断点、被判对集合都与原串行版一致)
    pairs: list[tuple[str, str]] = []
    for cx in pro_cids:
        for cy in cand.get(cx, []):
            if len(pairs) >= ep.max_judge_pairs_per_run:
                capped = True
                break
            pairs.append((cx, cy))
        if capped:
            break
    judged = len(pairs)

    def _pair_ab(cx, cy):
        a = {"subject": subj[cx], "headline": rep[cx]["title"], "snippet": rep[cx]["clean"]}
        b = {"subject": subj[cy], "headline": rep[cy]["title"], "snippet": rep[cy]["clean"]}
        return a, b

    # 判决缓存(S1):同事件判官(event_judge,占判官调用 ~47%,是省钱大头)按**对称**内容哈希缓存。
    # 预载本轮全部候选对的已有裁决 → 命中直接读、不调 LLM。默认关(judgment_cache_enabled)。
    # 只走合并主路(800/run 的大头);限额去重的少量同事件判(_same_event)不缓存,不值当。
    same_cache: dict | None = None
    same_ph: str | None = None
    if ep.judgment_cache_enabled and pairs:
        from pulsewire.store import get_cached_judgments
        same_ph = same_event_prompt_hash(settings)
        _pair_hashes = list({same_event_item_hash(*_pair_ab(cx, cy)) for cx, cy in pairs})
        async with sm() as _s:
            same_cache = await get_cached_judgments(
                _s, judge_name="same_event", prompt_hash=same_ph, item_hashes=_pair_hashes)

    async def _judge_one(cx, cy):
        a, b = _pair_ab(cx, cy)
        if same_cache is None:
            return await asyncio.to_thread(judge_same_event, a, b, settings=settings)
        # _judge_one 由 asyncio.gather 在事件循环单线程协作调度:cache 读 / new_verdicts 追加均在
        # await 之间同步执行,协程间不重入 → 无需锁(与判官层 map_judge 的线程并发不同)。
        ih = same_event_item_hash(a, b)
        hit = same_cache.get(ih)
        if hit is not None:
            return hit.get("same") is True   # 缓存命中:逐字复用,不调 LLM
        v = await asyncio.to_thread(judge_same_event_verdict, a, b, settings=settings)
        if v is not None and new_verdicts is not None:  # 只缓存干净裁决;脏/空(None)不记
            new_verdicts.append(make_row(ih, "same_event", same_ph, {"same": v}))
        return bool(v)  # None(脏)→ False:保守不合(与原 judge_same_event 同向)

    merges = await _judge_and_merge(pairs, _judge_one, uf, _JUDGE_CONC)

    if capped:
        log.warning("events.judge.capped", judged=judged, cap=ep.max_judge_pairs_per_run,
                    note="超判官硬闸,剩余簇各自成事件(保守)")

    # 6) 成事件 + 打分
    #    主路:union-find 分组;旁路(board_classifier):每簇**单独**成事件(不进 uf,闭 codex Fix1)。
    groups: dict[str, list[str]] = {}
    for cid in pro_cids:
        groups.setdefault(uf.find(cid), []).append(cid)
    # (cids, is_bypass):主路组标 False、旁路单簇标 True;关时 bypass_cids 空 → 只有主路组(同现状)。
    event_groups: list[tuple[list[str], bool]] = [(cids, False) for cids in groups.values()]
    event_groups += [([cid], True) for cid in cluster_ids if cid in bypass_cids]

    events = []
    n_shell_dropped = 0
    for cids, is_bypass in event_groups:
        ev_members = [m for c in cids for m in members.get(c, [])]
        if not ev_members:
            continue
        # 空壳源护栏:若**全部成员都没抓到真文章正文**(清洗后正文剔掉标题词后 < min_body_chars,基本只剩
        # 标题/跳转链,如 google-news 跳转卡)→ 真文章从没被抓到,排除出榜(下游只拿标题自由发挥写错数字)。
        if not _event_has_real_body(ev_members, ep.min_body_chars):
            n_shell_dropped += 1
            continue
        # 事件代表 = **最新且有料**的成员(治影子跑暴露的"代表是旧卡"伤够新:原取正文最长可能选到旧文)。
        # 优先正文≥80 字符里发布最新者;无有料则全员取最新;published_at 缺省视为最旧。
        _oldest = datetime.min.replace(tzinfo=timezone.utc)
        substantive = [m for m in ev_members if len(m["clean"]) >= 80]
        rep_m = max(substantive or ev_members, key=lambda m: m["published_at"] or _oldest)
        rep_cid = rep_m["cluster_id"]
        sw = [(m["source"], (sources[m["source"]].weight if m["source"] in sources else 1.0))
              for m in ev_members]
        wsrc = S.weighted_distinct_sources(sw)
        fam_recent = {S.source_family(m["source"]) for m in ev_members
                      if m["published_at"] and m["published_at"] >= fresh_recent}
        fam_base = {S.source_family(m["source"]) for m in ev_members
                    if m["published_at"] and m["published_at"] >= fresh_base}
        vel = S.velocity(len(fam_recent), max(len(fam_base), 1), v_max=ep.velocity_max)
        is_mag = S.is_magnitude_entity(f"{rep_m['title']} {subj[rep_cid]}", ep.magnitude_whitelist)
        heat = S.heat_score(wsrc, vel,
                            magnitude_bonus=ep.magnitude_floor_bonus if is_mag else 0.0)
        pubs = [m["published_at"] for m in ev_members if m["published_at"]]
        peak_at = max(pubs) if pubs else None
        ev_vec = _norm(np.mean([svec[c] for c in cids], axis=0))
        relevance = {d.key: float(ev_vec @ dom_vec[d.key]) for d in active}
        # 板块分配主信号=成员源的多数领域(可靠;sources.yaml 已按领域策源)。语义相关度太弱不堪当主信号
        # (影子跑实测 max≈0.4/中位 0.05),降为诊断/兜底——这正是避免重蹈"按兴趣词选稿"覆辙。
        # board_only 源(GitHub 项目榜)不参与分板投票——否则跨域簇里它仍会贡献 domain 票,
        # 把已知偏差打进投票(2026-06-27 codex 第二审逮到:只挡候选不挡投票=不彻底)。
        # 投票:排除 board_only;board_classifier 开时再排除 mixed(主路簇里搭车的 mixed item 不投票)。
        dom_ct = Counter(sources[m["source"]].domain for m in ev_members
                         if m["source"] in sources and sources[m["source"]].domain
                         and not sources[m["source"]].board_only
                         and not (bc_on and sources[m["source"]].mixed))
        source_domain = dom_ct.most_common(1)[0][0] if dom_ct else None
        ev = {
            "relevance": relevance, "source_domain": source_domain, "heat_score": heat, "is_magnitude": is_mag,
            "category": rep_m.get("category"), "representative_source": rep_m["source"],
            "peak_at": peak_at, "rep_at": rep_m["published_at"], "rep_item_id": rep_m["item_id"],
            "rep_cluster_id": rep_cid,
            "headline": rep_m["title"], "n_sources": round(wsrc, 2), "velocity": round(vel, 3),
            # 选稿同事件去重兜底用(主体短语向量 + 判官);治全局聚类漏的同故事(用户实测:霍尔木兹/SpaceX 各出两张)
            "subject_vec": ev_vec, "subject": subj[rep_cid], "snippet": rep_m["clean"],
            # 已剪记忆闸(clip_memory)对账用:事件的全部成员簇 id(命中在追线挂线痕=此事已剪过)
            "member_cluster_ids": list(cids),
        }
        if is_bypass:
            # 旁路 mixed 事件:无专源锚,source_domain 留空,交 board_classifier(step 6.5)读内容判板。
            ev["source_domain"] = None
            ev["is_mixed"] = True
            ev["mixed_sources"] = [
                (m["source"], sources[m["source"]].domain if m["source"] in sources else None)
                for m in ev_members
            ]
        events.append(ev)

    if n_shell_dropped:
        log.info("events.shell.dropped", n_shell_dropped=n_shell_dropped,
                 min_body_chars=ep.min_body_chars,
                 note="全成员无真文章正文(只剩标题/跳转链)的事件已排除出榜")

    # 6.5) 分板分类器(board_classifier;见 docs/DESIGN.md §2.6):对 is_mixed 旁路事件读内容判板,
    #      原地改写 source_domain(归对板)或留 None(other/abstain/低置信/fail → 丢弃)。专源事件零接触。
    #      LLM 锁在 classify 回调,整批一次 to_thread(别堵事件循环)。关时不进(events 无 is_mixed)。
    if bc_on:
        board_cache: dict | None = None
        if ep.judgment_cache_enabled:
            from pulsewire.events.board_classifier import board_item_hash, board_prompt_hash
            from pulsewire.store import get_cached_judgments
            _bph = board_prompt_hash(settings, active)
            _bhashes = list({board_item_hash(e) for e in events if e.get("is_mixed")})
            async with sm() as _s:
                board_cache = await get_cached_judgments(
                    _s, judge_name="board", prompt_hash=_bph, item_hashes=_bhashes)
        classifier = make_board_classifier(settings, active,
                                           judgment_cache=board_cache, new_verdicts=new_verdicts)
        await asyncio.to_thread(classify_mixed_events, events, classifier,
                                top_n=ep.board_judge_top_n)

    # 6.75) 已剪记忆闸标注(clip_memory,①治日报逐日重复;默认关):成员簇对账在追线挂线痕
    #      (thread_clusters=逐日已剪台账),命中的事件打 prev_report(既往天数/最近已剪日/前情/
    #      材料全旧)。真正的踢在 _board_select 闸链首道 filter_already_clipped。
    #      账本查询失败 = fail-open 当无账本(增强功能,绝不拖垮选稿);闸关 = 零行为变化。
    novelty_judge = None
    if ep.clip_memory_enabled and events:
        from zoneinfo import ZoneInfo

        from pulsewire.events.clip_memory import (
            annotate_prev_reports,
            load_clip_ledger,
            logical_date,
            make_novelty_judge,
            novelty_item_hash,
            novelty_prompt_hash,
        )
        tz_app = ZoneInfo(settings.app.timezone)
        today = logical_date(run_id, tz_app)  # 锚 run 逻辑日(f05 口径:跨午夜补课不错位)
        try:
            all_cids = sorted({c for e in events for c in e.get("member_cluster_ids", ())})
            async with sm() as _s:
                ledger = await load_clip_ledger(
                    _s, all_cids, today=today, window_days=ep.clip_window_days)
        except Exception as exc:  # noqa: BLE001 — 查账失败=当无账本(fail-open),绝不拖垮选稿
            ledger = {}
            log.warning("clip.ledger.failed", error=str(exc), error_type=type(exc).__name__)
        n_marked = annotate_prev_reports(events, ledger, tz=tz_app)
        log.info("clip.ledger.matched", events=len(events), matched=n_marked,
                 threads_hit=len({r["thread_id"] for r in ledger.values()}), today=today)
        # 判决缓存(S1):预载既往事件的 novelty 裁决(键含前情——昨天又剪过一天则自然失效重判)
        nov_cache: dict | None = None
        if ep.judgment_cache_enabled:
            from pulsewire.store import get_cached_judgments
            _nph = novelty_prompt_hash(settings)
            _nhashes = list({
                novelty_item_hash(e) for e in events
                if e.get("prev_report") and not e["prev_report"]["linked_today"]
                and not e["prev_report"]["stale_material"]
            })
            if _nhashes:
                async with sm() as _s:
                    nov_cache = await get_cached_judgments(
                        _s, judge_name="novelty", prompt_hash=_nph, item_hashes=_nhashes)
        novelty_judge = make_novelty_judge(
            settings, judgment_cache=nov_cache, new_verdicts=new_verdicts)

    # 诊断:新鲜度通过率 + 各域相关度分布(校准 τ_rel/freshness 用;别瞎猜阈值)
    n_fresh = sum(1 for e in events if S.passes_freshness_window(e["peak_at"], now, cfg=ep))
    for d in active:
        rels = sorted((e["relevance"][d.key] for e in events), reverse=True)
        if rels:
            log.info("events.relevance.dist", domain=d.key,
                     max=round(rels[0], 3), p10=round(rels[len(rels) // 10], 3),
                     p25=round(rels[len(rels) // 4], 3), median=round(rels[len(rels) // 2], 3),
                     ge_tau=sum(1 for r in rels if r >= ep.relevance_gate), tau_rel=ep.relevance_gate)
    log.info("events.freshness.dist", n_events=len(events), n_pass_fresh=n_fresh,
             window_h=ep.freshness_window_hours)

    # 7) 每域:新鲜度硬窗 → 源领域分板块 → 限额+同事件去重兜底 → 写 rankings。
    #    选稿(含判官复判,同步 LLM)先在事务外用 to_thread 算好,再开短事务只写库(别让 LLM 占着事务/堵事件循环)。
    def _same_event(a: dict, b: dict) -> bool:
        try:
            return judge_same_event(
                {"subject": a["subject"], "headline": a["headline"], "snippet": a["snippet"]},
                {"subject": b["subject"], "headline": b["headline"], "snippet": b["snippet"]},
                settings=settings)
        except PermanentLLMError:
            raise  # 没钱/凭证失效:熔断整跑,绝不吞成保守票(2026-07-02 E1)
        except Exception:
            return False  # 判官失败=保守不合(绝不误折叠)

    # 重磅度闸(B 档语义"水货"筛;默认关,见 docs/DESIGN.md §2):分板块后、限额前,把头部水货移除,
    # 回填靠 apply_event_quotas 自身 heat 降序贪心。LLM 判官锁在 make_water_judge 工厂内,同步 → 在 to_thread 里跑。
    # 判决缓存(S1,默认关):三道 board_select 闸(水货/够格/话题)建工厂前预载本轮候选的已有裁决,
    # 命中直接读不调 LLM。水货/够格 board-无关(headline+snippet 键,跨板缓存);话题 board-相关
    # (键含板块画像),按各事件**已定板**(source_domain,已过 6.5 分板器改写)预载对应板的裁决。
    mag_cache: dict | None = None
    worthy_cache: dict | None = None
    topic_cache: dict | None = None
    if ep.judgment_cache_enabled:
        from pulsewire.store import get_cached_judgments
        if ep.magnitude_gate_enabled:
            from pulsewire.events.magnitude_judge import magnitude_item_hash, magnitude_prompt_hash
            _hashes = list({magnitude_item_hash(e) for e in events})
            async with sm() as _s:
                mag_cache = await get_cached_judgments(
                    _s, judge_name="magnitude", prompt_hash=magnitude_prompt_hash(settings),
                    item_hashes=_hashes)
        if ep.worthiness_gate_enabled:
            from pulsewire.events.worthiness_judge import worthiness_item_hash, worthiness_prompt_hash
            _hashes = list({worthiness_item_hash(e) for e in events})
            async with sm() as _s:
                worthy_cache = await get_cached_judgments(
                    _s, judge_name="worthiness", prompt_hash=worthiness_prompt_hash(settings),
                    item_hashes=_hashes)
        if ep.topic_gate_enabled:
            from pulsewire.events.topic_judge import topic_item_hash, topic_prompt_hash
            _portraits = getattr(ep, "topic_portraits", None) or {}
            _dom_by_key = {d.key: d for d in active}
            _hashes = list({
                topic_item_hash(_dom_by_key[e["source_domain"]], e, _portraits.get(e["source_domain"]))
                for e in events if e.get("source_domain") in _dom_by_key
            })
            async with sm() as _s:
                topic_cache = await get_cached_judgments(
                    _s, judge_name="topic", prompt_hash=topic_prompt_hash(settings),
                    item_hashes=_hashes)
    # 重磅度闸(B 档语义"水货"筛;默认关,见 docs/DESIGN.md §2):分板块后、限额前,把头部水货移除,
    # 回填靠 apply_event_quotas 自身 heat 降序贪心。LLM 判官锁在 make_water_judge 工厂内,同步 → 在 to_thread 里跑。
    water_judge = (
        make_water_judge(settings, judgment_cache=mag_cache, new_verdicts=new_verdicts)
        if ep.magnitude_gate_enabled else None
    )
    # 话题闸(P0:治 AI 板混入非 AI 内容;默认关,docs/DESIGN.md):分板块后、限额前,对 heat 头部判"主题属不属于本板块",
    # 跑题的移除,回填同样靠 apply_event_quotas heat 降序贪心。工厂返回 for_board(d),成本闸/缓存跨板块共享。
    topic_for_board = (
        make_topic_judge(settings, judgment_cache=topic_cache, new_verdicts=new_verdicts)
        if ep.topic_gate_enabled else None
    )
    # 要闻够格闸(用户"纯,没有就不报,所有板块一样";默认关,见 docs/DESIGN.md §2.7):水货闸之后再判
    # "够不够格当今日要闻",不够格踢、不硬凑数(够格不足 final_limit 就少报)。三板共用同一工厂(成本闸/缓存跨板共享)。
    worthiness_judge = (
        make_worthiness_judge(settings, judgment_cache=worthy_cache, new_verdicts=new_verdicts)
        if ep.worthiness_gate_enabled else None
    )

    suffix = "__shadow" if shadow else ""

    async def _board_select(d) -> tuple:
        # 新鲜窗按域覆盖(见 docs/DESIGN.md §2.7):AI 板放宽到 d.freshness_window_hours(144h)填满,
        # bio/geo 该字段为 None → 退回全局 ep.freshness_window_hours(48h)= 零行为变化。
        d_win = getattr(d, "freshness_window_hours", None)
        board_evs = [
            e for e in events
            if S.passes_freshness_window(e["peak_at"], now, cfg=ep, window_hours=d_win)
            # 展示的代表(rep)本身也须在窗内:防"事件 peak 被近期短提及撑进窗、却拿 19 天前旧文当头条"
            # (2026-06-29 盲考官逮到 #2 Claude Fable 19天前;见 docs/DESIGN.md §2.7)。
            # rep_at 缺省 → 回落 peak_at(已过上面的窗),不静默误杀 rep 无日期的真事件(闭 codex MEDIUM2)。
            and S.passes_freshness_window(e.get("rep_at") or e.get("peak_at"), now, cfg=ep, window_hours=d_win)
            and e["source_domain"] == d.key  # 板块=成员源多数领域(可靠);热度+新鲜度才是选稿轴
        ]
        topic_judge = topic_for_board(d) if topic_for_board else None

        def _select(evs: list[dict], _topic_judge=topic_judge) -> list[dict]:
            # 已剪记忆闸放闸链**最前**:踢掉的重复位由后续闸在更宽候选面上回填给真新事;
            # 闸关(novelty_judge=None)= 原样透传,零行为变化。
            fresh_evs = filter_already_clipped(evs, novelty_judge,
                                               top_n=ep.novelty_judge_top_n,
                                               final_limit=cfg.final_limit)
            on_topic = filter_off_topic(fresh_evs, _topic_judge, top_n=ep.topic_judge_top_n,
                                        final_limit=cfg.final_limit)
            after_water = filter_water(on_topic, water_judge, top_n=ep.magnitude_judge_top_n,
                                       final_limit=cfg.final_limit)
            # 要闻够格闸:不够格的踢(纯,没有就少报);够格不足 final_limit → 板块自然变短。
            after_worthy = filter_unworthy(after_water, worthiness_judge,
                                           top_n=ep.worthiness_judge_top_n, final_limit=cfg.final_limit)
            return apply_event_quotas(
                after_worthy, final_limit=cfg.final_limit, cfg=cfg, now=now, same_event=_same_event)

        return d, len(board_evs), await asyncio.to_thread(_select, board_evs)

    # 三板并行选稿(2026-07-02 提速批;原 for 串行=真跑 ~23min 大头):板间无共享可变状态——
    # events 在此只读、每事件经 source_domain 只属一板;共享的闸工厂(water/worthiness/topic/同事件判)
    # 计数与缓存已在各工厂内加锁(全局成本闸 max_*_per_run 跨板仍精确)。gather 按传入顺序返回
    # → plans/写库顺序与原串行版逐字节一致。
    plans: list[tuple] = []  # (ikey, d, kept)
    domain_kept = {}
    for d, before, kept in await asyncio.gather(*(_board_select(d) for d in active)):
        base_key = getattr(d, "interest_key", None) or make_interest_key(d.interest, d.tags)
        plans.append((base_key + suffix, d, kept))
        domain_kept[d.key] = len(kept)
        log.info("events.domain.done", domain=d.key, board_candidates=before,
                 kept=len(kept), deduped=before - len(kept), shadow=shadow)

    # 判决缓存写回(S1):本轮所有判官(same_event/magnitude/worthiness/topic/board)新判的裁决
    # 一次性落库,下轮同内容+同口径直接读不调 LLM(治 f20)。增强功能:写回失败不拖垮已完成的选稿
    # (裁决还在,只是没缓存,下轮重判)。on_conflict_do_nothing 兜同键重复(同内容多事件/对称对)。
    if new_verdicts:
        from collections import Counter as _Counter

        from pulsewire.store import upsert_judgments
        try:
            async with sm() as session:
                async with session.begin():
                    await upsert_judgments(session, new_verdicts)
            log.info("events.judgment_cache.wrote", total=len(new_verdicts),
                     by_judge=dict(_Counter(r["judge_name"] for r in new_verdicts)))
        except Exception as exc:  # noqa: BLE001 — 缓存写回是增强,失败不拖垮选稿
            log.warning("events.judgment_cache.write_failed", error=str(exc))

    async with sm() as session:
        async with session.begin():
            for ikey, d, kept in plans:
                await prune_rankings(session, interest_key=ikey,
                                     keep_item_ids=[e["rep_item_id"] for e in kept])
                for rank, e in enumerate(kept, start=1):
                    # 已剪记忆随行(0012):留下的既往事件把 prev_report 写进 rankings.meta,
                    # summarize 读它做③增量写稿(rankings 只存 rep_cluster_id,下游二次对账会漏,
                    # 连续第3天起代表簇多为新簇)。linked_today(重跑)不带前情——那是今天自己的稿。
                    pr = e.get("prev_report")
                    meta = None
                    if pr and not pr.get("linked_today"):
                        meta = {"clip": {"days_prior": pr["days_prior"], "last_date": pr["last_date"],
                                         "prev_text": (pr.get("prev_text") or "")[:400],
                                         "thread_id": pr.get("thread_id")}}
                    await upsert_ranking(
                        session, interest_key=ikey, interest=d.interest, tags=d.tags,
                        item_id=e["rep_item_id"], cluster_id=e["rep_cluster_id"],
                        recall_score=e["relevance"].get(d.key, 0.0),  # 诊断:相关度(非下游契约)
                        rule_score=e["heat_score"], rerank_score=e["velocity"],
                        final_score=e["heat_score"], rank=rank, provider="events", run_id=run_id,
                        meta=meta,
                    )

    return {"events": len(events), "clusters": len(cluster_ids), "judged": judged,
            "merges": merges, "judge_capped": capped, "domains": domain_kept, "provider": "events"}
