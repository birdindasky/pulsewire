"""Embedding 后端:把文本转成向量(语义近重复用)。

- mlx   : mlx-embeddings(Apple Silicon/Metal,默认 Qwen3-0.6B-8bit,$0 离线)。**仅 Apple Silicon**。
- local : fastembed(ONNX Runtime,默认 jina-embeddings-v3,$0 离线,1024 维)。
          **跨平台**:Linux / Windows / Intel Mac / AMD 都能纯 CPU 跑,非苹果机器走这条。
- jina  : Jina API(需 key,阶段 3 暂未实现,留接口)。
模型重(首次下载),按模型名做进程内单例,避免每次跑重载。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pulsewire.config import Settings

# 进程内模型单例:{(model_name, cache_dir): TextEmbedding 实例}
_LOCAL_MODELS: dict[tuple[str, str], object] = {}

# MLX 模型单例:{model_name: (model, tokenizer)}。Qwen3 跑 Apple GPU,重(载入慢),按名缓存。
_MLX_MODELS: dict[str, object] = {}

# 默认稳定缓存目录(避开系统临时目录 /var/folders/.../T,免被 macOS 定期清理后重下 2GB+)
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "pulsewire" / "fastembed"


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成向量(顺序与输入一致)。"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """检索「问题」侧向量(jina-v3 retrieval.query task)。与 embed_passage 配对用。"""
        ...

    def embed_passage(self, texts: list[str]) -> list[list[float]]:
        """检索「入库内容」侧向量(jina-v3 retrieval.passage task)。与 embed_query 配对用。"""
        ...


class LocalEmbedder:
    """fastembed 本地 embedding(ONNX,无需 API key)。"""

    def __init__(self, model_name: str, cache_dir: str | None = None,
                 threads: int | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = str(cache_dir) if cache_dir else str(_DEFAULT_CACHE_DIR)
        self.threads = threads  # CPU 线程上限:None=库默认(抓满核);设数=留核给 UI,防发烫/卡

    def _model(self):
        key = (self.model_name, self.cache_dir, self.threads)
        model = _LOCAL_MODELS.get(key)
        if model is None:
            from fastembed import TextEmbedding  # 延迟导入:重依赖,用到才加载

            Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
            kwargs = {"cache_dir": self.cache_dir}
            if self.threads:
                kwargs["threads"] = self.threads
            model = TextEmbedding(self.model_name, **kwargs)
            _LOCAL_MODELS[key] = model
        return model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [[float(x) for x in vec] for vec in self._model().embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        """检索问题侧向量。jina-v3 须走 query_embed(retrieval.query task),非默认 embed。"""
        vecs = list(self._model().query_embed(text))
        return [float(x) for x in vecs[0]]

    def embed_passage(self, texts: list[str]) -> list[list[float]]:
        """检索入库侧向量(回填档案卡)。jina-v3 须走 passage_embed(retrieval.passage task)。"""
        if not texts:
            return []
        return [[float(x) for x in vec] for vec in self._model().passage_embed(list(texts))]


class MlxEmbedder:
    """Qwen3-Embedding via mlx-embeddings(Apple GPU/Metal,$0 离线)。1024 维、L2 归一。

    - embed()/embed_passage():无指令,对称(去重/事件/卡片入库)。Qwen3 内部 last-token 池化。
    - embed_query():加 Qwen3 官方检索指令前缀(QA 非对称:问题侧)。与 passage 配对用。
    重(首次载入 ~1s),按模型名做进程内单例。
    """

    def __init__(self, model_name: str, query_instruction: str | None = None) -> None:
        self.model_name = model_name
        self.query_instruction = query_instruction or (
            "Instruct: 给定一个问题,检索能回答它的新闻段落\nQuery: "
        )

    def _model(self):
        m = _MLX_MODELS.get(self.model_name)
        if m is None:
            import mlx_embeddings  # 延迟导入:重依赖,用到才加载

            m = mlx_embeddings.load(self.model_name)
            _MLX_MODELS[self.model_name] = m
        return m

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import mlx.core as mx
        import mlx_embeddings
        import numpy as np

        model, tok = self._model()
        out = mlx_embeddings.generate(model, tok, list(texts))
        emb = getattr(out, "text_embeds", out)
        mx.eval(emb)
        arr = np.asarray(emb, dtype=np.float32)
        # 维度护栏:池化没生效会返回 (N, seqlen, 1024) 或 (N, 16384) → 当场冒泡,不静默产垃圾向量
        if arr.ndim != 2 or arr.shape[1] != 1024:
            raise ValueError(
                f"MLX 嵌入维度异常 {arr.shape}(应为 (N,1024));疑池化未生效"
            )
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        arr = arr / np.clip(norms, 1e-9, None)  # 显式 L2 归一(幂等)
        return [[float(x) for x in row] for row in arr]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._encode(list(texts))

    def embed_passage(self, texts: list[str]) -> list[list[float]]:
        return self._encode(list(texts))

    def embed_query(self, text: str) -> list[float]:
        vecs = self._encode([self.query_instruction + text])
        return vecs[0]


def get_embedder(settings: Settings) -> Embedder:
    cfg = settings.dedup.embedding
    if cfg.provider == "local":
        return LocalEmbedder(cfg.model, cache_dir=cfg.cache_dir, threads=cfg.threads)
    if cfg.provider == "mlx":
        # mlx-embeddings 仅 Apple Silicon(pyproject sys_platform=='darwin' 装不到别的平台)。
        # 非苹果机器给一句能照做的话,而不是首次 embed 时抛看不懂的 ModuleNotFoundError。
        if importlib.util.find_spec("mlx_embeddings") is None:
            raise RuntimeError(
                "provider=mlx 需要 Apple Silicon 上的 mlx-embeddings(本机没装到)。"
                "非 Apple Silicon(Intel Mac / Linux / Windows / AMD)请在 config.yaml 把 "
                "dedup.provider 改为 local、dedup.model 改回 jinaai/jina-embeddings-v3——"
                "fastembed/ONNX,纯 CPU 跨平台,中文去重质量够用。"
            )
        return MlxEmbedder(cfg.model, query_instruction=cfg.query_instruction)
    if cfg.provider == "jina":
        # 失败要冒泡:没实现就明确报错,不静默退化成"不去重"
        raise NotImplementedError(
            "Jina API embedder 阶段 3 未实现;请用 provider=local(本地 fastembed)"
        )
    raise ValueError(f"未知 embedding provider:{cfg.provider}")
