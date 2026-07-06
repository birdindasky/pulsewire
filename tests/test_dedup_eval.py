"""去重评测集:验证 embedding 在配置阈值下"宁漏勿误"——零误合并、召回达标。

需要本地 embedding 模型(首次会下载约 2.3GB);模型不可用时自动跳过。无需数据库/网络。
"""

from __future__ import annotations

import pytest
import yaml

from pulsewire.config import PROJECT_ROOT, get_settings
from pulsewire.dedup import get_embedder

EVAL = yaml.safe_load(
    (PROJECT_ROOT / "tests" / "fixtures" / "dedup_eval.yaml").read_text(encoding="utf-8")
)["pairs"]


def _cosine(a, b):
    import numpy as np

    a, b = np.array(a), np.array(b)
    return float(a @ b / ((a @ a) ** 0.5 * (b @ b) ** 0.5))


@pytest.fixture(scope="module")
def pair_scores():
    try:
        emb = get_embedder(get_settings())
        texts = []
        for p in EVAL:
            texts += [p["a"], p["b"]]
        vecs = emb.embed(texts)
    except Exception as exc:  # 模型下载/加载失败 → 跳过(CI 无网络等)
        pytest.skip(f"本地 embedding 模型不可用,跳过评测集:{exc}")
    scores = []
    for i, p in enumerate(EVAL):
        scores.append((_cosine(vecs[2 * i], vecs[2 * i + 1]), bool(p["same"]), p.get("kind", "")))
    return scores


def test_eval_clean_separation(pair_scores):
    """正例最低 cos 应高于负例最高 cos(干净间隔,可分)。"""
    pos = [s for s, same, _ in pair_scores if same]
    neg = [s for s, same, _ in pair_scores if not same]
    assert min(pos) > max(neg), f"正例下界 {min(pos):.3f} 未高于负例上界 {max(neg):.3f}"


def test_no_false_merge_at_threshold(pair_scores):
    """核心铁律:生产阈值下零误合并(precision=1)。误合并最伤可信度。"""
    threshold = get_settings().dedup.embedding.similarity_threshold
    fp = [(s, k) for s, same, k in pair_scores if (not same) and s >= threshold]
    assert not fp, f"阈值 {threshold} 下出现误合并:{fp}"


def test_near_dup_always_merges(pair_scores):
    """同源异库近重复(生产主力场景)必须全部合并。"""
    threshold = get_settings().dedup.embedding.similarity_threshold
    near = [(s, k) for s, same, k in pair_scores if same and k == "near_dup"]
    assert near, "评测集应含 near_dup 正例"
    for s, k in near:
        assert s >= threshold, f"近重复 cos={s:.3f} < 阈值 {threshold},漏合了主力场景"


def test_recall_floor(pair_scores):
    """整体召回信息性下限(跨语言松改写允许漏,故下限设宽)。"""
    threshold = get_settings().dedup.embedding.similarity_threshold
    tp = sum(1 for s, same, _ in pair_scores if same and s >= threshold)
    fn = sum(1 for s, same, _ in pair_scores if same and s < threshold)
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    assert recall >= 0.6, f"召回 {recall:.2f} 过低"


def test_hard_negatives_below_threshold(pair_scores):
    """相关但不同(真实失败模式)必须低于阈值,不能被误合并。"""
    threshold = get_settings().dedup.embedding.similarity_threshold
    for s, same, kind in pair_scores:
        if kind == "related_diff":
            assert not same  # 评测集标注自检
            assert s < threshold, f"相关但不同 cos={s:.3f} ≥ 阈值 {threshold},会误合并"
