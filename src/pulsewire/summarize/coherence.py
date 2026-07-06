"""标题错位护栏(2026-06-17 eval 实锤)。

病根:DeepSeek-v4 偶发把一整批的 headline 写串位——给某 item 写对了 tldr/insight,却配上
邻条的 headline(同一 JSON 对象内 headline 与 tldr 讲两件事)。回填按 item_id 对齐没毛病,
错位发生在模型输出本身;summarize 之后没有"标题对不对得上正文"的检查 → 张冠李戴漏到飞书。
真数据:地缘一批 4 条标题整体错一位(伊朗标题配巴勒斯坦正文…),踩 dedup/threads/readability 三条一票否决。

治法(纯确定性,零额外 LLM 调用):
- 出稿后语义比对每条 headline 与它自己的 tldr+insight 余弦(jina-v3,同 dedup 那把)。
- 自配余弦低于 floor 且存在明显更优归属(best_other - 自配 > margin)= 判该标题错位。
- 错位本质是一次洗牌:在"错位集"内按"标题↔正文最佳余弦"重新配对(贪心最大权匹配),把漂走的
  标题发回它真正描述的那条正文。tldr/insight/item_id 三者本就绑对,只动 headline。
- 模型漏写/写重导致配不回的那条(余弦够不着 floor),用它自己**正确的 tldr** 派生一个安全标题兜底,
  保证绝不展示一个跟正文打架的标题。
"""

from __future__ import annotations

import math
import re

# 句/从句切分:从 tldr 取首句做兜底标题
_SENT_SPLIT = re.compile(r"[。!?！？;；\n]")
_CLAUSE_SEP = ("，", ",", "、", "：", ":", "；")


def _cos(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def plan_headline_repair(
    head_vecs: list[list[float]],
    body_vecs: list[list[float]],
    *,
    floor: float,
    margin: float,
) -> tuple[dict[int, int], set[int], set[int]]:
    """规划错位标题的重配。

    入参:同一批内各条 headline 向量、对应 (tldr+insight) 向量(下标一一对应)。
    返回:
      assignment: body下标 -> 该 body 应显示的 headline 下标(默认 b->b;只改判错位的)。
      drifted   : 原标题被判错位的 body 下标集合(供日志/计数)。
      unresolved: 重配后仍没领到合格标题、需 tldr 兜底的 body 下标集合。
    """
    n = len(body_vecs)
    assignment = {b: b for b in range(n)}
    if n < 2:
        return assignment, set(), set()
    M = [[_cos(head_vecs[h], body_vecs[b]) for b in range(n)] for h in range(n)]
    drifted: set[int] = set()
    for i in range(n):
        diag = M[i][i]
        best_other = max((M[i][j] for j in range(n) if j != i), default=0.0)
        if diag < floor and (best_other - diag) > margin:
            drifted.add(i)
    if not drifted:
        return assignment, drifted, set()
    # 只在错位集内重配:正确条目是不动点,绝不偷它们的标题。
    free_heads = set(drifted)
    free_bodies = set(drifted)
    cands = sorted(
        (
            (M[h][b], h, b)
            for h in drifted
            for b in drifted
            if M[h][b] >= floor
        ),
        reverse=True,
    )
    for _sim, h, b in cands:
        if h in free_heads and b in free_bodies:
            assignment[b] = h
            free_heads.discard(h)
            free_bodies.discard(b)
    unresolved = set(free_bodies)
    return assignment, drifted, unresolved


def lead_from_tldr(tldr: str, *, max_len: int = 30) -> str:
    """从(正确的)tldr 派生一个与正文一致的安全标题:取首句,过长则在从句分隔处收口。"""
    t = (tldr or "").strip()
    if not t:
        return "(标题缺失)"
    parts = [p.strip() for p in _SENT_SPLIT.split(t) if p.strip()]
    lead = parts[0] if parts else t
    if len(lead) > max_len:
        head = lead[:max_len]
        cut = max((head.rfind(sep) for sep in _CLAUSE_SEP), default=-1)
        if cut >= max_len // 2:
            head = head[:cut]
        lead = head
    return lead or t[:max_len]
