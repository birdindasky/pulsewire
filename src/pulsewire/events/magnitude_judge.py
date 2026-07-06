"""重磅度闸判官(B 档,2026-06-22):events 选稿出口对头部候选判"是不是水货"。

水货 = 一眼就看出不值得占今天日报版面的:论坛/个人闲聊、冷知识花边、纯表态呼吁没真事、营销软文。
治 `/eval` 唯一未过的"够热门"——events 引擎挡得住"不相关/过期/没正文",但挡不住"有正文够新却不值得占版面"。
设计见 `docs/DESIGN.md` §2(codex 审 GO-with-fixes);是 `min_body_chars` 空壳护栏的语义版兄弟。

铁律护栏(B 档命门 = 零误杀,宁错放别错杀,照搬 event_judge.py 哲学):
- 判官保守(拿不准 = KEEP);LLM 失败 / 无 key / 超成本闸 / 脏返回值 → KEEP(留)。
- 🔴 严格 `out.get("water") is True` 才砍(闭 codex M2):`{"water":"yes"}` 这类字符串 `bool("yes")==True`
  会误杀真新闻,绝不用 `bool()` 强转。非严格布尔 True 一律 fallback KEEP。
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

log = get_logger()

# 正文截断(2026-07-05 考官发现①):修单让 snippet 从摘要级变全文级(semianalysis 单篇清洗后 3-6 万字),
# 本判官原样直塞 → 3票×2判官=十万级 token/事件。对齐 topic_judge 的 800 字口径:判"是不是水货"
# 看开头 800 字绰绰有余。🔴 缓存键铁律:item_hash 哈希**喂进 LLM 的确切文本**(截断后),截断口径折进失效键。
_BODY_TRUNCATE = 800

_SYSTEM = (
    "你是新闻主编。判断给你的这条是不是「水货」——一眼就看出不值得占今天日报版面的。\n"
    "**只有这两类算水货(B 档,从严界定,只砍一眼就水的):**\n"
    "① 论坛 / 个人闲聊:个人在 Reddit / 论坛发帖**求取**——求问、求推荐 / 求替代品、求助 / 求教程、"
    "问怎么联系客服、纠结选择、晒生活经历、唠嗑、玩梗。**判据 = 这帖的目的是『跟大家要答案 / 要建议 / 要帮助』。**\n"
    "② 纯花边 / 冷知识 / 猎奇趣闻:跟正经资讯无关的八卦小品、冷知识。\n"
    "**其余一律 KEEP(不是水货),哪怕它没有『今天刚发生的事』也留:**\n"
    "分析、预测、评论、观点、智库报告、综述、展望、科普、前沿研究解读、行业评论、营销稿——这些都有价值,留。\n"
    "⚠️ **边界(求取 vs 给出,务必分清)**:\n"
    "  · 帖子主体是在**给出**——分享一个结论 / 经验 / 评测 / 见解 / 数据 → 评论观点 → **KEEP**;\n"
    "  · 帖子主体是在**求取**——问问题 / 求推荐 / 求替代 / 求助 → **① 水货,即便它提到了真产品 / 真公司 / 真技术名**"
    "(提到 LiteLLM / OpenAI / Anthropic 不改变它是『求助帖』的本质)。\n"
    "  典型水货标题:『有没有 X 的替代品 / 你们都在用啥』『怎么联系到 X 的客服 / 真人』『X 报错求助』『该选 A 还是 B』。\n"
    "判准:**宁可错放别错杀**,真新闻 / 真分析拿不准一律 KEEP。只判内容形态(是私人求取 / 闲聊 / 花边,还是给出信息),"
    "**不判它有没有真实事件、也不判它属不属于某个板块**。\n"
    '只输出 JSON:{"water": true/false, "reason": "一句话理由"}'
)


def judge_is_water(headline: str, body: str, settings: "Settings") -> tuple[bool, str]:
    """单条判水货。返回 (is_water, reason)。

    🔴 严格 `is True` 判定(闭 codex M2):脏返回值 / 字段缺失 / None → is_water=False(KEEP)。
    LLM 失败/无 key 由 complete_json 冒泡,调用方(工厂)兜成 KEEP。
    """
    cfg = settings.threads  # 复用「在追」判官模型(同是保守语义判)
    body = (body or "")[:_BODY_TRUNCATE]  # 全文级 snippet 截到判官口径(见 _BODY_TRUNCATE 注)
    user = (
        f"标题:{headline}\n正文:{body}\n\n"
        '只输出 JSON:{"water": true/false, "reason": "一句话理由"}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="magnitude_judge")
    )
    is_water = out.get("water") is True  # 严格 is True;"yes"/"true"/1/None 一律 KEEP(B 档命门)
    reason = str(out.get("reason", ""))[:120]
    return is_water, reason


# 判决缓存(S1)标识。
JUDGE_NAME = "magnitude"


def magnitude_prompt_hash(settings: "Settings") -> str:
    """本判官的失效键 = system prompt + **影响裁决的运行时口径**(模型/max_tokens/票数)。

    2026-07-03 考官指出:裁决不止取决于 _SYSTEM,还取决于 judge_model 等——换模型(如把判官
    从 flash 换成 pro 治抽风)口径就变了,若只哈希 _SYSTEM 则 hash 不变 → 会拿旧模型旧裁决当新读。
    折进模型/max_tokens/票数,换模即换 key 自然失效。工厂与引擎预载须都用本函数算,保证键一致。
    """
    cfg = settings.threads
    votes = max(1, getattr(settings.rank.event_pool, "magnitude_judge_votes", 1))
    return prompt_hash_of(_SYSTEM, model=cfg.judge_model,
                          max_tokens=cfg.judge_max_tokens, votes=votes,
                          extra=f"trunc={_BODY_TRUNCATE}")


def magnitude_item_hash(ev: dict) -> str:
    """本判官喂 LLM 的确切输入(headline + snippet)的内容哈希 = 缓存 item_hash。

    与工厂内判官读同一份内容(headline + **截断后**正文,与 judge_is_water 喂 LLM 的口径逐字一致),
    预载(引擎)与判定(工厂)必须用本函数算,保证键一致。
    """
    return hash_input((ev.get("headline", "") or "") + "\x00"
                      + (ev.get("snippet", "") or "")[:_BODY_TRUNCATE])


def make_water_judge(
    settings: "Settings", *,
    judgment_cache: dict[str, dict] | None = None,
    new_verdicts: list[dict] | None = None,
) -> Callable[[dict], bool]:
    """工厂:返回 (event_dict) -> is_water 回调,带成本闸 + 缓存 + fail-safe + 被砍可观测。

    注入给选稿层(events/engine.py),LLM/IO 全锁在这里;纯过滤逻辑注入假回调即可单测。
    被判 water 的事件打 `magnitude.water_dropped`(headline + reason)供 Phase 2 A/B 递盲考官验"零误杀"。
    """
    ep = settings.rank.event_pool
    calls = [0]
    cache: dict[str, bool] = {}
    # 并发判(gate_pool.map_judge)下 calls/cache 会被多线程碰:锁守护成本闸计数与缓存读写,
    # max_water_judges_per_run 仍是精确硬顶(LLM 调用本身在锁外,不串行化)。
    lock = threading.Lock()

    votes = max(1, getattr(ep, "magnitude_judge_votes", 1))
    need = votes // 2 + 1  # 严格多数才砍(3→2):保 B 档零误杀,且补 flash 抽风的偶发漏判
    prompt_hash = magnitude_prompt_hash(settings)  # 失效键(含模型/口径),建工厂时定一次

    def judge(ev: dict) -> bool:
        key = ev.get("rep_item_id") or ev.get("headline", "")
        with lock:
            if key in cache:
                return cache[key]
        # 持久判决缓存(S1):同内容 + 同 prompt 上一轮判过 → 直接读裁决,不调 LLM(治 f20 白烧钱)。
        ihash = magnitude_item_hash(ev) if (judgment_cache is not None or new_verdicts is not None) else None
        if judgment_cache is not None and ihash in judgment_cache:
            is_water = judgment_cache[ihash].get("water") is True  # B 档铁律:严格 is True,脏值=KEEP
            with lock:
                cache[key] = is_water
            return is_water
        # 多数票:judge_is_water 跑 votes 次,够多数才砍。flash 偶发返错被多数稀释(2026-06-25 缝)。
        # 提前停省 token:已达多数即停 / 剩余票数也凑不够多数即停 → 典型每条 ~2 次调用。
        water_votes, cast, last_reason = 0, 0, ""
        dirty = False  # 任一票故障 / 成本闸截断 = 结果被污染,不缓存(只缓存真判,与 board/worthiness 同纪律)
        for i in range(votes):
            with lock:
                if calls[0] >= ep.max_water_judges_per_run:
                    dirty = True  # 成本闸截断:票不全,别把这次(保守留)缓存成永久裁决
                    break  # 成本闸到顶 → 用已投的票决(不够多数=保守留,绝不因预算耗尽误杀)
                calls[0] += 1
            cast += 1
            try:
                is_w, reason = judge_is_water(
                    ev.get("headline", "") or "", ev.get("snippet", "") or "", settings)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,判官绝不吞成 fail-safe 票(2026-07-02 E1)
            except Exception as exc:  # noqa: BLE001 — 判官是增强,失败=非水货票(保守),绝不拖垮选稿/误杀真新闻
                log.warning("magnitude.judge_failed", error=str(exc),
                            headline=(ev.get("headline", "") or "")[:60])
                is_w, reason = False, ""
                dirty = True  # 该票是故障兜底非真判 → 不缓存(下轮重判)
            if is_w:
                water_votes += 1
                last_reason = reason
            if water_votes >= need:
                break  # 已够多数砍
            if water_votes + (votes - (i + 1)) < need:
                break  # 剩余票凑不够多数 → 留(省 token)
        is_water = water_votes >= need
        with lock:
            cache[key] = is_water
            # 未命中持久缓存 → 记下裁决,选稿完统一写回(下轮同内容直接读)。
            # 🔴 只缓存全程干净票 + 无成本闸截断判出的裁决(dirty 跳过):虽水货闸兜底方向=留(安全),
            #    但统一"只缓存真判"不变式(考官 2026-07-03),不让一次故障/饿死的兜底被永久粘住。
            if new_verdicts is not None and ihash is not None and not dirty:
                new_verdicts.append(make_row(ihash, JUDGE_NAME, prompt_hash, {"water": is_water}))
        if is_water:
            log.info("magnitude.water_dropped",
                     headline=(ev.get("headline", "") or "")[:60], reason=last_reason,
                     votes=f"{water_votes}/{cast}")
        return is_water

    return judge


def filter_water(board_evs: list[dict], judge: Callable[[dict], bool] | None,
                 *, top_n: int, final_limit: int) -> list[dict]:
    """B 档重磅度闸(纯过滤,LLM 锁在 judge 回调里):对 heat 头部判水货,把判定 water 的事件**移除**。

    🔴 闭 codex M1:只**移除**水货、**不截断** board_evs——回填靠下游 apply_event_quotas 自身
    heat 降序贪心天然实现(删掉头部水货,后面的真新闻自然顶进 final_limit),绝不在这里截成"头部窗口"
    再传(那会破坏 quotas 跨整池的同事件去重/源族折叠/老项限额)。
    🔴 闭 codex m3:effective_top_n = max(top_n, final_limit + 5),防配小了把 final_limit 尾部水货漏判。
    judge=None(闸关)→ 原样返回。
    """
    if judge is None:
        return board_evs
    effective_top_n = max(top_n, final_limit + 5)
    head = sorted(board_evs, key=lambda e: e.get("heat_score", 0.0), reverse=True)[:effective_top_n]
    # 并发判(2026-07-02 提速批):verdicts 与 head 同序,语义 = [judge(e) for e in head]
    verdicts = map_judge(judge, head)
    dropped = {id(e) for e, w in zip(head, verdicts) if w}  # id() 认对象本身,不靠字段唯一
    if not dropped:
        return board_evs
    return [e for e in board_evs if id(e) not in dropped]
