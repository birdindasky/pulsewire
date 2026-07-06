"""事件线 B 层:AI 判官——给定新报道 + A 缩出的候选线,判它接哪条线还是新开。

只看 A 预过滤后的候选(同主体、数量少),天然防"啥都往一条线塞";拿不准倾向新开。
失败由 engine 降级为只信 A。见 docs/DESIGN.md §4。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json
from pulsewire.threads.llm import complete_json

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_SYSTEM = (
    "你是新闻「事件线」编辑。给你一条**新报道**和几条**正在追踪的事件线**,判断这条新报道是不是"
    "某条线的后续进展(同一个故事在演进),还是一个新故事。\n"
    "判准:同一主体同一件事的不同阶段(传闻→申请→获批→落地)= 同一条线;只是同公司但不同事"
    "(OpenAI 上市 vs OpenAI 发新模型)= 不同线;拿不准时倾向新开(NEW),别硬塞。\n"
    '只输出 JSON:{"line":"<命中线编号 L1/L2/…,或 NEW>","confidence":<0~1>}'
)


def judge_line(
    *, headline: str, tldr: str | None, subject: str,
    candidates: list[tuple[str | None, str | None]],  # [(线名, 线现状摘要), …],顺序即 L1,L2,…
    settings: Settings,
) -> tuple[int | None, float]:
    """返回 (命中候选下标 or None=新开, confidence)。失败/越界冒泡或归 None,由 engine 兜。"""
    cfg = settings.threads
    lines = []
    for i, (name, summary) in enumerate(candidates, start=1):
        line = f"L{i}. {name or subject}"
        if summary:
            line += f" —— 现状:{summary}"
        lines.append(line)
    user = (
        f"新报道:\n  主体:{subject}\n  标题:{headline}\n"
        + (f"  速读:{tldr}\n" if tldr else "")
        + "\n正在追踪的事件线:\n"
        + "\n".join(lines)
        + '\n\n只输出 JSON:{"line":"L?|NEW","confidence":0~1}'
    )
    out = parse_json(
        complete_json(_SYSTEM, user, model=cfg.judge_model, max_tokens=cfg.judge_max_tokens,
                      settings=settings, stage="thread_judge")
    )
    decision = str(out.get("line", "NEW")).strip().upper()
    conf = float(out.get("confidence", 0.0) or 0.0)
    if not decision.startswith("L"):
        return None, conf
    try:
        idx = int(decision[1:]) - 1
    except ValueError:
        return None, conf
    return (idx, conf) if 0 <= idx < len(candidates) else (None, conf)
