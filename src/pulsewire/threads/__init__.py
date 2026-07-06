"""threads — 事件线(跨天盯梢):把跨天、措辞各异但同一故事的多个簇串成一条线。

方案 B+A(见 docs/DESIGN.md §4):A 认主体缩候选(subject)→ B AI 判官理剧情。
- step 1 ✅ 建表(store.tables: Thread / ThreadCluster)
- step 2 ✅ A 层 subject 抽取 + 归一化 + 同主体匹配(subject.py)
- step 3 ✅ B 判官(judge.py)+ 归线引擎(engine.py)+ 流水线 threads 站
- step 4+ --rebuild / 前端「在追」
"""

from .engine import run_threads, thread_domain
from .judge import judge_line
from .subject import extract_subject, match_subject, normalize_subject, subjects_close

__all__ = [
    "extract_subject",
    "match_subject",
    "normalize_subject",
    "subjects_close",
    "judge_line",
    "run_threads",
    "thread_domain",
]
