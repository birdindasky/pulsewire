"""要闻够格闸(2026-06-29,用户拍板"纯,没有就不报,所有板块一样")。

放宽窗+广义后 AI 板硬凑 final_limit 会把尾巴的边缘货捞上来(reddit 炫耀/求助帖、小众论文、
传闻+否认、蹭关键词的非本领域新闻)。这些都"技术上过现有闸"但**不够格当今日要闻**。
本闸判"这条够不够格当今日要闻",不够格的踢——**宁缺毋滥、没有就少报**(三板统一)。

与水货闸(magnitude)的关系:水货闸只砍"一眼就水的"(论坛闲聊/花边),保守留分析/研究/观点;
本闸**更严**:连小众无大众影响的论文、传闻否认、个人炫耀/经验帖、蹭"AI"噱头也踢。是"纯"的执行者。

铁律护栏:
- 🔴 方向 = **默认踢、够格才留**(与水货闸相反):某条留下需 LLM 多数票判"够格当要闻";凑不齐多数 → 踢。
- 🔴 **但绝不因基础设施失败误杀**:LLM 失败 / 无 key / 超成本闸 / 脏返回 → 该票算"够格"(留),保护真新闻不被故障误踢。
  只有 LLM **真判出**多数"不够格"才踢。
- 多数票(pro,治判官抖动):每条判 N 次,够格票 < 多数 → 踢。
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

# 正文截断(2026-07-05 考官发现①,同 magnitude):判"够不够格当要闻"看开头 800 字足够,
# 别把全文级 snippet 原样塞给 3 票判官。缓存键同步只哈希截断后文本,口径折进失效键。
_BODY_TRUNCATE = 800

_SYSTEM = (
    "你是日报主编,在决定一条内容**够不够格上今日要闻版面**。标准从严——宁缺毋滥。\n"
    "**够格(worthy=true)= 一桩真实发生/披露的、有分量的新闻事件:**\n"
    "产品/模型发布与重大更新、研究突破(有明确进展或结论、非纯小众)、公司大动作(融资/并购/IPO/"
    "重大人事/重大业务)、政策与监管动作、行业重要事件。\n"
    "**不够格(worthy=false,踢):**\n"
    "① 论坛/社区个人帖:求助、求推荐、提问、晒经验、炫耀做法(show-off)、个人感想——哪怕在分享技巧也算;\n"
    "② 纯小众学术论文:外行零感知、无明显大众或行业影响的窄课题(一般 arxiv 预印本多属此类,除非是重磅成果);\n"
    "③ 非事件:传闻+否认、'据称正考虑'、纯预测/畅想/观点而无新事实、花边趣闻;\n"
    "④ 蹭关键词:主角/主题其实在别的领域,只是标题带了'AI'之类的词。\n"
    "**判准:这条放进给普通读者看的'今日要闻',他会觉得'这是一桩值得知道的事'吗?会→worthy;**"
    "**只是'有人在论坛聊/一篇小众论文/一条没下文的传闻'→ not worthy。**\n"
    "⚠️ **绝不质疑真实性(命门)**:对'某公司发布/推迟/收购/融资/投资了 X'这类**媒体直接报道的公司动作或决定**,"
    "默认当**真实事件 → worthy**,**即便你没听说过那个产品/型号/公司**——那很可能是你知识截止之后的新东西,**不等于虚构/传闻**。"
    "你只判'体裁够不够格当要闻',**不判事件或产品的真假**。只有报道**自己写明**是'传闻/据传/未经证实/正考虑/拒绝置评'时才算③非事件。\n"
    "⚠️ 确凿的真新闻(公司发布/重大政策/明确突破/融资并购)**必须 worthy**,别误杀;只踢明显不够格的(论坛帖/小众论文/纯观点/明示传闻)。\n"
    '只输出 JSON:{"worthy": true/false, "reason": "一句话理由"}'
)


def judge_is_worthy(headline: str, body: str, settings: "Settings") -> tuple[bool, str]:
    """单条判够不够格当要闻。返回 (is_worthy, reason)。

    🔴 脏返回 / 字段缺失 / None → is_worthy=True(留,保护真新闻不被故障误踢)。
    LLM 失败/无 key 由 complete_json 冒泡,调用方(工厂)兜成 True(留)。
    """
    cfg = settings.threads
    body = (body or "")[:_BODY_TRUNCATE]  # 全文级 snippet 截到判官口径(见 _BODY_TRUNCATE 注)
    user = (
        f"标题:{headline}\n正文:{body}\n\n"
        '只输出 JSON:{"worthy": true/false, "reason": "一句话理由"}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="worthiness_judge")
    )
    # 严格:只有明确 worthy is False 才算不够格;脏值/缺失 → True(留,保护真新闻)
    is_worthy = out.get("worthy") is not False
    reason = str(out.get("reason", ""))[:120]
    return is_worthy, reason


JUDGE_NAME = "worthiness"  # 判决缓存(S1)标识


def worthiness_prompt_hash(settings: "Settings") -> str:
    """够格闸失效键 = _SYSTEM + 模型/max_tokens/票数(同 magnitude 纪律,换口径即失效)。"""
    cfg = settings.threads
    votes = max(1, getattr(settings.rank.event_pool, "worthiness_judge_votes", 3))
    return prompt_hash_of(_SYSTEM, model=cfg.judge_model,
                          max_tokens=cfg.judge_max_tokens, votes=votes,
                          extra=f"trunc={_BODY_TRUNCATE}")


def worthiness_item_hash(ev: dict) -> str:
    """够格闸喂 LLM 的确切输入(headline + snippet)的内容哈希 = 缓存 item_hash。

    与 judge_is_worthy 读同一份内容(headline + **截断后**正文,口径逐字一致),board-无关,可跨板缓存。
    """
    return hash_input((ev.get("headline", "") or "") + "\x00"
                      + (ev.get("snippet", "") or "")[:_BODY_TRUNCATE])


def make_worthiness_judge(
    settings: "Settings", *,
    judgment_cache: dict[str, dict] | None = None,
    new_verdicts: list[dict] | None = None,
) -> Callable[[dict], bool]:
    """工厂:返回 (event_dict) -> is_worthy 回调,带成本闸 + 缓存 + 多数票 + fail-safe。

    🔴 方向=默认踢:某条留需多数票判 worthy;凑不齐多数=踢(纯)。
    🔴 fail-safe=留:LLM 失败/超预算/脏返回 → 该票算 worthy(绝不因故障误杀真新闻)。
    被判不够格的打 `worthiness.dropped` 供盲考官验"误杀了没"。
    判决缓存(S1,默认关):`judgment_cache` 命中直接读裁决不调 LLM;miss 记入 `new_verdicts` 待写回。
    """
    ep = settings.rank.event_pool
    calls = [0]
    cache: dict[str, bool] = {}
    # 并发判(gate_pool.map_judge)下 calls/cache 会被多线程碰:锁守护成本闸计数与缓存读写,
    # max_worthiness_judges_per_run 仍是精确硬顶(LLM 调用本身在锁外,不串行化)。
    lock = threading.Lock()
    votes = max(1, getattr(ep, "worthiness_judge_votes", 3))
    need = votes // 2 + 1  # 留需够格严格多数(3→2);凑不齐=踢
    prompt_hash = worthiness_prompt_hash(settings)  # 失效键(含模型/口径),建工厂时定一次

    def judge(ev: dict) -> bool:
        key = ev.get("rep_item_id") or ev.get("headline", "")
        with lock:
            if key in cache:
                return cache[key]
        # 持久判决缓存(S1):同内容 + 同 prompt 上一轮判过 → 直接读裁决,不调 LLM(治 f20)。
        ihash = worthiness_item_hash(ev) if (judgment_cache is not None or new_verdicts is not None) else None
        if judgment_cache is not None and ihash in judgment_cache:
            is_worthy = judgment_cache[ihash].get("worthy") is not False  # 与 judge_is_worthy 同向:脏/缺=留
            with lock:
                cache[key] = is_worthy
            return is_worthy
        worthy_votes, cast, last_reason = 0, 0, ""
        dirty = False  # 任一票故障 / 成本闸截断 = 结果被污染,不缓存(见下方缓存说明)
        for i in range(votes):
            with lock:
                if calls[0] >= ep.max_worthiness_judges_per_run:
                    # 成本闸到顶:不再补票(2026-07-01 纯优先改),只用已投的票决——见下方 fail-closed 说明。
                    dirty = True  # 🔴 预算截断的"踢"是可见损失(见缓存说明),标脏不缓存
                    break
                calls[0] += 1
            cast += 1
            try:
                w, reason = judge_is_worthy(
                    ev.get("headline", "") or "", ev.get("snippet", "") or "", settings)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,判官绝不吞成 fail-safe 票(2026-07-02 E1)
            except Exception as exc:  # noqa: BLE001 — LLM 失败=该票算 worthy(留,绝不因故障误杀)
                log.warning("worthiness.judge_failed", error=str(exc),
                            headline=(ev.get("headline", "") or "")[:60])
                w, reason = True, ""
                dirty = True  # 该票是故障兜底非真判 → 不缓存(下轮重判)
            if w:
                worthy_votes += 1
            else:
                last_reason = reason
            if worthy_votes >= need:
                break  # 已够多数够格 → 留
            if worthy_votes + (votes - (i + 1)) < need:
                break  # 剩余票也凑不齐够格多数 → 踢(省 token)
        # 🔴 预算耗尽 fail-closed(2026-07-01,纯优先"宁缺毋滥"):不把未投的票补成 worthy,只按实投的够格票决
        #    ——预算耗尽的尾部宁少报,绝不补成"够格"把边缘货塞满板(rank4:靠后板 geo 被前板耗尽预算后整段放行)。
        #    注:这与"LLM 单条失败→该票算 worthy"(:98-101)不同——那护单条真新闻不因故障误杀,此处护"纯"不放水。
        is_worthy = worthy_votes >= need
        with lock:
            cache[key] = is_worthy
            # 🔴 判决缓存(S1,考官 2026-07-03 逮到):worthiness 的**预算耗尽**方向是 fail-closed=踢(可见损失,
            #    同 board),若把"一次预算饿死导致的踢"缓存下来,真够格新闻会被永久误杀(缓存 append-only 不自愈)。
            #    故只缓存**全程干净票 + 无成本闸截断**判出的裁决(dirty 时跳过,下轮重判)。LLM 单条故障→worthy(留)
            #    虽是安全方向,也一并标脏不缓存,保持"只缓存真判"不变式统一(与 board/same_event 同纪律)。
            if new_verdicts is not None and ihash is not None and not dirty:
                new_verdicts.append(make_row(ihash, JUDGE_NAME, prompt_hash, {"worthy": is_worthy}))
        if not is_worthy:
            log.info("worthiness.dropped", votes=f"{worthy_votes}/{cast}",
                     headline=(ev.get("headline", "") or "")[:60], reason=last_reason)
        return is_worthy

    return judge


def filter_unworthy(board_evs: list[dict], judge: Callable[[dict], bool] | None,
                    *, top_n: int, final_limit: int) -> list[dict]:
    """要闻够格闸(纯过滤,LLM 锁在 judge):对 heat 头部判够格,**只留判过且够格的**。

    judge=None(闸关)→ 原样返回(零行为变化)。
    effective_top_n = max(top_n, final_limit + 5):覆盖会进 final_limit 的头部 + buffer。
    🔴 纯语义(闭 codex MEDIUM1,2026-06-29):**只返回判过且够格的头部条目,未判的尾部一律不要**——
    否则头部够格的不足 final_limit 时,下游 apply_event_quotas 会从未判尾部凑数,放不够格的进榜(defeat 纯)。
    尾部 heat 比头部低、本就进不了 final_limit(head ⊇ top-final_limit),丢掉不损失终榜、且实现"没有就少报"。
    🔴 judge(e) 抛异常 → 留(闭 codex MINOR;与 judge 内部 fail-safe 同向,绝不因故障误杀真新闻)。
    """
    if judge is None:
        return board_evs
    effective_top_n = max(top_n, final_limit + 5)
    head = sorted(board_evs, key=lambda e: e.get("heat_score", 0.0), reverse=True)[:effective_top_n]

    def _worthy(e: dict) -> bool:
        try:
            return judge(e)
        except PermanentLLMError:
            raise  # 没钱/凭证失效:熔断整跑,绝不吞成 fail-safe 留(2026-07-02 E1)
        except Exception:  # noqa: BLE001 — 判官异常=留(fail-safe,绝不因故障拖垮选稿/误杀真新闻)
            log.warning("worthiness.filter_error", headline=(e.get("headline", "") or "")[:60])
            return True

    # 并发判(2026-07-02 提速批):verdicts 与 head 同序,语义 = [judge(e) for e in head]
    verdicts = map_judge(_worthy, head)
    return [e for e, w in zip(head, verdicts) if w]
