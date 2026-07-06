"""语义同事件判官(2026-06-16 方案 B):报告选稿层,对"中等相似"配对多问一次 LLM。

词法/embedding 折叠只压得住"措辞接近"的重复;同一件事换个角度写(停火「敲定」vs「细节曝光」)
余弦岔得开、压不住,就在日报里连出两条(用户一票否决:相同内容连着留)。这里只对落在
[event_dedup_min_sim, select_sim_dedup) 带的配对补一道语义判:是同一件事→折叠。

铁律护栏(宁可漏合也别误合):判官保守(拿不准=不同);LLM 失败/无 key/超成本闸 → 不折叠
(退回纯词法现状,绝不因判官出错而把不同的事强行合并)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.llm import complete_json

if TYPE_CHECKING:
    from pulsewire.config import Settings
    from pulsewire.rank.engine import Candidate

log = get_logger()

_SYSTEM = (
    "你是新闻去重编辑。给你两条新闻标题,判断它们是不是**同一件事**。\n"
    "判准:同一事件(同一时间、同一主角、同一进展)的不同报道/不同角度 = 同一件事(SAME),"
    "哪怕一条讲『达成』、另一条讲『细节/影响/反应』;\n"
    "只是同一主体的不同事、或同一领域的不同事件 = 不同(DIFF)。\n"
    "拿不准时一律判 DIFF(宁可让两条都留下,也绝不把不同的事误合)。\n"
    '只输出 JSON:{"same": true/false}'
)


def judge_same_event(title_a: str, title_b: str, settings: "Settings") -> bool:
    """两标题是否同一件事。失败/无 key 冒泡给调用方兜成 False(保守不折叠)。"""
    cfg = settings.threads  # 复用「在追」判官模型(同是"同一故事吗"语义判)
    user = f"标题A:{title_a}\n标题B:{title_b}\n\n只输出 JSON:{{\"same\": true/false}}"
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="select_dedup_judge")
    )
    return bool(out.get("same", False))


def make_same_event_judge(settings: "Settings") -> Callable[["Candidate", "Candidate"], bool]:
    """工厂:返回 (cand_a, cand_b) -> bool 回调,带成本闸 + 缓存 + fail-safe。

    注入给 apply_quotas(纯函数),LLM/IO 全锁在这里,纯函数仍可用假回调测。
    """
    cfg = settings.rank
    calls = [0]
    cache: dict[tuple[str, str], bool] = {}

    def judge(a: "Candidate", b: "Candidate") -> bool:
        key = (a.item_id, b.item_id) if a.item_id <= b.item_id else (b.item_id, a.item_id)
        if key in cache:
            return cache[key]
        if calls[0] >= cfg.event_dedup_max_judges:
            return False  # 成本闸到顶 → 保守不折叠
        calls[0] += 1
        try:
            same = judge_same_event(a.title or "", b.title or "", settings)
        except Exception as exc:  # noqa: BLE001 — 判官是增强,失败=不折叠,绝不拖垮选稿
            log.warning("rank.event_dedup.judge_failed", error=str(exc))
            same = False
        cache[key] = same
        if same:
            log.info("rank.event_dedup.folded", a=(a.title or "")[:40], b=(b.title or "")[:40])
        return same

    return judge
