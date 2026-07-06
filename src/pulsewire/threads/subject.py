"""事件线 A 层:事件主体(subject)抽取 + 归一化 + 同主体匹配。见 docs/DESIGN.md §4。

A 层只做"预过滤":给簇抽一个事件主体短语,把候选缩到同主体的少数在追线;
真正"接哪条线 / 新开"由 B 判官(step 3)定。故匹配宽松——宁可多给候选,B 再筛。

跨语言一致是 A 层能跨天匹配的前提:抽取提示要求用最广为人知的专有名词(公司/产品用通用英文名),
让同一件事的中英文报道都归到一致的主体短语。仍有够不着的(如纯措辞差异),由 B 兜底,v1 可接受。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from pulsewire.llm_errors import PermanentLLMError
from pulsewire.obs import get_logger
from pulsewire.summarize.backends import parse_json

if TYPE_CHECKING:
    from pulsewire.config import Settings

log = get_logger()

_WS = re.compile(r"[\s　]+")  # 折叠空白(含全角空格)
# 主体短语两端的标点/引号装饰(做匹配键时剥掉,不影响内部 token)
_STRIP = "「」『』\"'《》【】()()[]，,。.、!！?？:：;；—-_/\\|·　 "

_SYSTEM = (
    "你是新闻事件归类助手。给定一条新闻的标题(可能含摘要/领域),抽取它属于哪条可跨天追踪的"
    "「事件主体」故事线。规则:\n"
    "1. 用『主体 + 事件关键词』的极简短语,如 'OpenAI IPO'、'DeepSeek V4 release'、'Israel Iran conflict'。\n"
    "2. 为让同一件事的中英文报道归到**一致**的短语(这是跨天匹配的关键):主体用通用英文名"
    "(OpenAI、Nvidia、DeepSeek);事件关键词也优先用通用英文词(IPO、launch、release、lawsuit、"
    "acquisition、earnings、conflict)。人名、地名、无通用英文名的中文主体才保留原文。\n"
    "   例:中文标题『OpenAI 据报道提交上市申请』也应输出 'OpenAI IPO'(而非 'OpenAI 上市申请')。\n"
    "3. 只抓核心故事,别用泛词(不要 '人工智能'、'AI'、'科技新闻'、'大模型')。\n"
    '4. 只输出 JSON:{"subject":"<事件主体短语>"}'
)


def normalize_subject(raw: str) -> str:
    """归一化为匹配键:小写、折叠空白、剥两端标点装饰。空输入得空串。"""
    if not raw:
        return ""
    s = _WS.sub(" ", raw.strip().lower())
    return " ".join(t.strip(_STRIP) for t in s.split() if t.strip(_STRIP)).strip()


def _tokens(subject: str) -> set[str]:
    return {t for t in normalize_subject(subject).split() if t}


def _score(a: str, b: str) -> float:
    """两个主体的接近度 0~1:归一相等或一方 token 子集记 1.0,否则 token Jaccard。"""
    na, nb = normalize_subject(a), normalize_subject(b)
    if not na or not nb:
        return 0.0
    ta, tb = _tokens(a), _tokens(b)
    if na == nb or ta <= tb or tb <= ta:  # 子集:"OpenAI IPO" ⊆ "OpenAI IPO 获批"
        return 1.0
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def subjects_close(a: str, b: str, threshold: float = 0.5) -> bool:
    """两个主体是否'文本高度接近'(A 层预过滤用,宽松)。"""
    return _score(a, b) >= threshold


def match_subject(subject: str, candidates: list[str], threshold: float = 0.5) -> str | None:
    """从在追线的主体列表里挑最接近的;都不够接近返回 None(=新主体,B 再确认是否真新开)。"""
    best, best_score = None, threshold
    for c in candidates:
        sc = _score(subject, c)
        if sc >= best_score or (best is None and sc >= threshold):
            best, best_score = c, sc
    return best


def cosine(a, b) -> float:
    """两向量余弦相似度;任一为零向量返回 0。a/b 为等长浮点序列。"""
    import numpy as np

    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(va @ vb / (na * nb))


def select_candidate_threads(
    subject: str,
    threads: list,
    *,
    match_threshold: float,
    subject_vec=None,
    thread_vecs: dict[str, object] | None = None,
    semantic_threshold: float = 1.1,
    semantic_top_k: int = 0,
) -> list:
    """A 层收候选在追线 = 词法接近 ∪ 语义接近(并集去重,最终接不接由 B 判官定)。

    - 词法:`subjects_close`(token Jaccard >= match_threshold)。
    - 语义(可选,传 subject_vec + 各线 subject 向量才启用):主体短语 embedding 余弦
      >= semantic_threshold 的,按相似度取 top_k 补进候选——治"同故事换措辞,词不重叠"的裂线。
    默认参数(top_k=0)= 纯词法,与旧行为一致。threads 元素须有 .thread_id 和 .subject。
    """
    chosen: dict[str, object] = {}
    for t in threads:
        if t.subject and subjects_close(subject, t.subject, match_threshold):
            chosen[t.thread_id] = t
    if subject_vec is not None and thread_vecs and semantic_top_k > 0:
        scored: list[tuple[float, object]] = []
        for t in threads:
            if t.thread_id in chosen:  # 词法已收,不重复
                continue
            v = thread_vecs.get(t.thread_id)
            if v is None:
                continue
            sim = cosine(subject_vec, v)
            if sim >= semantic_threshold:
                scored.append((sim, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _sim, t in scored[:semantic_top_k]:
            chosen[t.thread_id] = t
    return list(chosen.values())


def _complete_json(system: str, user: str, settings: Settings) -> str:
    """A 层抽取的 LLM 调用(flash 档);抽到 threads.llm 复用,保留此 seam 便于测试 monkeypatch。"""
    from pulsewire.threads.llm import complete_json

    cfg = settings.threads
    return complete_json(
        system, user, model=cfg.subject_model, max_tokens=cfg.subject_max_tokens,
        settings=settings, stage="subject",
    )


def extract_subject(
    headline: str, *, summary: str | None = None, domain: str | None = None, settings: Settings
) -> str:
    """LLM 抽取事件主体短语(展示原文);匹配键由 normalize_subject 派生。

    失败/空冒泡——由上层(step 3 归线站)决定降级,不在此静默。
    🔴 守卫(2026-07-04 考官):判决缓存(S1)的键**故意不含主体**,安全前提=主体只从
    headline+截断正文派生(键内内容的函数,零独立信息)。若未来给本函数喂**键外**信息
    (全文/外部实体库等),须同步重审 events/judge_cache 各 *_item_hash,否则缓存会陈旧复用。
    """
    parts = [f"标题: {headline}"]
    if summary:
        parts.append(f"摘要: {summary[:500]}")
    if domain:
        parts.append(f"领域: {domain}")
    parts.append('只输出 JSON:{"subject":"<事件主体短语>"}')
    user = "\n".join(parts)
    attempts = settings.threads.json_schema_retry + 1
    last_err: Exception | None = None
    for i in range(attempts):  # flash 偶发空返回/坏 JSON → 退避重试兜底
        try:
            subject = (parse_json(_complete_json(_SYSTEM, user, settings)).get("subject") or "").strip()
            if subject:
                return subject
            last_err = RuntimeError("LLM 返回空 subject")
        except PermanentLLMError:
            raise  # 没钱/凭证失效:立即熔断,别退避重试同一 402、别静默降级标题(2026-07-02 E1)
        except Exception as exc:  # JSON 解析失败等
            last_err = exc
        log.warning("threads.subject.retry", attempt=i + 1, of=attempts, error=str(last_err))
        if i + 1 < attempts:
            time.sleep(0.8 * (2 ** i))  # 指数退避:立刻重试常撞同一抽风窗口(2026-06-15 二⑧)
    raise RuntimeError(f"subject 抽取失败(重试 {attempts} 次):{last_err}")
