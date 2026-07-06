"""语义问答引擎(v2 主线B②):大白话问 → 召回真历史卡 → LLM 引用式回答。见 docs/DESIGN.md §3。

链:embed_query(问题) → recall_cards_by_vector(summaries.card_vec) → 组证据清单 → LLM 只据证据答+标源
→ 校验引用卡号真实 → 返回 {answer, used, cards}。

铁律(pulsewire 零编造基因,同数字回源):
- 回答只准用召回到的真卡,每句标源 [n];证据没提到的不许补。
- 召回为空/全低于 τ_qa → 直接"没找到",不调 LLM(省钱 + 杜绝无证据硬答)。
- LLM 失败/无 key/超时 → 报不可用,**绝不编一个答案**(失败冒泡不静默)。
- used 卡号越界/虚构 → 判失败、降级"没找到",绝不把可能编的展示。
  ⚠️ 运行时只能挡"卡号不存在";挡不住"填合法卡号但 answer 借题发挥编内容"(RAG 头号失效模式,
  codex F3)——那条只能靠独立考官逐句追溯证据,见 docs/DESIGN.md §3。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pulsewire.config import get_settings
from pulsewire.dedup.embedding import get_embedder
from pulsewire.obs import get_logger
from pulsewire.store import get_sessionmaker
from pulsewire.store.repo import recall_cards_by_vector
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.llm import complete_json

if TYPE_CHECKING:
    from pulsewire.store.tables import Summary

log = get_logger()

_SYSTEM = (
    "你是历史档案问答助手。**只能依据下面【证据】里的卡作答**——每个论断后用 [n] 标出处卡号。\n"
    "证据里没提到的,一个字都不许补(不许用你自己的知识、不许推测、不许借题发挥)。\n"
    "证据不足以回答这个问题 → enough 设 false、answer 留空。\n"
    "用大白话答(外行一遍看懂),简洁。\n"
    '只输出 JSON:{"enough": true/false, "answer": "带 [n] 标注的回答", "used": [引用到的卡号]}'
)

_NOT_FOUND = "档案里没找到相关内容。"
_UNAVAILABLE = "问答暂时不可用(稍后再试或先开 Docker)。"


def format_evidence(cards: list[tuple[Summary, float]]) -> str:
    """召回的卡 → 编号证据清单(喂 LLM)。卡号从 1 起,与 used 校验对齐。"""
    lines = []
    for i, (s, _sim) in enumerate(cards, 1):
        date = s.created_at.strftime("%Y-%m-%d") if s.created_at else "?"
        body = (s.tldr_rendered or "").strip()
        insight = (s.insight_rendered or "").strip()
        chunk = f"[{i}] ({date}) {s.headline}\n    {body}"
        if insight:
            chunk += f"\n    {insight[:300]}"
        lines.append(chunk)
    return "\n\n".join(lines)


def parse_validate(raw: str, n_cards: int) -> dict:
    """解析 LLM 输出 + 零编造校验。返回 {enough, answer, used}。

    严格:enough 必须 is True;used 必须非空且全部在 [1, n_cards](越界/虚构=判不达标→没找到)。
    任一不满足 → enough=False(降级,绝不把可能编的展示)。
    parse_json 对空/坏输入会抛(json.loads;deepseek 偶发返空,见 reasoning-model-maxtokens-empty)→ 兜成降级。
    """
    try:
        out = parse_json(raw) or {}
    except Exception:  # noqa: BLE001 — LLM 返空/坏 JSON=不可信→降级没找到,绝不崩、绝不编
        return {"enough": False, "answer": "", "used": []}
    enough = out.get("enough") is True
    used_raw = out.get("used") or []
    answer = str(out.get("answer", "")).strip()
    # used 必须是 1..n_cards 范围内的整数;越界/非整数/虚构 → 校验失败
    used = []
    valid = True
    if not isinstance(used_raw, list) or not used_raw:
        valid = False
    else:
        for u in used_raw:
            if isinstance(u, bool) or not isinstance(u, int) or u < 1 or u > n_cards:
                valid = False
                break
            used.append(u)
    if not (enough and valid and answer):
        return {"enough": False, "answer": "", "used": []}
    return {"enough": True, "answer": answer, "used": used}


async def answer(question: str, *, settings=None) -> dict:
    """问一句 → 引用式回答。返回 {ok, enough, answer, cards:[{n,headline,date,item_id}], error?}。

    cards 只含被引用(used)的真卡,供回链展示。任何"没找到/不可用"路径都不返回编造内容。
    """
    settings = settings or get_settings()
    qa = settings.qa
    q = (question or "").strip()
    if not q:
        return {"ok": True, "enough": False, "answer": _NOT_FOUND, "cards": []}

    # 1) 召回(零成本,本地向量)
    try:
        embedder = get_embedder(settings)
        qvec = embedder.embed_query(q)
        sm = get_sessionmaker()
        async with sm() as session:
            cards = await recall_cards_by_vector(
                session, vector=qvec, limit=qa.top_k, relevance_floor=qa.relevance_floor)
    except Exception as exc:  # noqa: BLE001 — 失败冒泡不静默:报不可用,不编
        log.error("qa.recall_failed", error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "enough": False, "answer": _UNAVAILABLE, "cards": [],
                "error": "recall_failed"}

    if not cards:  # 召回空/全低于 τ_qa → 直接没找到,不调 LLM
        log.info("qa.no_recall", question=q[:60])
        return {"ok": True, "enough": False, "answer": _NOT_FOUND, "cards": []}

    cards = cards[: qa.max_context_cards]
    evidence = format_evidence(cards)

    # 2) 引用式回答(调 LLM)
    user = f"【证据】\n{evidence}\n\n【问题】{q}\n\n只输出 JSON。"
    try:
        raw = complete_json(_SYSTEM, user, model=qa.answer_model,
                            max_tokens=qa.answer_max_tokens, settings=settings, stage="qa_answer")
    except Exception as exc:  # noqa: BLE001 — LLM 挂=报不可用,绝不编
        log.error("qa.llm_failed", error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "enough": False, "answer": _UNAVAILABLE, "cards": [],
                "error": "llm_failed"}

    # 3) 零编造校验
    res = parse_validate(raw, len(cards))
    if not res["enough"]:
        log.info("qa.not_enough", question=q[:60])
        return {"ok": True, "enough": False, "answer": _NOT_FOUND, "cards": []}

    # 4) 回链:只回被引用的真卡(n→headline/date/item_id)
    used_cards = []
    for n in res["used"]:
        s, sim = cards[n - 1]
        used_cards.append({
            "n": n, "headline": s.headline, "item_id": s.item_id,
            "date": s.created_at.strftime("%Y-%m-%d") if s.created_at else None,
            "similarity": round(sim, 4),
        })
    log.info("qa.answered", question=q[:60], used=len(used_cards))
    return {"ok": True, "enough": True, "answer": res["answer"], "cards": used_cards}
