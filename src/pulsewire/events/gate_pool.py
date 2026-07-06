"""闸判官并发执行器(2026-07-02 提速批):三道闸 + 分板器共用的"头部并发判"。

背景:话题/水货/够格三闸 + 分板器的 LLM 判官原先全串行(板内逐条排队、三板再排队),
2026-07-01 真跑 ~23min 全耗在等 API 回包——rank 总 35min 的大头。各事件的判定彼此独立
(输入/票数/提前停全闭在单事件的 judge 回调里),并发只改"多快回",不改任何判定语义。

并发预算:GATE_CONC=5/闸 × 3 板并行 ≤15 路,叠加限额去重的串行同事件判 ≈ 峰值 ~18,
落在 2026-06-22 rank A/B 实测干净区(conc≤20)内。真风险 = 并发压出限流 → 判官失败率飙
→ 各闸 fail 方向(放行/误杀)静默生效,由 deploy/perf/rank_ab.py 失败率硬门(<2%)看住。

🔴 各判官工厂闭包里的成本闸计数(calls)/缓存(cache)由工厂内 threading.Lock 守护
   (见各 make_*),并发下 max_*_judges_per_run 仍是精确硬顶。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

GATE_CONC = 5  # 每道闸的并发判官数(3 板并行 × 5 = 峰值 15 路,≤ 实测干净区 20)


def map_judge(judge: Callable[[T], R], head: list[T]) -> list[R]:
    """对 head 逐条跑 judge:并发执行、按 head 原顺序返回(语义 = [judge(e) for e in head])。

    ThreadPoolExecutor.map 保序;任一 judge 抛异常则原样冒泡(与串行版一致——各闸的
    fail-safe 兜底本就锁在 judge 回调内部或过滤器的包装里)。≤1 条走串行,免开池。
    """
    if len(head) <= 1:
        return [judge(e) for e in head]
    with ThreadPoolExecutor(max_workers=min(GATE_CONC, len(head))) as pool:
        return list(pool.map(judge, head))
