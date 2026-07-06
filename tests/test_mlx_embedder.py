"""MlxEmbedder(Qwen3/MLX)回归测试:守池化坑 + 维度 + 对称/非对称用法。
非 mac / 没装 mlx-embeddings 时自动跳过(跨平台不挂)。"""
from __future__ import annotations

import pytest

mlx_embeddings = pytest.importorskip("mlx_embeddings")

from pulsewire.config.models import EmbeddingCfg  # noqa: E402
from pulsewire.dedup.embedding import MlxEmbedder  # noqa: E402

MODEL = "mlx-community/Qwen3-Embedding-0.6B-8bit"


@pytest.fixture(scope="module")
def emb() -> MlxEmbedder:
    return MlxEmbedder(MODEL)


def _cos(a, b):
    import numpy as np

    a, b = np.array(a), np.array(b)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def test_dim_and_l2(emb: MlxEmbedder):
    """维度必须 1024 且 L2 归一(池化没生效会是 16384 或非 1)。"""
    import numpy as np

    v = emb.embed(["测试一条新闻向量"])
    assert len(v) == 1 and len(v[0]) == 1024
    assert abs(float(np.linalg.norm(v[0])) - 1.0) < 0.02


def test_symmetric_separation(emb: MlxEmbedder):
    """对称用法:同事件改写余弦 > 无关。"""
    a, b, c = emb.embed([
        "中方宣布制裁菲律宾防长特奥多罗及其亲属",
        "中国对菲防长特奥多罗实施制裁 含其家属",
        "我国首颗文物遥感卫星文物01星成功发射",
    ])
    assert _cos(a, b) > _cos(a, c)


def test_query_passage_retrieval(emb: MlxEmbedder):
    """非对称:带指令的问题应检索到对的段落(命中 #0)。"""
    q = emb.embed_query("中方对菲律宾防长采取了什么措施?")
    docs = emb.embed_passage([
        "中方宣布制裁菲律宾防长特奥多罗及其亲属,因其在南海问题上的恶劣言行",
        "辽宁舰编队完成远海实战化训练任务返回母港",
    ])
    assert _cos(q, docs[0]) > _cos(q, docs[1])


def test_empty_input(emb: MlxEmbedder):
    assert emb.embed([]) == []
    assert emb.embed_passage([]) == []


def test_get_embedder_dispatch():
    """provider=mlx 时 get_embedder 返回 MlxEmbedder。"""
    from pulsewire.dedup.embedding import get_embedder

    class _S:
        class dedup:
            embedding = EmbeddingCfg(provider="mlx", model=MODEL)

    assert isinstance(get_embedder(_S), MlxEmbedder)
