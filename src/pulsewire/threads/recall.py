"""A 层候选召回:词法 + 语义(embedding)合一。

engine(日跑)与 rebuild(归档重放)共用同一套召回,免逻辑两处分叉。
- 词法:token Jaccard(`subjects_close`)。
- 语义:主体短语 embedding 余弦(复用 dedup 的 jina-v3,当场算不存库),治"同故事换措辞、词不重叠"裂线。
召回只是把候选凑齐,最终接哪条线/新开仍由 B 判官定(B 兜底防过度合并)。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pulsewire.obs import get_logger
from pulsewire.threads.subject import select_candidate_threads

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()


def ensure_subject_vecs(subjects: list[str], memo: dict[str, object], settings: Settings) -> None:
    """把缺失的主体短语批量 embed 进 memo(按字符串去重,全程每个主体只算一次)。"""
    missing = sorted({s for s in subjects if s and s not in memo})
    if not missing:
        return
    import numpy as np

    from pulsewire.dedup.embedding import get_embedder

    vecs = get_embedder(settings).embed(missing)
    for s, v in zip(missing, vecs):
        memo[s] = np.asarray(v, dtype=float)


def gather_candidates(
    subject: str, active: list, settings: Settings, memo: dict[str, object]
) -> list:
    """A 层候选 = 词法 ∪ 语义;语义出错(模型/内存)降级为纯词法,不拖垮归线。

    active 元素须有 .thread_id 和 .subject;memo 跨调用复用(按主体字符串缓存向量)。
    """
    cfg = settings.threads
    subject_vec = None
    thread_vecs: dict[str, object] | None = None
    if cfg.semantic_match:
        try:
            ensure_subject_vecs(
                [subject, *(t.subject for t in active if t.subject)], memo, settings
            )
            subject_vec = memo.get(subject)
            thread_vecs = {
                t.thread_id: memo[t.subject]
                for t in active
                if t.subject and t.subject in memo
            }
        except Exception as exc:  # noqa: BLE001 — embedding 故障(模型/内存)降级为纯词法,不丢整域归线
            log.warning("threads.semantic.degraded", error=str(exc))
            subject_vec, thread_vecs = None, None
    return select_candidate_threads(
        subject,
        active,
        match_threshold=cfg.match_threshold,
        subject_vec=subject_vec,
        thread_vecs=thread_vecs,
        semantic_threshold=cfg.semantic_threshold,
        semantic_top_k=cfg.semantic_top_k if cfg.semantic_match else 0,
    )
