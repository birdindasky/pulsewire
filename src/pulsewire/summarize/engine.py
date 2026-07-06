"""统一总结引擎:对阶段4精排候选生成结构化日报总结,每个数字回源对账。

数字 0 编造的关键:**只把事实的 label(不含数字)喂给模型**,数字以 {Fn} 占位;模型从没见过
具体数字 → 编不出来。产出后由 verify 用库里真实值替换占位,并把模型偷写的裸数字标出来。
分块产出:每批 batch_size 条单独一次 LLM 调用,避免单次响应超模型输出 token 上限被截断成坏 JSON。
JSON Schema 失败:重试 → repair(再要一次严格 JSON)→ 仍失败则该块跳过(记录,不静默);全块失败才冒泡。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pulsewire.dedup.embedding import get_embedder
from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.obs.alert import alert_failure
from pulsewire.store import (
    get_items_by_ids,
    get_rankings,
    get_sessionmaker,
    prune_summaries,
    upsert_digest,
    upsert_summary,
)
from pulsewire.verify import scrub_unsourced_numbers, verify_item

from .backends import complete, parse_json
from .schema import DigestOutput, FactToken

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_SYSTEM = (
    "你是一份面向中文读者的科技日报主编,读者是关心 AI 与科技的普通人,不一定懂专业术语。"
    "为每个条目产出三段:\n"
    "- headline:中文短标题。**第一要务是让人一眼看清这件事本身——谁(公司/人/产品)+ 做了什么 + 涉及谁/什么**,"
    "而不是卖关子。主体、动作、对象直给(例:『SpaceX 正式递交招股书启动上市』"
    "『五角大楼用马斯克的 Grok AI 辅助对伊朗的弹药打击』)。六条禁令:① 别把媒体名/作者名写进标题"
    "(如『TechCrunch 扒出…』『据 XX 报道』);② 别用不点明事实的卖关子钩子(『谁将成最大赢家』"
    "『结果你想不到』『野心毕露』这类只吊胃口不给信息的悬念词);③ 别写『某模型发布』式干巴标题;"
    "④ **禁营销腔/热度词**——『成热门/爆火/走红/受追捧/受关注/热度飙升/人气高涨/持续走红/掀起…热潮』"
    "这类『它很火』的废话一律不准进标题;读者要的是『它是什么、做了什么』,不是『它火不火』(火不火放 tldr 说);"
    "⑤ **禁堆砌**——一条标题别塞三个以上逗号分句(产品名+火不火+功能+特性+适配谁全堆上);开头先一句"
    "点清主体本质(『X 是做什么的』或『谁做了什么』),最多再带一个最关键的点,其余细节全留给 tldr;"
    "精简归精简,但别把『它到底有什么用』这个核心也一并删掉;"
    "⑥ **禁虚假新发布**——去掉营销腔后,**不许改用『发布/上线/推出/开源了/问世/亮相』**把一个已存在已久、"
    "只是近期才变火的东西(尤其 GitHub 项目)写成『今天才新出现』;拿不准是不是真·当天首发,就用中性陈述"
    "(『X 是一个做……的工具/平台』,只说它是什么、不暗示时间);只有确属当天的发布事件(某公司今天正式发布新品)才可用『发布』。"
    "**判定从严**:标题主语若是某个产品/项目/工具/库/平台(而非『某方今天宣布/签署/推迟…』式的事件新闻),"
    "一律默认它已存在、按『X 是一个……』陈述,绝不写『推出/发布/上线/亮相』——哪怕它是某大公司(NVIDIA/谷歌等)的官方项目也一样。\n"
    "**清楚永远优先于抓人**——具体事实本身就是最好的钩子,在说清事实的前提下再点出最抓人的那个细节。\n"
    "- tldr:一句话讲清这条最核心的事,给只想扫一眼的人。\n"
    "- insight:详细、充分展开的白话深度解读,像懂行的朋友讲给外行听,**400-700 字,分 2-4 个自然段**,"
    "不要写成一句话敷衍。要尽量覆盖:① 出现专业术语时用打比方/类比解释清楚;"
    "② 交代背景与来龙去脉(这是谁、之前发生过什么、为什么现在出现);③ 讲清它具体做了什么、"
    "关键的技术或做法细节;④ 和同类/竞品/旧方案横向对比,点出差异和高下;⑤ 说清为什么值得关注、"
    "对不同读者(开发者/创业者/普通用户)分别意味着什么;⑥ 有社区讨论、争议或质疑就如实带上;"
    "⑦ 点出『接下来值得看什么』或悬念。自然口语,有信息密度,别堆术语、别浮夸、别复述标题、别空话凑字数。\n"
    "外行可读铁律(直接对应读者『看不懂这是啥新闻』的抱怨,headline 和 tldr 尤其严格):全程写给完全不懂技术的人。"
    "① **专业名词必翻译**:禁止把论文/技术术语原样堆进标题(几何/算子/架构/机制的学名、"
    "『流水线』『并发漏洞』『护城河』『各向异性』『多模态』这类行话),一律翻成大白话『让 X 能做到 Y』"
    "并点出意义(so what)——普通人读完要能说出『哦,这是个 XX 的事』。纯学术细节实在讲不清,"
    "就只讲它的意义、别塞术语名(讲不成人话的纯理论宁可只留一句意义)。"
    "② **别卖关子**:只给态度/不给实质的(如『FDA 持开放态度』『备受关注』)要写清实质进展(批没批/到哪步),"
    "或注明『尚未定论』,不许只吊一个模糊态度让人看完不知发生了什么。"
    "③ **别写空泛感慨**:每条必须落到一件具体新鲜事(谁、何时、做了什么),不要『重新聚焦』『结束遥遥无期』"
    "这种没有新信息的感慨句。\n"
    "数字铁律(最重要):insight 里**优先用定性描述**(如『大幅领先』『显著提升』『接近一半』『翻了几倍』),"
    "尽量少写具体数字。一个具体数字只有满足以下之一才能写:① 它在下方『可引用事实』里 → 用 {Fn} 占位引用;"
    "② 它在我给你的正文里逐字出现过。**凭印象、常识或推测写的任何数字(百分比、分数、金额、倍数)一律不许出现**,"
    "拿不准就改成文字描述。日期/金额/百分比尤其小心:只有正文逐字出现才照抄、且写法单位要一致"
    "(正文是 'June 22' 就别写成 '6月22日'、'$295 billion' 就别换算成 '2950亿'),否则一律定性说"
    "(『近期』『数千亿美元』『过半』)。打比方也不要编数字。"
    "尤其:若这条的正文其实只是一个标题或一段跳转链接(没有真正文可依),金额/数字一律改定性说"
    "(『巨额』『数千亿美元级』),绝不照标题里的数字去换算或加工——宁缺毋错。\n"
    "传闻与重大断言铁律:公司上市/IPO/融资估值、性能倍数提升、临床治愈/突破、战争伤亡/制裁封锁"
    "这类重大断言,凡条目标注『仅单一来源』,或正文本身用 reportedly/rumored/据传 等口吻,"
    "必须写成『据报道』『有消息称』,headline 也一样——绝不许把传闻写成既成事实;"
    "多源同报的才可平实陈述。官方营销话术(厂商自称『性能翻倍』『行业领先』)要么注明"
    "『官方宣称』,要么改定性转述,不可当独立事实。\n"
    "条目聚焦铁律:有的条目是 newsletter/多话题简报/播客,**headline 和 tldr 必须锁定单独一件事**——"
    "若该条并列了多个话题,只挑最重磅的一件来写,**标题里绝不并列两件事**(禁止『讨论 X 与 Y』『X，同时 Y』"
    "『X 与 Y』这种把两件不相干的事塞进一条标题);其余顺带话题(『还提到』『other news』『五件事』之类)"
    "连提都不要提,绝不写进 headline/tldr/insight。严格只输出 JSON。"
)


def _build_tokens(items_facts: dict[str, list[dict]]) -> tuple[list[FactToken], dict[str, list[FactToken]]]:
    """给每条目的 enriched 事实分配全局 token F1..FN;返回(全部, 按 item 分组)。"""
    all_tokens: list[FactToken] = []
    by_item: dict[str, list[FactToken]] = {}
    n = 0
    for item_id, facts in items_facts.items():
        for f in facts:
            n += 1
            ft = FactToken(
                token=f"F{n}", item_id=item_id, source_id=f["source_id"],
                label=f.get("label", f.get("kind", "")), value=f.get("value"),
                unit=f.get("unit", ""),
            )
            all_tokens.append(ft)
            by_item.setdefault(item_id, []).append(ft)
    return all_tokens, by_item


def _build_user_prompt(
    ordered,
    by_item: dict[str, list[FactToken]],
    content_max_chars: int = 200,
    corroboration: dict[str, int] | None = None,
    github_ids: set[str] | None = None,
    tracked: dict[str, dict] | None = None,
) -> str:
    """ordered: [(item_id, title, content)];事实只给 label(不给数字),逼模型用占位。

    content 已是"全文/逐字稿优先,否则 feed 摘要";按 content_max_chars 截断控 token。
    corroboration:每条目"多源同报"佐证数;单源条目在提示词里点名,要求重大断言写『据报道』。
    tracked:已剪记忆(③增量写稿):{item_id: {days_prior, last_date, prev_text}}——此前已剪报过的
    事件带前情上下文,只写新进展不复述(治日报逐日重复的"写稿侧")。缺省/未命中=零变化。
    """
    lines = [
        "请为以下条目各写日报内容,每条三段:headline(一眼看清『谁做了什么』的短标题,清楚优先于抓人,"
        "别塞媒体名、别卖关子)、tldr(一句话速读)、",
        "insight(400-700 字、2-4 段的白话深度解析:术语打比方、背景来龙去脉、做了什么的细节、"
        "与同类横向对比、对不同读者的意义、社区争议、接下来看什么;要有信息密度,别凑字数)。",
        "数字**只能用下方 {Fn} 占位**引用,其它数字必须在正文里出现过才能写。",
        "",
        "条目:",
    ]
    for idx, (item_id, title, content) in enumerate(ordered, start=1):
        lines.append(f"[{idx}] item_id={item_id}")
        lines.append(f"    原标题: {title}")
        if content:
            lines.append(f"    正文/摘要: {content[:content_max_chars]}")
        toks = by_item.get(item_id, [])
        if toks:
            facts = "  ".join(f"{{{t.token}}}={t.label}" + (f"({t.unit})" if t.unit else "") for t in toks)
            lines.append(f"    可引用事实: {facts}")
        else:
            lines.append("    可引用事实:(无,正文里没有的数字一律不要写)")
        if github_ids and item_id in github_ids:
            # GitHub 开源项目(热榜按近期 star 涨势入选、AI 板的 github-trending 条目同理):
            # 治"老项目被写成今日新发布"(2026-06-25 三考 freshness 一票否决,用户选诚实改写文案)。
            lines.append(
                "    ⚠️ 这是 GitHub 上的开源项目、按近期 star 热度入选(**不是今日新发布**,多为已存在一段"
                "时间、近期热门的项目):严禁写成『发布/推出/上线/开源了/正式发布/新推出/今天/刚问世/新出现/"
                "尚处早期阶段』等暗示『刚诞生』的措辞。『近期热门/star 涨势迅猛』这类背景**只放进 tldr/insight 如实说**,"
                "**别堆进 headline**(headline 守上面五条禁令,尤其禁营销腔);**headline 只写清『这项目是什么 + 解决什么』**,"
                "绝不写『成热门/受追捧/受关注/人气高涨』。别凭空猜诞生时间。"
            )
        if corroboration is not None:
            c = corroboration.get(item_id, 1)
            if c >= 2:
                lines.append(f"    信源佐证: {c} 个源同报")
            else:
                lines.append("    信源佐证: 仅单一来源(重大断言须写『据报道』,不可写成既成事实)")
        if tracked and item_id in tracked:
            tk = tracked[item_id]
            prev = (tk.get("prev_text") or "")[:300]
            lines.append(
                f"    ⚠️ 连续报道(此前已报 {tk.get('days_prior', 1)} 天,最近 {tk.get('last_date', '')}):"
                f"这件事本日报此前已经剪报过,读者已知前情:『{prev}』。今天**只写自上次报道以来的"
                "新进展**(新事实/新数字/新表态/反转/后续落地);headline 聚焦今天的新变化,不要重新"
                "介绍这件事本身;背景最多一句带过,严禁把前情内容当新闻再讲一遍;前情里出现过的数字"
                "不要再写(除非今天的正文里再次逐字出现)。若今天材料的核心就是前情的复述,tldr 开头"
                "写『进展有限:』并只用一两句说清最新状态,insight 也相应从短。"
            )
    lines.append("")
    lines.append(
        '输出 JSON:{"digest":"<一段总体概述,白话点出今天最值得关注的几件事>","items":'
        '[{"item_id":"<上面的item_id>","headline":"...","tldr":"...","insight":"..."}, ...]},'
        "为每个 item_id 各一条。"
    )
    return "\n".join(lines)


async def _corroboration_map(session, settings: Settings, rankings) -> dict[str, int]:
    """每条目的"多源同报"佐证数 = max(簇内源数, 事件热度),与 rank 的 convergence 同口径。

    高风险定性断言闸门用:真大事必然多源同报,单源的"上市/伤亡/突破"标待核实。
    热度按 rank 的同一套窗口/阈值现算(rankings 表不存 heat;近窗约数千条,秒级)。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from pulsewire.rank.heat import compute_heat
    from pulsewire.store import get_recent_embeddings
    from pulsewire.store.tables import Cluster

    corro = {r.item_id: 1 for r in rankings}

    cluster_of = {r.item_id: r.cluster_id for r in rankings if r.cluster_id}
    if cluster_of:
        rows = (
            await session.execute(
                select(Cluster.cluster_id, Cluster.source_count).where(
                    Cluster.cluster_id.in_(set(cluster_of.values()))
                )
            )
        ).all()
        counts = {cid: sc for cid, sc in rows}
        for iid, cid in cluster_of.items():
            corro[iid] = max(corro[iid], counts.get(cid, 1))

    since = datetime.now(timezone.utc) - timedelta(hours=settings.rank.heat_window_hours)
    rows = await get_recent_embeddings(session, since=since)
    if rows:
        heats = compute_heat(
            [r[2] for r in rows], [r[1] for r in rows],
            threshold=settings.rank.heat_sim_threshold,
        )
        heat_map = dict(zip((r[0] for r in rows), heats))
        for iid in corro:
            corro[iid] = max(corro[iid], heat_map.get(iid, 1))
    return corro


def _complete_validated(system: str, user: str, settings: Settings) -> DigestOutput:
    """调后端 + 校验 JSON Schema;失败重试/repair,耗尽冒泡。"""
    last_err: Exception | None = None
    attempts = settings.summarize.json_schema_retry + 1
    for i in range(attempts):
        u = user if i == 0 else user + "\n\n上次输出不是合法 JSON 或缺字段,请严格按要求只输出 JSON。"
        try:
            raw = complete(system, u, settings)
            return DigestOutput.model_validate(parse_json(raw))
        except PermanentLLMError:
            raise  # 没钱/凭证失效:重试也没用,立即熔断,别追加提示傻重试 3 轮(2026-07-02 E1)
        except Exception as exc:
            last_err = exc
            log.warning("summarize.parse.retry", attempt=i, error=str(exc))
    raise RuntimeError(f"总结 JSON Schema 校验重试耗尽:{last_err}")


def _repair_headlines(items: list, settings: Settings):
    """标题错位护栏:语义检测 + 错位集内重配 + tldr 兜底(详见 coherence.py)。

    对整域一批产出跑一次(不分块):整批能跨块复原被写串的原标题。
    失败降级=原样返回不改(告警),绝不因护栏崩了拖垮主报。返回 (items, rep|None)。
    """
    from pulsewire.dedup.embedding import get_embedder

    from .coherence import lead_from_tldr, plan_headline_repair

    try:
        heads = [it.headline for it in items]
        bodies = [f"{it.tldr} {it.insight}" for it in items]
        vecs = get_embedder(settings).embed(heads + bodies)
        n = len(items)
        assignment, drifted, unresolved = plan_headline_repair(
            vecs[:n], vecs[n:],
            floor=settings.summarize.headline_coherence_floor,
            margin=settings.summarize.headline_coherence_margin,
        )
    except Exception as exc:  # 护栏自身失败:降级不改,不静默——告警可见
        log.warning("summarize.headline.check_failed", error=str(exc))
        return items, None
    if not drifted:
        return items, None
    new_items: list = []
    reassigned = fallback = 0
    detail: list[str] = []
    for b, it in enumerate(items):
        if b in unresolved:
            new_items.append(it.model_copy(update={"headline": lead_from_tldr(it.tldr)}))
            fallback += 1
            detail.append(f"{it.item_id}:fallback")
        elif assignment[b] != b:
            new_items.append(it.model_copy(update={"headline": items[assignment[b]].headline}))
            reassigned += 1
            detail.append(f"{it.item_id}<-{items[assignment[b]].item_id}")
        else:
            new_items.append(it)
    return new_items, {"reassigned": reassigned, "fallback": fallback, "detail": "; ".join(detail)}


async def run_summarize(
    settings: Settings,
    *,
    interest_key: str,
    run_id: str | None = None,
    sessionmaker=None,
) -> dict:
    """对某兴趣的精排结果生成总结并对账落库。返回汇总 dict。"""
    sm = sessionmaker or get_sessionmaker()

    async with sm() as session:
        async with session.begin():
            rankings = await get_rankings(session, interest_key=interest_key)
            if not rankings:
                return {"interest_key": interest_key, "items": 0, "note": "无精排结果,先跑 rank"}
            item_ids = [r.item_id for r in rankings]
            cluster_of = {r.item_id: r.cluster_id for r in rankings}
            items = await get_items_by_ids(session, item_ids)
            items_by_id = {it.item_id: it for it in items}

            # 收集各条目的 enriched 事实(带 source_id)
            items_facts = {
                iid: (items_by_id[iid].facts or {}).get("enriched", [])
                for iid in item_ids if iid in items_by_id
            }
            all_tokens, by_item = _build_tokens(items_facts)

            # 正文优先用 transcript/全文(facts.fulltext),没有再退回 feed 摘要(content)
            def _content(it):
                ft = (it.facts or {}).get("fulltext")
                if ft and ft.get("text"):
                    return ft["text"]
                return it.content

            ordered = [
                (iid, items_by_id[iid].title, _content(items_by_id[iid]))
                for iid in item_ids if iid in items_by_id
            ]
            # GitHub 开源项目条目(源 id 含 'github':trending/search/fresh 全派生源,或带 facts.github):
            # 写稿给它们"热门 repo 非今日新发布"专属口径,治"老项目冒充新事"(治热榜 + AI 板的 github-trending 条目)。
            github_ids = {
                iid for iid in item_ids if iid in items_by_id
                and ("github" in (items_by_id[iid].source or "").lower()
                     or (items_by_id[iid].facts or {}).get("github"))
            }
            # 多源同报佐证:喂给提示词(单源条目点名要求『据报道』)+ verify 断言闸门。
            corro = await _corroboration_map(session, settings, rankings)
            # 已剪记忆(③增量写稿):rank 的已剪记忆闸把 prev_report 随行写进 rankings.meta.clip
            # (0012;闸用事件全体成员簇对账,这里直接读=读到的就是闸真实看到的,零二次对账)。
            # meta 缺省(闸关/未命中/linked_today 重跑)= 不带前情,零变化。
            tracked_ctx: dict[str, dict] = {}
            for r in rankings:
                clip = (r.meta or {}).get("clip") if getattr(r, "meta", None) else None
                if clip and clip.get("prev_text"):
                    tracked_ctx[r.item_id] = clip
            if tracked_ctx:
                log.info("summarize.clip.tracked", interest_key=interest_key,
                         tracked=len(tracked_ctx))
            # 分块总结:每批 batch_size 条单独一次 LLM 调用,响应不超模型输出上限(防截断成坏 JSON)。
            # 某块重试耗尽 → 记录 + 跳过该块条目(不崩、不静默全空);全部块都失败才冒泡。
            bs = max(1, settings.summarize.batch_size)
            chunks = [ordered[i:i + bs] for i in range(0, len(ordered), bs)]
            produced: dict[str, object] = {}
            digest_text = ""
            digest_chunk: list = []  # 产出 digest 的那个块(digest 只用它的原文校验数字)
            chunks_failed = 0

            # 各块写稿并发:同步 LLM(pro)丢线程池,不阻塞事件循环;块间无依赖,prompt/模型/产物全不变,
            # 仅去掉"一块一块排队等 pro"的串行浪费(原瓶颈:summarize 域内串行)。
            # asyncio.gather 按传入顺序返回 → 合并/取 digest 仍按块序,与串行版逐字一致(内容零变化)。
            async def _do_chunk(ci: int, chunk):
                cu = _build_user_prompt(
                    chunk, by_item,
                    content_max_chars=settings.summarize.prompt_content_max_chars,
                    corroboration=corro,
                    github_ids=github_ids,
                    tracked=tracked_ctx,
                )
                try:
                    out = await asyncio.to_thread(_complete_validated, _SYSTEM, cu, settings)
                    return chunk, out
                except RuntimeError as exc:
                    log.warning("summarize.chunk.failed", interest_key=interest_key,
                                chunk=ci, of=len(chunks), error=str(exc))
                    return chunk, None

            for chunk, out in await asyncio.gather(
                *(_do_chunk(ci, chunk) for ci, chunk in enumerate(chunks))
            ):
                if out is None:
                    chunks_failed += 1
                    continue
                for s in out.items:
                    produced[s.item_id] = s
                if not digest_text and out.digest:  # 概述取首个成功块(覆盖最高名次条目)
                    digest_text = out.digest
                    digest_chunk = chunk
            if not produced:
                raise RuntimeError(
                    f"总结全部分块失败(interest_key={interest_key},{len(chunks)} 块均重试耗尽)"
                )
            # 标题错位护栏:对整域全部产出一次性检测+重配(整批比分块强——被写串的原标题能跨块复原;
            # 分块只能在本块内重配,跨块错位会降级成 tldr 兜底)。详见 coherence.py。
            headline_repaired = 0
            if settings.summarize.headline_coherence_check and len(produced) >= 2:
                repaired_items, rep = _repair_headlines(list(produced.values()), settings)
                if rep:
                    produced = {s.item_id: s for s in repaired_items}
                    headline_repaired = rep["reassigned"] + rep["fallback"]
                    log.warning("summarize.headline.repaired", interest_key=interest_key,
                                reassigned=rep["reassigned"], fallback=rep["fallback"],
                                detail=rep["detail"])
            # 先全部对账(占位替换 + 数字探测 + 关键词断言闸门),不立刻落库——
            # 中间还有一道 LLM 断言审计要翻状态。
            skipped = 0
            verified: list[tuple[str, object, object]] = []  # (iid, item_summary, VerifiedItem)
            for iid, _title, _content in ordered:
                item_summary = produced.get(iid)
                if item_summary is None:
                    skipped += 1
                    continue
                token_map = {t.token: t for t in by_item.get(iid, [])}
                # 原文(标题+正文)一并传入:原文里出现过的数字有来源(版本号/产品号),不算可疑
                v = verify_item(
                    item_summary, token_map, source_text=f"{_title}\n{_content or ''}",
                    corroboration=corro.get(iid, 1),
                    risk_min_sources=settings.summarize.risk_min_sources,
                )
                verified.append((iid, item_summary, v))

            # LLM 断言审计(治本层):复审"单源 + 关键词闸门放行"的条目成稿,
            # 逮换了措辞的"传闻当事实"。只拉 ok→needs_review,绝不反向放行。
            # 失败 → 告警降级(只剩关键词闸门),不拖垮日报;metrics 记 audit_failed。
            audit_flagged: dict[str, list[str]] = {}
            audit_failed = False
            rms = settings.summarize.risk_min_sources
            if settings.summarize.llm_audit and rms > 1:
                cands = [
                    (iid, v.headline, v.tldr, v.insight)
                    for iid, _s, v in verified
                    if v.status == "ok" and corro.get(iid, 1) < rms
                ]
                if cands:
                    from .audit import audit_single_source_items

                    try:
                        audit_flagged = audit_single_source_items(cands, settings)
                    except PermanentLLMError:
                        raise  # 没钱/凭证失效:熔断整跑,别把断言闸吞成 fail-open 放行(2026-07-02 E1)
                    except Exception as exc:
                        audit_failed = True
                        log.warning("summarize.audit.failed", interest_key=interest_key,
                                    candidates=len(cands), error=str(exc))

            # 卡向量增量(v2 主线B②):本轮成稿的卡当场算 card_vec,语义问答即刻能翻到当天新闻,
            # 不必再等日报后手动 `pulsewire embed-cards`。文本格式与 embed-cards 回填严格一致
            # (headline\ntldr\ninsight),保证管道卡与回填卡同分布、召回不偏。模型已被 dedup 阶段
            # 载入(进程内单例),实测仅多 ~3s。失败=降级留空(报告照发、card_vec 留 NULL 交
            # embed-cards 幂等兜底),绝不拖垮日报落库。
            card_vecs: dict[str, list[float]] = {}
            if verified:
                try:
                    embedder = get_embedder(settings)
                    texts = [
                        f"{v.headline}\n{v.tldr or ''}\n{v.insight or ''}".strip()
                        for _iid, _s, v in verified
                    ]
                    vecs = await asyncio.to_thread(embedder.embed_passage, texts)
                    card_vecs = {iid: vec for (iid, _s, _v), vec in zip(verified, vecs)}
                except Exception as exc:  # noqa: BLE001 — 卡向量是 QA 次级能力,失败不拖垮日报
                    log.warning("summarize.card_vec.failed", interest_key=interest_key,
                                n=len(verified), error=str(exc), error_type=type(exc).__name__)

            ok = risk_flagged = 0
            needs_review = skipped
            for iid, item_summary, v in verified:
                llm_claims = audit_flagged.get(iid)
                if llm_claims:
                    v.status = "needs_review"
                    v.risky_claims = v.risky_claims + [f"llm:{c}" for c in llm_claims]
                if v.status == "ok":
                    ok += 1
                else:
                    needs_review += 1
                if v.risky_claims:
                    risk_flagged += 1
                    log.info("summarize.risk_flagged", interest_key=interest_key,
                             item_id=iid, claims=v.risky_claims, corroboration=corro.get(iid, 1))
                await upsert_summary(
                    session,
                    interest_key=interest_key, item_id=iid, cluster_id=cluster_of.get(iid),
                    headline=v.headline,
                    tldr_raw=item_summary.tldr, tldr_rendered=v.tldr,
                    insight_raw=item_summary.insight, insight_rendered=v.insight,
                    status=v.status, used_source_ids=v.used_source_ids,
                    unresolved=v.unresolved_tokens,
                    # 单源高风险断言带 claim: 前缀并入 suspect(JSONB 列,免迁移,报告可见)
                    suspect=v.suspect_numbers + [f"claim:{c}" for c in v.risky_claims],
                    backend=settings.summarize.backend, model=settings.summarize.model, run_id=run_id,
                    card_vec=card_vecs.get(iid),
                )
            # 清掉本轮没产出总结的旧行(分块失败跳过的条目),免上一轮旧内容冒充本轮上线
            await prune_summaries(
                session, interest_key=interest_key,
                keep_item_ids=[iid for iid, _t, _c in ordered if iid in produced],
            )

            # digest 概述同样回源:只用「产出 digest 的那个块」的原文校验(模型写 digest 时只看了这几条),
            # 不放宽到全部条目——否则首块编的数字若恰好命中别块原文就能逃过 [待核实]。
            digest_source = "\n".join(f"{t}\n{c or ''}" for _i, t, c in digest_chunk)
            digest_clean, digest_flagged = scrub_unsourced_numbers(digest_text, digest_source)
            await upsert_digest(
                session, interest_key=interest_key, digest=digest_clean,
                backend=settings.summarize.backend, model=settings.summarize.model, run_id=run_id,
            )

    # 块失败可见性(2026-06-15 二⑦):失败块比例高 = 本轮丢了成片条目内容(prune 会软删这些条目旧总结)。
    # 全块失败已在上面 raise(走主流程告警);这里覆盖"部分块失败"的静默丢内容,补告警别让它无声。
    if chunks_failed and chunks_failed / len(chunks) >= settings.summarize.chunk_fail_alert_ratio:
        await alert_failure(
            settings, run_id=run_id or "—", stage=f"summarize:{interest_key}",
            error=(f"分块总结失败 {chunks_failed}/{len(chunks)}(≥"
                   f"{settings.summarize.chunk_fail_alert_ratio:.0%}),本轮丢了成片条目内容;"
                   f"多半 LLM 抽风,可 `pulsewire run` 同 run_id 续跑补救"),
            error_type="HighChunkFailureRate",
        )
    return {
        "interest_key": interest_key,
        "items": len(ordered),
        "summarized": ok + needs_review,
        "verified_ok": ok,
        "needs_review": needs_review,
        "risk_flagged": risk_flagged,
        "audit_flagged": len(audit_flagged),
        "audit_failed": audit_failed,
        "chunks": len(chunks),
        "chunks_failed": chunks_failed,
        "headline_repaired": headline_repaired,
        "digest_flagged": len(digest_flagged),
        "facts_available": len(all_tokens),
        "backend": settings.summarize.backend,
        "digest": digest_clean,
    }
