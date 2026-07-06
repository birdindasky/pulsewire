"""events.allocate —— 全局事件池 → 板块分配 + 限额(DESIGN §6;复刻 apply_quotas 防刷屏,闭 codex M5)。

① assign_board:每事件按相关度 argmax 分到**一个**板块(须过相关性闸),都不过=不进任何板块。
   因聚类是**全局一次**(柱③),同一事件早已合成一个 event → 一事件只分一个板块,**跨板块重复由构造消除**
   (无需事后 cross-board 扫描;§6.4 那道兜底留作未来保险)。
② apply_event_quotas:板块内按 heat_score 降序贪心,复刻 RankCfg 的 per_category/per_source/old_item 限额
   + **源族折叠**(代表源按 score.source_family 归族,防 google-news 镜像刷屏),取前 final_limit。
全纯函数(无 LLM/DB,可测)。事件以 dict 表示(键见下),便于单测与 engine 解耦。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from pulsewire.events.score import passes_relevance_gate, source_family

if TYPE_CHECKING:
    from pulsewire.config.models import EventPoolCfg, RankCfg

# 显著实体提取(同事件去重候选门用):标题/主体里的专名(大写起头、≥3 字符的品牌/机构/地名/人名)。
# 用途:两张卡共享显著实体(同公司/同机构)→ 交判官裁决是不是"同一件事换个角度"(治全局聚类漏的低余弦同事件,
# 如 SpaceX「收 Cursor」vs「市值超亚马逊」judge==同却被当两张端上来)。**不是**实体限张数闸——判官说不同就都留
# (用户明示:同公司不设闸,只杀同事件)。泛词/2 字母缩写(US/UK/EU…天然因 ≥3 长度被剔)入停用表降噪。
_ENTITY_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from",
    "as", "is", "are", "be", "its", "it", "this", "that", "these", "those", "new", "says",
    "said", "say", "after", "over", "amid", "into", "out", "off", "you", "your", "need", "know",
    "how", "why", "what", "who", "when", "will", "can", "could", "may", "might", "not", "more",
    "most", "than", "then", "now", "just", "still", "amp", "ceo", "cfo", "ipo", "gdp",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december",
}
_CAP_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.&\-]{2,}")


def _salient_entities(text: str) -> set[str]:
    out: set[str] = set()
    for m in _CAP_TOKEN.finditer(text or ""):
        tok = m.group(0)
        if not tok[0].isupper():  # 只取大写起头(专名/品牌)
            continue
        low = tok.lower().strip(".")
        if low and low not in _ENTITY_STOP:
            out.add(low)
    return out


def shares_salient_entity(a: dict, b: dict) -> bool:
    """两事件的代表标题/主体短语是否共享显著实体(同公司/机构/地名)。仅作判官候选门,判同与否仍由判官定。"""
    ea = _salient_entities(a.get("headline", "")) | _salient_entities(a.get("subject", ""))
    eb = _salient_entities(b.get("headline", "")) | _salient_entities(b.get("subject", ""))
    return bool(ea & eb)

# 事件 dict 约定键:
#   relevance: dict[board, float]  各板块相关度
#   heat_score: float              热度(排序主轴)
#   is_magnitude: bool             白名单实体一手首发(豁免相关性闸到 τ_floor)
#   category: str | None           代表条目类目(per_category 限额)
#   representative_source: str|None 代表源(per_source 限额,折叠源族)
#   peak_at: datetime | None       主峰(old_item 判定)


def assign_board(event: dict, *, cfg: EventPoolCfg) -> str | None:
    """按相关度 argmax 分到一个板块;须过相关性闸(量级实体降到 τ_floor)。都不过→None。"""
    relevance: dict[str, float] = event.get("relevance") or {}
    if not relevance:
        return None
    board, score = max(relevance.items(), key=lambda kv: kv[1])
    if passes_relevance_gate(score, is_magnitude=bool(event.get("is_magnitude")), cfg=cfg):
        return board
    return None


def _age_hours(peak_at: datetime | None, now: datetime) -> float:
    if peak_at is None:
        return 0.0  # 无日期不当老项(新鲜度硬窗另管;此处只防"老项占位")
    return (now - peak_at).total_seconds() / 3600.0


def apply_event_quotas(
    events: list[dict], *, final_limit: int, cfg: RankCfg, now: datetime, same_event=None
) -> list[dict]:
    """板块内限额 + **同事件去重兜底**(复刻 apply_quotas):heat_score 降序贪心 + 各类/单源族/老项限额。

    **同事件去重(治全局聚类 recall~0.87 漏的"同事件换个角度")**:与已选事件配对,凡**主体向量余弦
    ≥ event_dedup_min_sim** 或 **共享显著实体**(同公司/机构,见 shares_salient_entity)→ 交 `same_event`
    判官裁决,判同才折叠(留先到的高热那张)。判官预算用尽(event_dedup_max_judges)后保守不折叠。
    **注意**:共享实体只是"送判官"的候选门,**不是实体限张数**——判官说不同就都留(同公司多件不同真事全保;
    用户明示「同实体不设闸」)。same_event=None(纯函数单测)=退回纯余弦阈值(≥select_sim_dedup 折叠)。
    same_event:回调 (ev_a, ev_b)->bool;回调内锁 LLM,本函数保持可测(注入假回调)。
    """
    from pulsewire.threads.subject import cosine

    kept: list[dict] = []
    kept_vecs: list = []  # 与 kept 同序的 subject_vec
    per_cat: dict[str, int] = {}
    per_src: dict[str, int] = {}
    old_count = 0
    acad_count = 0  # 已选纯学术论文数(arxiv 等)
    judges_used = 0
    acad_prefixes = tuple(getattr(cfg, "academic_source_prefixes", None) or ())
    acad_limit = getattr(cfg, "academic_paper_limit", 0)
    for ev in sorted(events, key=lambda e: e.get("heat_score", 0.0), reverse=True):
        cat = ev.get("category") or "_none"
        if per_cat.get(cat, 0) >= cfg.per_category_limit:
            continue
        src = source_family(ev.get("representative_source")) or "_none"  # 源族折叠(防镜像刷屏)
        if per_src.get(src, 0) >= cfg.per_source_limit:
            continue
        # 硬核学术论文限额(够前沿×够看懂的折中):纯论文源(arxiv-*)每板块最多 acad_limit 条
        is_acad = bool(acad_limit) and (ev.get("representative_source") or "").lower().startswith(acad_prefixes)
        if is_acad and acad_count >= acad_limit:
            continue
        is_old = _age_hours(ev.get("peak_at"), now) > cfg.old_item_age_hours
        if is_old and old_count >= cfg.old_item_limit:
            continue
        # 同事件去重兜底:候选门(余弦近 ∪ 共享实体)圈出"可能同事件"的对,判官全权裁决是否折叠。
        v = ev.get("subject_vec")
        dup = False
        for kv, kept_ev in zip(kept_vecs, kept):
            c = cosine(v, kv) if (v is not None and kv is not None) else 0.0
            if same_event is not None:
                related = c >= cfg.event_dedup_min_sim or shares_salient_entity(ev, kept_ev)
                if related and judges_used < cfg.event_dedup_max_judges:
                    judges_used += 1
                    if same_event(ev, kept_ev):
                        dup = True
                        break
                # 预算耗尽=保守不折叠(绝不盲合)
            elif c >= cfg.select_sim_dedup:  # 无判官(纯函数单测):退回纯余弦阈值
                dup = True
                break
        if dup:
            continue
        kept.append(ev)
        kept_vecs.append(v)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        per_src[src] = per_src.get(src, 0) + 1
        if is_acad:
            acad_count += 1
        if is_old:
            old_count += 1
        if len(kept) >= final_limit:
            break
    return kept
