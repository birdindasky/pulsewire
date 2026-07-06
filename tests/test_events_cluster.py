"""events.cluster:A 层候选圈选纯逻辑 + 锁定参数固化(承重墙,改参数须重过校准门)。"""
from __future__ import annotations

from pulsewire.events import cluster as C


def test_locked_params_unchanged():
    # 锁定值守卫:Phase 1a 验过的配置,改动须重过校准门并更新此测试(防误改静默漂移)
    assert C.CAND_COSINE_FLOOR == 0.55
    assert C.CAND_LEX_JACCARD == 0.5
    assert C.CAND_TOP_K == 8
    assert C.CONTENT_TRUNCATE == 500
    # v2 判官 prompt 守卫:必是校准验过的"同一个底层现实事件...就判同"激进版(非保守原始版,
    # 移植保真门曾因抄成保守版把 recall 从 0.868 砸到 0.755)。
    assert "同一个底层现实事件" in C.JUDGE_SYS_V2
    assert "就判同" in C.JUDGE_SYS_V2


def test_surface_candidates_semantic_neighbor():
    # 两簇主体向量近(cos≈1)→ 互为候选;第三簇正交 → 不进
    ids = ["a", "b", "c"]
    subjects = {"a": "OpenAI IPO", "b": "OpenAI listing", "c": "Gaza ceasefire"}
    vecs = {"a": [1.0, 0.0], "b": [0.99, 0.01], "c": [0.0, 1.0]}
    cand = C.surface_candidates(ids, subjects, vecs)
    assert "b" in cand["a"]  # 近邻进候选
    assert "c" not in cand["a"]  # 正交不进


def test_surface_candidates_lexical_union():
    # 向量缺失但主体词法子集 → 仍靠词法进候选(并集)
    ids = ["a", "b"]
    subjects = {"a": "Nvidia bond", "b": "Nvidia bond sale"}
    vecs: dict = {}  # 无向量
    cand = C.surface_candidates(ids, subjects, vecs)
    assert cand["a"] == ["b"]  # 词法接近(子集)进候选


def test_surface_candidates_topk_cap():
    # 超过 top-K 个近邻 → 截断到 CAND_TOP_K
    n = C.CAND_TOP_K + 5
    ids = [f"i{k}" for k in range(n + 1)]
    subjects = {i: "same subject phrase here" for i in ids}
    vecs = {i: [1.0, 0.0] for i in ids}  # 全互近
    cand = C.surface_candidates(ids, subjects, vecs)
    assert len(cand["i0"]) == C.CAND_TOP_K  # 硬顶 top-K
