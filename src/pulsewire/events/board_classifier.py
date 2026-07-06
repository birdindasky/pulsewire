"""分板分类器(2026-06-27,见 docs/DESIGN.md §2.6):给「综合源旁路事件」按内容判板归属。

治 50 个综合源混发乱跑——根因 = 源的 `domain` 标签不可信(36氪标 ai 却发财经),按源标签
分板会乱跑、还挤占专源候选名额。综合源(source.mixed=true)走旁路:全 mixed 的 cluster
单簇成事件、打 `is_mixed=True`,由本判官**读事件全文判它真正属于哪个板**(带 confidence/abstain
+ member 证据),归对板 / 都不属于(other)/ 拿不准(abstain)/ 低置信 一律丢弃。

是 topic_judge「话题闸」的上游兄弟,但**保守方向相反**(命门,务必看清):
- 话题闸判「已分到某板的内容跑不跑题」→ 保守 = KEEP(留,宁错放别错杀真内容)。
- 本判官判「无可信 domain 的纯 mixed 事件归哪板」→ 保守 = **丢弃**(返回 None)。
  纯 mixed 事件没有专源锚、没有可信 domain,判不出时**不能**放它进任何板(放=退回乱跑)。
  故:脏返回 / abstain / other / 低置信 / LLM 失败 / 成本闸到顶 → 一律 None(丢弃)。

铁律护栏:
- 🔴 board 必须是 active 域 key 之一(ai/bio/geo)且 confidence ≥ _CONFIDENCE_FLOOR 且 abstain≠True,
  才返回该 board;任何不满足 → None(丢弃)。
- 🔴 严格判定:`abstain is True` 才算弃权;confidence 脏值兜 0(→丢);board 不在 active key → 当 other(丢)。
- LLM 失败/无 key 由 complete_json 冒泡,工厂兜成 None(丢弃)+ 告警。
"""

from __future__ import annotations

import threading
from collections import Counter
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

_BODY_TRUNCATE = 800  # 正文摘要截断(控 token;头部够判归属)
_CONFIDENCE_FLOOR = 0.6  # 置信地板:低于此当"拿不准"丢弃(保守;纯 mixed 无锚,宁缺毋滥)

_SYSTEM = (
    "你是新闻分版编辑。给你几个【板块】定义和一条来自**综合源**的【事件】。\n"
    "⚠️ 这条事件来自'什么领域都发'的综合源(科技站 / 大报 / 综合科刊 / 播客),**它的源头标签不可信**,"
    "你必须**只看内容**判断它的主题**整体**属于哪个板块。\n"
    "判定四选一,输出到 board 字段:\n"
    "- 明确属于某个板块大领域 → 填那个板块的 key(哪怕讲的是它的商业 / 融资 / 政策 / 人事角度,也算属于);\n"
    "- 明确**不属于任何给定板块**(如纯财经股市 / 体育赛事 / 影视娱乐 / 消费数码评测,且不沾任何板块主题)→ 填 \"other\";\n"
    "- 你**拿不准**它属于哪个、或证据不足 → abstain 填 true。\n"
    "**命门 = 别把真东西误判成 other**:只要它的主角或主题落在某个板块的大领域里,就归那个板;"
    "只有整条**八竿子打不着任何板块**才填 other。真拿不准就 abstain,别硬塞也别硬丢。\n"
    "confidence 填 0~1 的把握度(只有很有把握属于某板才给高分)。\n"
    '只输出 JSON:{"board": "<板块key 或 other>", "confidence": 0.0-1.0, "abstain": true/false, "reason": "一句话理由"}'
)


def _boards_brief(active: list["DomainCfg"], portraits: dict[str, str]) -> str:
    """列出所有候选板块给 LLM 选(key + label + 关注 + 可选画像)。"""
    parts = []
    for d in active:
        line = f"- key=\"{d.key}\":{d.label} —— {d.interest}"
        if d.tags:
            line += f"(标签:{', '.join(d.tags)})"
        p = portraits.get(d.key)
        if p:
            line += f";画像:{p}"
        parts.append(line)
    return "\n".join(parts)


def _member_evidence(ev: dict) -> str:
    """member 证据:这条事件由哪些综合源报的 + 它们各自的(不可信)原 domain 先验分布。

    给 LLM 上下文"这是综合源、原标签别信",闭 codex 反馈②(触发缺 member 证据)。
    """
    ms = ev.get("mixed_sources") or []  # [(source_id, prior_domain), ...]
    if not ms:
        return "(无成员源信息)"
    srcs = ", ".join(sorted({sid for sid, _ in ms}))
    prior = Counter(dom for _, dom in ms if dom)
    prior_txt = ", ".join(f"{k}×{v}" for k, v in prior.most_common()) or "无"
    return f"来源(均为综合源,标签不可信):{srcs}\n它们被标的原领域分布(仅供参考,别据此判):{prior_txt}"


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def classify_board(
    ev: dict, active: list["DomainCfg"], settings: "Settings", *,
    portraits: dict[str, str] | None = None,
) -> tuple[str | None, float, bool, str]:
    """单条判板归属。返回 (board_key_or_None, confidence, abstain, reason)。

    board_key_or_None:active 域 key(归对板)或 None(丢弃:other / abstain / 低置信 / 脏返回)。
    LLM 失败/无 key 由 complete_json 冒泡,调用方(工厂)兜成 None(丢弃)。
    """
    portraits = portraits or {}
    active_keys = {d.key for d in active}
    cfg = settings.threads  # 复用判官模型(同 topic_judge,分类是逻辑活,省档 flash)
    body = (ev.get("snippet", "") or "")[:_BODY_TRUNCATE]
    user = (
        f"【可选板块】\n{_boards_brief(active, portraits)}\n\n"
        f"【事件标题】{ev.get('headline', '') or ''}\n"
        f"【事件主体】{ev.get('subject', '') or ''}\n"
        f"【正文摘要】{body}\n"
        f"【{_member_evidence(ev)}】\n\n"
        f"判断这条事件**整体**属于哪个板块,或 other(都不属于),或 abstain(拿不准)。\n"
        '只输出 JSON:{"board": "<板块key 或 other>", "confidence": 0.0-1.0, "abstain": true/false, "reason": "一句话理由"}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="board_classifier")
    )
    reason = str(out.get("reason", ""))[:120]
    conf = _safe_float(out.get("confidence"))
    if out.get("abstain") is True:  # 严格:拿不准 → 丢
        return None, conf, True, reason
    board = str(out.get("board", "") or "").strip().lower()
    if board not in active_keys:  # other / 空 / 无效 key → 丢
        return None, conf, False, reason
    if conf < _CONFIDENCE_FLOOR:  # 低置信 → 丢(保守:纯 mixed 无锚,宁缺毋滥)
        return None, conf, False, reason
    return board, conf, False, reason


JUDGE_NAME = "board"  # 判决缓存(S1)标识


def board_prompt_hash(settings: "Settings", active: list["DomainCfg"]) -> str:
    """分板器失效键 = _SYSTEM + 模型/max_tokens/票数 + **候选板块清单+画像**(全折进 extra)。

    候选板块(key/label/interest/tags/portrait)是判官口径的一部分:板换了 / 画像改了,归属会变,
    须整体失效 → 折进 extra(区别于 topic 把板放 item_hash——board 判官一次看全部板,是口径级)。
    """
    cfg = settings.threads
    votes = max(1, getattr(settings.rank.event_pool, "board_judge_votes", 1))
    portraits = getattr(settings.rank.event_pool, "topic_portraits", None) or {}
    return prompt_hash_of(_SYSTEM, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                          votes=votes, extra=_boards_brief(active, portraits))


def board_item_hash(ev: dict) -> str:
    """分板器缓存 item_hash = **稳定的事件内容**(标题+截断正文+成员证据)。

    **故意不含主体短语**(2026-07-04 A/B:主体 flash 每轮现抽、天然抖,进键则同内容跨轮键变白建缓存;
    归属判断取决于内容与成员源,不取决于主体措辞)。
    """
    body = (ev.get("snippet", "") or "")[:_BODY_TRUNCATE]
    material = "\x00".join([
        ev.get("headline", "") or "", body, _member_evidence(ev),
    ])
    return hash_input(material)


def make_board_classifier(
    settings: "Settings", active: list["DomainCfg"], *,
    judgment_cache: dict[str, dict] | None = None,
    new_verdicts: list[dict] | None = None,
) -> Callable[[dict], str | None]:
    """工厂:返回 `classify(event_dict) -> board_key | None`。

    成本闸 + 缓存闭在外层 → `max_board_judges_per_run` 是真·每 run 全局硬顶(不随板块数膨胀)。
    🔴 保守方向 = 丢弃(与 topic_judge 相反):成本闸到顶 / LLM 失败 → None(纯 mixed 无可信 domain,
    不能放行)。判定结果打 `board_classifier.rerouted`(归对板)/ `.dropped`(丢)供 A/B 盲考官验。
    LLM/IO 全锁这里,纯逻辑注入假 classify 即可单测。
    判决缓存(S1,默认关):**只缓存干净裁决**——分板保守方向=丢弃(可见性损失),若把"一次抽风/预算耗尽
    导致的丢"也记进缓存,会把本该归板的真事件永久误杀。故 `dirty`(任一票异常 / 成本闸截断)时不记录,
    只缓存全程干净票决出的结果(真归板 or 真 other/abstain/低置信丢)。
    """
    ep = settings.rank.event_pool
    portraits = getattr(ep, "topic_portraits", None) or {}
    calls = [0]
    cache: dict[str, str | None] = {}
    # 并发判(gate_pool.map_judge)下 calls/cache 会被多线程碰:锁守护成本闸计数与缓存读写,
    # max_board_judges_per_run 仍是精确硬顶(LLM 调用本身在锁外,不串行化)。
    lock = threading.Lock()
    votes = max(1, getattr(ep, "board_judge_votes", 1))
    need = votes // 2 + 1  # 严格多数才归板(3→2);凑不齐=丢弃(保守 drop,治 flash 抽风把边缘货飘进板)
    prompt_hash = board_prompt_hash(settings, active)  # 失效键(含模型/口径/板清单),建工厂时定一次

    def classify(ev: dict) -> str | None:
        key = ev.get("rep_item_id") or ev.get("headline", "")
        with lock:
            if key in cache:
                return cache[key]
        # 持久判决缓存(S1):同内容 + 同 prompt(含板清单)上一轮判过 → 直接读裁决,不调 LLM。
        ihash = (board_item_hash(ev)
                 if (judgment_cache is not None or new_verdicts is not None) else None)
        if judgment_cache is not None and ihash in judgment_cache:
            cached = judgment_cache[ihash].get("board")
            result = cached if (cached in {d.key for d in active}) else None  # 脏/失效 key → 丢(保守)
            with lock:
                cache[key] = result
            return result
        # 多数票:classify_board 跑 votes 次,某真板得严格多数才归该板,否则丢弃(保守)。
        # 提前停:某真板已够多数即停 / 剩余票也凑不出任何真板多数即停(省 token,典型每条 ~2 次)。
        tally: Counter = Counter()
        cast, last_conf, last_reason = 0, 0.0, ""
        dirty = False  # 任一票异常 / 成本闸截断 = 结果不纯,不缓存(护"丢弃"这个可见损失方向)
        for _ in range(votes):
            with lock:
                if calls[0] >= ep.max_board_judges_per_run:
                    dirty = True  # 成本闸截断:票不全,别把这次(可能的)丢缓存成永久丢
                    break  # 成本闸到顶 → 用已投票决(不够多数=丢,绝不因预算耗尽误放行)
                calls[0] += 1
            cast += 1
            try:
                board, conf, _abstain, reason = classify_board(
                    ev, active, settings, portraits=portraits)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,判官绝不吞成 fail-safe 票(2026-07-02 E1)
            except Exception as exc:  # noqa: BLE001 — LLM 失败=该票投 None(丢),绝不拖垮选稿
                log.warning("board_classifier.judge_failed",
                            error=str(exc), headline=(ev.get("headline", "") or "")[:60])
                board, conf, reason = None, 0.0, ""
                dirty = True  # 有票是故障丢,不是真判丢 → 不缓存(否则真事件被永久误杀)
            tally[board] += 1  # board = 板 key 或 None(other/abstain/低置信/fail 都归 None)
            if board:
                last_conf, last_reason = conf, reason
            top, topn = tally.most_common(1)[0]
            if top is not None and topn >= need:
                break  # 某真板已够多数
            best_real = max([n for b, n in tally.items() if b is not None], default=0)
            if best_real + (votes - cast) < need:
                break  # 剩余票凑不出任何真板多数 → 丢
        top_board, top_count = tally.most_common(1)[0] if tally else (None, 0)
        result = top_board if (top_board is not None and top_count >= need) else None
        with lock:
            cache[key] = result
            # 只缓存干净裁决(见工厂 docstring):dirty 时跳过,下轮重判(护"丢弃"方向不永久误杀)。
            if new_verdicts is not None and ihash is not None and not dirty:
                new_verdicts.append(make_row(ihash, JUDGE_NAME, prompt_hash, {"board": result}))
        if result:
            log.info("board_classifier.rerouted", board=result, confidence=round(last_conf, 2),
                     votes=f"{top_count}/{cast}",
                     headline=(ev.get("headline", "") or "")[:60], reason=last_reason)
        else:
            log.info("board_classifier.dropped", votes=f"{top_count}/{cast}",
                     headline=(ev.get("headline", "") or "")[:60],
                     reason=(f"多数票未达成 tally={dict(tally)}" if cast else "未判(成本闸)"))
        return result

    return classify


def is_bypass_cluster(member_source_ids, sources) -> bool:
    """cluster 是否走「旁路」(board_classifier;见 docs/DESIGN.md §2.6,闭 codex Fix2)。

    旁路 ⟺ **有 ≥1 mixed member 且无任何专源 member**(无专源锚、源标签不可信)。
    专源 member = 在 sources 且 not board_only 且 not mixed。
    🔴 必须"含 mixed"才旁路(闭 Fix2):全 board_only / 全停用源的簇 has_mixed=False → 主路,
    其 dom_ct 本就空、按现状自然丢弃,绝不误纳进判官白费钱。
    """
    has_pro = any(s in sources and not sources[s].board_only and not sources[s].mixed
                  for s in member_source_ids)
    if has_pro:
        return False
    return any(s in sources and sources[s].mixed for s in member_source_ids)


def classify_mixed_events(
    events: list[dict], classify: Callable[[dict], str | None] | None, *, top_n: int,
) -> None:
    """原地给 is_mixed 旁路事件判板:改写 `source_domain`(归对板)或留 None(丢弃)。

    🔴 只判 heat 头部 `top_n` 个 is_mixed 事件(有界成本);尾部低热 mixed 不判、`source_domain`
    保持 None = 丢弃(不值得花 LLM)。classify=None(闸关)→ 不动任何事件。
    非 is_mixed 事件(专源主路)根本不碰——`source_domain` 已由专源投票定,零接触(主路零回归)。
    """
    if classify is None:
        return
    mixed = [e for e in events if e.get("is_mixed")]
    head = sorted(mixed, key=lambda e: e.get("heat_score", 0.0), reverse=True)[:max(top_n, 0)]
    # 并发判(2026-07-02 提速批):verdicts 与 head 同序;事件改写留在主线程做(每条只写自己的 dict)
    verdicts = map_judge(classify, head)
    for e, board in zip(head, verdicts):
        e["source_domain"] = board  # board key(归板)或 None(丢)
