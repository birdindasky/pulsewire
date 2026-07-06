"""已剪记忆闸(clip memory,2026-07-05 用户拍板"全上"):治日报逐日高度重复(实测相邻两日重合 39%)。

病根:选稿引擎对"昨天已经剪报过什么"零记忆——AI 板新鲜窗放宽到 144h 后,一桩大事连续多天
都在窗内、都够热,天天被重新选中、重新写稿,读者连续几天看到几乎一样的剪报。

账本 = threads(在追线)+ thread_clusters(逐日挂线痕:progress_date/headline 冻结落痕)。
threads 归线只处理"当天真正入选出稿"的簇,所以 thread_clusters 天然就是"已剪台账",零新表。

三味药(全上,本模块承 ①,②③ 见 render/deliver 与 summarize):
① 选稿闸(engine._board_select 首道):候选事件的成员簇命中账本 = 此事已剪过 →
   材料全旧(peak_at 早于上次已剪日零点)直接踢;有新材料 → novelty 判官判"有没有值得
   再剪一刀的新进展",没有 → 踢(腾位给真新事),有 → 留、下游盖"追·第N天"章。
② 前端红章:PNG 剪报卡 / webapp / 飞书卡在已追事件上标"追·第N天"。
③ 增量写稿:summarize 对在追事件带前情上下文,只写新进展不复述前情。

铁律护栏(与五判官同纪律):
- 🔴 fail-open:账本查询失败 / 判官故障 / 脏返回 / 成本闸到顶 → 一律当"有新进展"留下
  (最坏 = 回到今天的重复现状,绝不因故障误杀真新闻)。注意与 worthiness 的预算耗尽
  fail-closed 相反:那里放行=放水伤"纯",这里砍掉=丢真新闻,方向各护各的命门。
  PermanentLLMError(没钱/凭证失效)仍熔断整跑,绝不吞票。
- 🔴 重跑安全:线**今天已挂过**(linked_today;--force 重跑 / threads 站已跑完的场景)→
  该事件跳过闸直接留——它就是今天已入选的那条,线的 summary 已被今天自己的稿刷新,
  拿"今天自己的稿"当前情自比必判"无新进展",会把合法入选的事件在重跑时误杀。
- 🔴 判决缓存(S1)同纪律:键 = 喂给 LLM 的确切文本(前情截断 + 标题 + 截断正文),
  prompt_hash 折模型/口径;只缓存全程干净票(故障兜底票/预算截断不缓存)。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

from pulsewire.events.gate_pool import map_judge
from pulsewire.events.judge_cache import hash_input, make_row, prompt_hash_of
from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.llm import complete_json

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pulsewire.config import Settings

log = get_logger()

JUDGE_NAME = "novelty"  # 判决缓存(S1)标识

# 判官口径(改动即换 prompt_hash 自然失效):正文截 800 同 magnitude/worthiness;
# 前情截 400——判官只需要"读者已知什么"的梗概,不需要整篇旧稿。
_BODY_TRUNCATE = 800
_PREV_TRUNCATE = 400


def logical_date(run_id: str | None, tz: ZoneInfo) -> str:
    """run_id(<trigger>_<YYYYMMDD>)→ 逻辑日 YYYY-MM-DD;解析不出退当下本地日期。

    与 deliver._logical_date 同口径(治 f05 系跨午夜错位):晚间补课跨过午夜时,
    "今天"必须锚到 run 的逻辑日,否则账本会把 run 当天的挂线痕误当"昨日已剪"。
    """
    if run_id:
        tail = run_id.rsplit("_", 1)[-1]
        if len(tail) == 8 and tail.isdigit():
            return f"{tail[:4]}-{tail[4:6]}-{tail[6:8]}"
    return datetime.now(tz).strftime("%Y-%m-%d")


async def load_clip_ledger(
    session: "AsyncSession", cluster_ids: list[str], *, today: str, window_days: int
) -> dict[str, dict]:
    """查已剪台账:成员簇 → 所属在追线的既往剪报记录。返回 {cluster_id: rec}。

    rec = {thread_id, days_prior(今天之前已剪的不同日期数), last_date(最近已剪日 YYYY-MM-DD),
           prev_text(前情:线现状一句话,兜底最近挂线痕 headline), linked_today(线今天已挂过)}。
    只认 last_date 在 [today-window_days, today) 内的线——更久远的旧事重浮 = 当新事重报。
    progress_date 是 YYYY-MM-DD 字符串,字典序即时间序。
    """
    from sqlalchemy import select

    from pulsewire.store.tables import Thread, ThreadCluster

    cids = sorted(set(c for c in cluster_ids if c))
    if not cids:
        return {}
    hit_rows = (await session.execute(
        select(ThreadCluster.cluster_id, ThreadCluster.thread_id)
        .where(ThreadCluster.cluster_id.in_(cids))
    )).all()
    if not hit_rows:
        return {}
    tids = sorted({tid for _c, tid in hit_rows})

    # 线的全部挂线痕:算已剪天数 / 最近已剪日 / 前情标题(headline 是挂线时冻结的当日文案)
    trows = (await session.execute(
        select(ThreadCluster.thread_id, ThreadCluster.progress_date, ThreadCluster.headline)
        .where(ThreadCluster.thread_id.in_(tids), ThreadCluster.progress_date.isnot(None))
        .order_by(ThreadCluster.progress_date)
    )).all()
    threads = {t.thread_id: t for t in (await session.execute(
        select(Thread).where(Thread.thread_id.in_(tids))
    )).scalars()}

    floor = (date.fromisoformat(today) - timedelta(days=window_days)).isoformat()
    per_thread: dict[str, dict] = {}
    for tid in tids:
        mine = [(d, h) for t, d, h in trows if t == tid and d]
        prior = sorted({d for d, _h in mine if d < today})
        if not prior or prior[-1] < floor:
            continue  # 无既往剪报 / 已出记忆窗 → 不进账本
        linked_today = any(d >= today for d, _h in mine)
        last_headline = next((h for d, h in reversed(mine) if d < today and h), None)
        th = threads.get(tid)
        # 前情:线的"现状一句话"(每次挂线用当日成稿刷新 = 上次已剪的 tldr)。线今天已挂过时
        # summary 已是今天自己的稿,退回最近既往挂线痕的 headline(冻结值,不会被今天刷掉)。
        prev_text = None
        if th is not None and th.summary and not linked_today:
            prev_text = th.summary
        prev_text = prev_text or last_headline or (th.name if th is not None else None) or ""
        per_thread[tid] = {
            "thread_id": tid, "days_prior": len(prior), "last_date": prior[-1],
            "prev_text": prev_text, "linked_today": linked_today,
        }

    out: dict[str, dict] = {}
    for cid, tid in hit_rows:
        rec = per_thread.get(tid)
        if rec and (cid not in out or rec["last_date"] > out[cid]["last_date"]):
            out[cid] = rec
    return out


def annotate_prev_reports(events: list[dict], ledger: dict[str, dict], *, tz: ZoneInfo) -> int:
    """把账本命中标到事件上:ev["prev_report"] = rec + stale_material。返回标记数。

    stale_material(材料全旧,确定性踢、零 LLM):事件最新材料(peak_at)早于"上次已剪日
    **零点**"(应用时区)——连上次已剪那天都没到的陈料,必然已被上次剪报覆盖。取零点而非
    交付钟点是保守方向:上次已剪日**当天**发布的材料可能晚于交付、没被覆盖,一律交判官,
    绝不确定性误杀。peak_at 缺失 → 不判旧(交判官,fail-open)。
    """
    n = 0
    for ev in events:
        recs = [ledger[c] for c in ev.get("member_cluster_ids", ()) if c in ledger]
        if not recs:
            continue
        rec = max(recs, key=lambda r: r["last_date"])
        stale = False
        peak = ev.get("peak_at")
        if not rec["linked_today"] and peak is not None:
            cutoff = datetime.fromisoformat(rec["last_date"]).replace(tzinfo=tz)
            stale = peak < cutoff
        ev["prev_report"] = {**rec, "stale_material": stale}
        n += 1
    return n


_SYSTEM = (
    "你是日报主编。这条新闻**此前已经在本日报剪报过**,读者已经知道『前情』里的内容。\n"
    "现在给你今天抓到的最新报道材料,判断:**相比前情,今天有没有值得再剪一刀的新进展?**\n"
    "**有新进展(new=true):**新的事实/数字/官方表态/关键人物动作、事态升级或反转、"
    "此前传闻现在坐实(或被正式否认)、有分量的后续(裁决/发布/落地/规模或伤亡显著变化)。\n"
    "**没有新进展(new=false):**今天的材料与前情基本一回事——同一事实的复述、跟风转载、"
    "换一家媒体再报一遍、只有措辞差异或无关痛痒的细节补充、纯回顾盘点。\n"
    "⚠️ 拿不准就 new=true(宁可多报一天,绝不漏掉真进展)。\n"
    '只输出 JSON:{"new": true/false, "reason": "一句话理由"}'
)


def judge_has_new(prev_text: str, headline: str, body: str, settings: "Settings") -> tuple[bool, str]:
    """单票判"相比前情有没有新进展"。返回 (has_new, reason)。

    🔴 脏返回 / 字段缺失 → has_new=True(留;绝不因脏票误杀)。LLM 失败由 complete_json
    冒泡,调用方(工厂)兜成 True(留)。
    """
    cfg = settings.threads
    prev = (prev_text or "")[:_PREV_TRUNCATE]
    body = (body or "")[:_BODY_TRUNCATE]
    user = (
        f"前情(本日报已剪报过的内容):{prev}\n\n"
        f"今天的材料:\n标题:{headline}\n正文:{body}\n\n"
        '只输出 JSON:{"new": true/false, "reason": "一句话理由"}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="novelty_judge")
    )
    has_new = out.get("new") is not False  # 只有明确 false 才算"无新进展";脏/缺 → 留
    return has_new, str(out.get("reason", ""))[:120]


def novelty_prompt_hash(settings: "Settings") -> str:
    """失效键 = _SYSTEM + 模型/max_tokens/票数 + 截断口径(同 magnitude/worthiness 纪律)。"""
    cfg = settings.threads
    votes = max(1, getattr(settings.rank.event_pool, "novelty_judge_votes", 3))
    return prompt_hash_of(_SYSTEM, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                          votes=votes, extra=f"trunc={_BODY_TRUNCATE},prev={_PREV_TRUNCATE}")


def novelty_item_hash(ev: dict) -> str:
    """缓存 item_hash = 喂 LLM 的确切文本(前情截断 + headline + 截断正文)的内容哈希。

    前情进键(与其它闸不同):同一事件、同一批今日材料,前情变了(昨天又剪了一天)裁决就
    该重判——"相比 7-03 的稿没新进展"与"相比 7-04 的稿"是两个问题。
    """
    pr = ev.get("prev_report") or {}
    return hash_input(
        (pr.get("prev_text", "") or "")[:_PREV_TRUNCATE] + "\x00"
        + (ev.get("headline", "") or "") + "\x00"
        + (ev.get("snippet", "") or "")[:_BODY_TRUNCATE]
    )


def make_novelty_judge(
    settings: "Settings", *,
    judgment_cache: dict[str, dict] | None = None,
    new_verdicts: list[dict] | None = None,
) -> Callable[[dict], bool]:
    """工厂:返回 (event_dict) -> keep 回调,带成本闸 + S1 缓存 + 多数票 + fail-open。

    🔴 方向 = 默认留:踢需**严格多数**票判"无新进展"(3→2);凑不齐 = 留(宁多报一天别误杀)。
    🔴 fail-open:单票 LLM 失败 → 该票算"有新进展";成本闸到顶 → 剩余未判全留 + 告警
       (与 worthiness fail-closed 相反,见模块 docstring)。
    被踢的打 `clip.repeat.dropped` 日志,供盲考官验"误杀了没"。
    """
    ep = settings.rank.event_pool
    calls = [0]
    cache: dict[str, bool] = {}
    lock = threading.Lock()  # 并发判(gate_pool)下守护成本闸计数/缓存,硬顶仍精确
    votes = max(1, getattr(ep, "novelty_judge_votes", 3))
    need_stale = votes // 2 + 1  # 踢需"无新进展"严格多数
    prompt_hash = novelty_prompt_hash(settings)
    budget_warned = [False]

    def judge(ev: dict) -> bool:
        key = ev.get("rep_item_id") or ev.get("headline", "")
        with lock:
            if key in cache:
                return cache[key]
        pr = ev.get("prev_report") or {}
        ihash = novelty_item_hash(ev) if (judgment_cache is not None or new_verdicts is not None) else None
        if judgment_cache is not None and ihash in judgment_cache:
            keep = judgment_cache[ihash].get("new") is not False  # 与 judge_has_new 同向:脏/缺=留
            with lock:
                cache[key] = keep
            return keep
        new_votes, stale_votes, cast, last_reason = 0, 0, 0, ""
        dirty = False
        for i in range(votes):
            with lock:
                if calls[0] >= ep.max_novelty_judges_per_run:
                    # 🔴 成本闸到顶 = fail-open 留(与 worthiness 相反):这里砍掉=丢真新闻,
                    # 放行=回到重复现状。只告警一次别刷屏;标脏不缓存。
                    dirty = True
                    if not budget_warned[0]:
                        budget_warned[0] = True
                        log.warning("clip.novelty.budget_exhausted",
                                    cap=ep.max_novelty_judges_per_run,
                                    note="novelty 判官预算耗尽,剩余既往事件全留(fail-open)")
                    break
                calls[0] += 1
            cast += 1
            try:
                has_new, reason = judge_has_new(
                    pr.get("prev_text", "") or "", ev.get("headline", "") or "",
                    ev.get("snippet", "") or "", settings)
            except PermanentLLMError:
                raise  # 没钱/凭证失效:熔断整跑,判官绝不吞成 fail-open 票(2026-07-02 E1)
            except Exception as exc:  # noqa: BLE001 — 单票失败=该票算"有新进展"(留)
                log.warning("clip.novelty.judge_failed", error=str(exc),
                            headline=(ev.get("headline", "") or "")[:60])
                has_new, reason = True, ""
                dirty = True  # 故障兜底票非真判 → 不缓存,下轮重判
            if has_new:
                new_votes += 1
            else:
                stale_votes += 1
                last_reason = reason
            if stale_votes >= need_stale:
                break  # "无新进展"已够多数 → 踢
            if stale_votes + (votes - (i + 1)) < need_stale:
                break  # 剩余票也凑不齐踢的多数 → 留(省 token)
        keep = stale_votes < need_stale  # 凑不齐"无新进展"多数(含预算截断的残票)= 留
        with lock:
            cache[key] = keep
            # 只缓存全程干净票(与 same_event/worthiness/board 同纪律):故障兜底票 / 预算
            # 截断都标脏不缓存——缓存 append-only 不自愈,脏"留"会让真该踢的重复天天上榜。
            if new_verdicts is not None and ihash is not None and not dirty:
                new_verdicts.append(make_row(ihash, JUDGE_NAME, prompt_hash, {"new": keep}))
        if not keep:
            log.info("clip.repeat.dropped", reason="no_new_development",
                     votes=f"{stale_votes}/{cast}", days_prior=pr.get("days_prior"),
                     last_date=pr.get("last_date"),
                     headline=(ev.get("headline", "") or "")[:60], judge_reason=last_reason)
        return keep

    return judge


def filter_already_clipped(
    board_evs: list[dict], judge: Callable[[dict], bool] | None,
    *, top_n: int, final_limit: int,
) -> list[dict]:
    """已剪记忆闸(纯过滤,LLM 锁在 judge):踢"已剪过且无新进展"的事件,腾位给新事。

    judge=None(闸关)→ 原样返回(零行为变化)。放在闸链**最前**:踢掉的重复位由后续
    话题/水货/够格闸在更宽的候选面上回填,空出的名额给真新事(而非直接变短)。
    - 未命中账本(无 prev_report)/ 线今天已挂过(linked_today,重跑安全)→ 直接留,零成本。
    - 材料全旧(stale_material)→ 确定性踢,零 LLM(不占 heat 头部名额限制)。
    - 其余既往事件:仅 heat 头部 max(top_n, final_limit+5) 内的花判官预算;头部外原样留
      (heat 低、本就进不了 final_limit,烧判官没意义)。
    🔴 judge 抛异常 → 留(fail-open,与工厂内兜底同向);PermanentLLMError 冒泡熔断。
    """
    if judge is None:
        return board_evs
    effective_top_n = max(top_n, final_limit + 5)
    head_ids = {
        id(e) for e in sorted(board_evs, key=lambda e: e.get("heat_score", 0.0),
                              reverse=True)[:effective_top_n]
    }

    def _keep(e: dict) -> bool:
        pr = e.get("prev_report")
        if not pr or pr.get("linked_today"):
            return True
        if pr.get("stale_material"):
            log.info("clip.repeat.dropped", reason="stale_material",
                     days_prior=pr.get("days_prior"), last_date=pr.get("last_date"),
                     headline=(e.get("headline", "") or "")[:60])
            return False
        if id(e) not in head_ids:
            return True
        try:
            return judge(e)
        except PermanentLLMError:
            raise  # 没钱/凭证失效:熔断整跑
        except Exception:  # noqa: BLE001 — 判官异常=留(fail-open,绝不因故障误杀)
            log.warning("clip.filter_error", headline=(e.get("headline", "") or "")[:60])
            return True

    verdicts = map_judge(_keep, board_evs)  # 并发判,按原序返回(语义=[_keep(e) for e ...])
    return [e for e, k in zip(board_evs, verdicts) if k]
