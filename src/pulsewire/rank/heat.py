"""事件热度:近窗内"多少个不同源在报相似内容"。

为什么需要:dedup 阈值 0.88 管"同一篇文章合并",不管"同一事件聚类"——
同一事件的不同措辞帖(发布会的 N 个 reddit 帖 + 媒体报道)不会合簇,
"十几个源同报"这个最强新闻信号在簇 source_count 里不存在(实测:头条 107 条散成 95 簇)。
这里用宽松阈值(只计数、不合并存储)把信号找回来。

纯函数 + numpy 分块矩阵乘:近 36h 窗约 5k~8k 条,秒级,内存按块封顶。
"""

from __future__ import annotations


def compute_heat(
    vectors: list[list[float]],
    sources: list[str],
    *,
    threshold: float,
    block: int = 1024,
) -> list[int]:
    """逐条计算热度 = 与其余弦相似度 ≥ threshold 的条目覆盖的不同源数(含自身,最小 1)。

    vectors 与 sources 一一对应。分块算相似度矩阵,内存 O(block × N)。
    """
    import numpy as np

    n = len(vectors)
    if n == 0:
        return []
    m = np.asarray(vectors, dtype=np.float32)
    # 归一化 → 点积即余弦相似度(零向量防除零)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m = m / norms

    # 源 one-hot:neighbor_mask @ onehot → 每条的"邻居覆盖了哪些源"的计数,>0 即覆盖
    uniq = sorted(set(sources))
    src_idx = {s: i for i, s in enumerate(uniq)}
    onehot = np.zeros((n, len(uniq)), dtype=np.float32)
    for i, s in enumerate(sources):
        onehot[i, src_idx[s]] = 1.0

    heat: list[int] = []
    for start in range(0, n, block):
        sims = m[start : start + block] @ m.T  # (block, n)
        mask = (sims >= threshold).astype(np.float32)  # 含自身(对角线 sim=1)
        covered = (mask @ onehot) > 0  # (block, n_sources) bool
        heat.extend(int(x) for x in covered.sum(axis=1))
    return heat


def pick_hot_reps(
    item_ids: list[str],
    vectors: list[list[float]],
    heat: list[int],
    *,
    threshold: float,
    min_sources: int,
    top_n: int,
) -> list[str]:
    """贪心选热点代表:按热度降序取条目,抑制已选代表的邻居(同一事件只出一个代表)。

    返回热度 ≥ min_sources 的至多 top_n 个 item_id(热度降序)。
    """
    import numpy as np

    qualified = [i for i, h in enumerate(heat) if h >= min_sources]
    if not qualified:
        return []
    m = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m = m / norms

    qualified.sort(key=lambda i: heat[i], reverse=True)
    reps: list[str] = []
    rep_vecs: list = []
    for i in qualified:
        if len(reps) >= top_n:
            break
        # 与任一已选代表相似 → 同一事件,跳过(代表已在)
        if rep_vecs and float(np.max(np.stack(rep_vecs) @ m[i])) >= threshold:
            continue
        reps.append(item_ids[i])
        rep_vecs.append(m[i])
    return reps
