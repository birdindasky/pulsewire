"""话题闸判官(P0,2026-06-24):events 选稿出口对头部候选判"主题属不属于这个板块"。

治 AI 板混入非 AI 内容(电视剧/航空/财经/医药)——根因 = 板块归属唯一判据是「成员源多数领域」
(engine.py `source_domain == d.key`),综合源(IT之家只标 category:cn,domain 默认 ai)整批塞进 AI 板,
**没有内容级话题闸**;短中文标题向量余弦又分不开话题(电视剧对 AI 0.479 > 真 AI 工具 0.335,纯阈值救不了)。
故照搬 magnitude_judge 工厂骨架,加一道**喂事件全文+板块画像、按归属判**的 LLM 闸(不用余弦,codex 建议)。
是 magnitude_judge「水货闸」的话题版兄弟:水货闸判内容**形态**(私聊/花边),本闸判主题**领域归属**。

铁律护栏(命门 = 砍跑题不误杀真内容,宁错放别错杀,照搬 magnitude_judge 哲学):
- 判官保守(拿不准 / 沾边 / 属大领域子话题 = KEEP);LLM 失败 / 无 key / 超成本闸 / 脏返回值 → KEEP(留)。
- 🔴 严格 `out.get("off_topic") is True` 才砍:`{"off_topic":"yes"}` 这类字符串 `bool("yes")==True`
  会误杀真内容,绝不用 `bool()` 强转。非严格布尔 True 一律 fallback KEEP。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

from pulsewire.events.gate_pool import map_judge
from pulsewire.events.judge_cache import hash_input, make_row, prompt_hash_of
from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.llm import complete_json

if TYPE_CHECKING:
    from pulsewire.config import Settings
    from pulsewire.config.models import DomainCfg

log = get_logger()

_BODY_TRUNCATE = 800  # 正文摘要截断(控 token;头部够判归属,不需全文)

_SYSTEM = (
    "你是新闻分版编辑。给你一个【板块】定义和一条【事件】,判断这条事件**是不是明显跑题**"
    "——即它的主题**整个落在另一个大领域**、跟本板块八竿子打不着。\n"
    "**你的活是粗砍跑题,不是精挑纯度。** 千万别判「这条够不够格 / 够不够典型 / 够不够纯 / 是不是技术含量够」"
    "——那不是你的事。只要这条的**主角或主题属于本板块这个大领域**,哪怕讲的是它的"
    "**商业、金融、IPO、融资、收购、财报、产业、政治、政策、监管、人事**角度,一律留(off_topic=false)。\n"
    "**命门 = 宁可错放别错杀。** 只有当主题**明显是下面这些别的领域的事**才判 off_topic=true(踢):\n"
    "- 娱乐影视 / 电视剧 / 电影 / 明星八卦 / 音乐 / 综艺;体育赛事;\n"
    "- 纯消费硬件的发布·量产·评测(手机 / 家电,且不涉及本板块主题);\n"
    "- 交通事故 / 航空维修 / 灾害现场;社会刑案 / 个人讣告 / 个人生活求助闲聊;\n"
    "- 以及任何**主角和主题都不属于本板块大领域**的内容。\n"
    "只要**沾边、属于本板块大领域的任何子话题、或你拿不准**,一律 off_topic=false(留)。\n"
    "⚠️ 特别防误杀:本领域公司 / 机构的**资本运作(IPO / 融资 / 收购 / 财报)、政策监管、人事治理**仍属本领域,留;\n"
    "国际局势板块的**各国国内重大政治(换届 / 大选 / 政府更替 / 政局动荡 / 重大政策)、跨国突发(疫情 / 灾难 / 能源)**"
    "仍属国际局势,留。\n"
    "只判**主题属不属于这个板块**,不判它热不热、重不重要、是不是水货——那些是别的闸的事。\n"
    '只输出 JSON:{"off_topic": true/false, "reason": "一句话理由"}'
)


def _board_brief(d: "DomainCfg", portrait: str | None) -> str:
    """板块画像:label(大领域桶)+ interest(兴趣描述)+ tags + 可选 config 画像覆盖。"""
    parts = [f"【板块】{d.label}", f"【这个板块关注】{d.interest}"]
    if d.tags:
        parts.append(f"【标签】{', '.join(d.tags)}")
    if portrait:
        parts.append(f"【画像】{portrait}")
    return "\n".join(parts)


def judge_off_topic(
    d: "DomainCfg", ev: dict, settings: "Settings", *, portrait: str | None = None
) -> tuple[bool, str]:
    """单条判跑题。返回 (is_off_topic, reason)。

    🔴 严格 `is True` 判定:脏返回值 / 字段缺失 / None → is_off_topic=False(KEEP)。
    LLM 失败/无 key 由 complete_json 冒泡,调用方(工厂)兜成 KEEP。
    """
    cfg = settings.threads  # 复用「在追」判官模型(同是保守语义判)
    body = (ev.get("snippet", "") or "")[:_BODY_TRUNCATE]
    user = (
        f"{_board_brief(d, portrait)}\n\n"
        f"【事件标题】{ev.get('headline', '') or ''}\n"
        f"【事件主体】{ev.get('subject', '') or ''}\n"
        f"【正文摘要】{body}\n"
        f"【来源】{ev.get('representative_source', '') or ''}\n\n"
        f"判断这条事件的主题属不属于「{d.label}」板块。"
        '只输出 JSON:{"off_topic": true/false, "reason": "一句话理由"}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="topic_judge")
    )
    is_off = out.get("off_topic") is True  # 严格 is True;"yes"/"true"/1/None 一律 KEEP(命门)
    reason = str(out.get("reason", ""))[:120]
    return is_off, reason


JUDGE_NAME = "topic"  # 判决缓存(S1)标识


def topic_prompt_hash(settings: "Settings") -> str:
    """话题闸失效键 = _SYSTEM + 模型/max_tokens/票数(同 magnitude 纪律)。

    ⚠️ 板块画像(label/interest/tags/portrait)不进 prompt_hash——它属**输入**(在 user 消息里),
    已折进 `topic_item_hash`;画像改 → item_hash 变 → 自然失效(比放 prompt_hash 更精准,只失效受影响板)。
    """
    cfg = settings.threads
    votes = max(1, getattr(settings.rank.event_pool, "topic_judge_votes", 3))
    return prompt_hash_of(_SYSTEM, model=cfg.judge_model,
                          max_tokens=cfg.judge_max_tokens, votes=votes)


def topic_item_hash(d: "DomainCfg", ev: dict, portrait: str | None) -> str:
    """话题闸缓存 item_hash(**board-相关**,含板块画像)= 板块 brief + **稳定的文章内容**。

    话题闸判"这条属不属于板块 d",裁决随板块/画像变 → item_hash 必须含板块 brief(label/interest/
    tags/portrait)+ 标题/截断正文/来源。**故意不含主体短语**(2026-07-04 A/B:主体 flash 每轮
    现抽、天然抖,进键则同内容跨轮键变,命中率掉到 ~42%;归属判断取决于文章与板块,不取决于主体措辞)。
    """
    brief = _board_brief(d, portrait)
    body = (ev.get("snippet", "") or "")[:_BODY_TRUNCATE]
    material = "\x00".join([
        brief, ev.get("headline", "") or "",
        body, ev.get("representative_source", "") or "",
    ])
    return hash_input(material)


def make_topic_judge(
    settings: "Settings", *,
    judgment_cache: dict[str, dict] | None = None,
    new_verdicts: list[dict] | None = None,
) -> Callable[["DomainCfg"], Callable[[dict], bool]]:
    """工厂:返回 `for_board(domain) -> (event_dict)->is_off_topic` 回调。

    成本闸 + 缓存在**所有板块间共享**(`calls`/`cache` 闭在外层),故 `max_topic_judges_per_run`
    是真·每 run 全局硬顶(不随板块数膨胀;v2 扩到 7 板块也安全)。LLM/IO 全锁这里,纯过滤逻辑注入假回调即可单测。
    被判跑题的事件打 `topic.off_dropped`(board + headline + reason)供 A/B 递盲考官验"零误杀"。
    判决缓存(S1,默认关):`judgment_cache`(board-相关 item_hash 键)命中直接读;miss 记 `new_verdicts` 待写回。
    """
    ep = settings.rank.event_pool
    portraits = getattr(ep, "topic_portraits", None) or {}
    calls = [0]
    cache: dict[tuple, bool] = {}
    # 并发判(gate_pool.map_judge)下 calls/cache 会被多线程碰:锁守护成本闸计数与缓存读写,
    # max_topic_judges_per_run 仍是精确硬顶(LLM 调用本身在锁外,不串行化)。
    lock = threading.Lock()
    votes = max(1, getattr(ep, "topic_judge_votes", 3))
    need = votes // 2 + 1  # 判跑题需严格多数(3→2);凑不齐=KEEP(保守,与话题闸"宁错放别错杀"同向)
    prompt_hash = topic_prompt_hash(settings)  # 失效键(含模型/口径),建工厂时定一次

    def for_board(d: "DomainCfg") -> Callable[[dict], bool]:
        portrait = portraits.get(d.key)

        def judge(ev: dict) -> bool:
            key = (d.key, ev.get("rep_item_id") or ev.get("headline", ""))
            with lock:
                if key in cache:
                    return cache[key]
            # 持久判决缓存(S1):同内容(含板块)+ 同 prompt 上一轮判过 → 直接读裁决,不调 LLM。
            ihash = (topic_item_hash(d, ev, portrait)
                     if (judgment_cache is not None or new_verdicts is not None) else None)
            if judgment_cache is not None and ihash in judgment_cache:
                is_off = judgment_cache[ihash].get("off_topic") is True  # 命门:严格 is True,脏=KEEP
                with lock:
                    cache[key] = is_off
                return is_off
            # 多数票:judge_off_topic 跑 votes 次,判跑题需严格多数才踢,否则 KEEP(保守)。
            # 提前停:已够多数判跑题即停 / 剩余票也凑不出跑题多数即停(省 token,典型每条 ~2 次)。
            off_votes, cast, last_reason = 0, 0, ""
            dirty = False  # 任一票故障 / 成本闸截断 = 结果被污染,不缓存(只缓存真判,与 board/worthiness 同纪律)
            for _ in range(votes):
                with lock:
                    if calls[0] >= ep.max_topic_judges_per_run:
                        dirty = True  # 成本闸截断:票不全,别把这次(保守 KEEP)缓存成永久裁决
                        break  # 成本闸到顶 → 用已投票决;不足多数=KEEP(绝不因预算耗尽误杀)
                    calls[0] += 1
                cast += 1
                try:
                    is_off, reason = judge_off_topic(d, ev, settings, portrait=portrait)
                except PermanentLLMError:
                    raise  # 没钱/凭证失效:熔断整跑,判官绝不吞成 fail-safe 票(2026-07-02 E1)
                except Exception as exc:  # noqa: BLE001 — 判官失败=该票投"不跑题"(KEEP),绝不拖垮选稿/误杀真内容
                    log.warning("topic.judge_failed", board=d.key, error=str(exc),
                                headline=(ev.get("headline", "") or "")[:60])
                    is_off, reason = False, ""
                    dirty = True  # 该票是故障兜底非真判 → 不缓存(下轮重判)
                if is_off:
                    off_votes += 1
                    last_reason = reason
                if off_votes >= need:
                    break  # 已够多数判跑题 → 踢
                if off_votes + (votes - cast) < need:
                    break  # 剩余票也凑不出跑题多数 → KEEP
            is_off_final = off_votes >= need
            with lock:
                cache[key] = is_off_final
                # 🔴 只缓存全程干净票 + 无成本闸截断判出的裁决(dirty 跳过,考官 2026-07-03 统一不变式):
                #    话题闸兜底方向=KEEP(安全),但不让一次故障/饿死的兜底被永久粘住。
                if new_verdicts is not None and ihash is not None and not dirty:
                    new_verdicts.append(make_row(ihash, JUDGE_NAME, prompt_hash, {"off_topic": is_off_final}))
            if is_off_final:
                log.info("topic.off_dropped", board=d.key, votes=f"{off_votes}/{cast}",
                         headline=(ev.get("headline", "") or "")[:60], reason=last_reason)
            return is_off_final

        return judge

    return for_board


def filter_off_topic(board_evs: list[dict], judge: Callable[[dict], bool] | None,
                     *, top_n: int, final_limit: int) -> list[dict]:
    """话题闸(纯过滤,LLM 锁在 judge 回调里):对 heat 头部判归属,把判定跑题的事件**移除**。

    🔴 只**移除**跑题、**不截断** board_evs——回填靠下游 apply_event_quotas 自身 heat 降序贪心天然实现
    (删掉头部跑题,后面的真内容自然顶进 final_limit);绝不在这里截成"头部窗口"再传(那会破坏 quotas
    跨整池的同事件去重/源族折叠/老项限额)。照搬 magnitude_judge.filter_water 的 M1/m3 铁律。
    🔴 effective_top_n = max(top_n, final_limit + 5),防配小了把 final_limit 尾部跑题漏判。
    judge=None(闸关)→ 原样返回。
    """
    if judge is None:
        return board_evs
    effective_top_n = max(top_n, final_limit + 5)
    head = sorted(board_evs, key=lambda e: e.get("heat_score", 0.0), reverse=True)[:effective_top_n]
    # 并发判(2026-07-02 提速批):verdicts 与 head 同序,语义 = [judge(e) for e in head]
    verdicts = map_judge(judge, head)
    dropped = {id(e) for e, off in zip(head, verdicts) if off}  # id() 认对象本身,不靠字段唯一
    if not dropped:
        return board_evs
    return [e for e in board_evs if id(e) not in dropped]
