"""事件线 A 层测试:subject 归一化 / 同主体匹配(纯函数)+ 抽取(mock LLM,不打网络)。

外加 cosine / select_candidate_threads(语义召回纯逻辑,用玩具向量,不真 embed)。
"""

from __future__ import annotations

import json
import types

from pulsewire.config import get_settings
from pulsewire.threads import subject as S


def _thr(tid: str, subj: str):
    """在追线 stand-in,只需 .thread_id / .subject。"""
    return types.SimpleNamespace(thread_id=tid, subject=subj)


def test_normalize_collapses_case_space_punct():
    assert S.normalize_subject("  OpenAI   IPO ") == "openai ipo"
    assert S.normalize_subject("「DeepSeek V4 发布」") == "deepseek v4 发布"
    assert S.normalize_subject("") == ""
    # 全角空格也折叠
    assert S.normalize_subject("伊朗　以色列　冲突") == "伊朗 以色列 冲突"


def test_subjects_close_subset_and_jaccard():
    # 子集:演进事件标题变长仍算同一条线
    assert S.subjects_close("OpenAI IPO", "OpenAI IPO 获批")
    # 归一相等(大小写/空白差异)
    assert S.subjects_close("openai ipo", "  OpenAI  IPO ")
    # 词序无关(token 集合)
    assert S.subjects_close("以色列 伊朗 冲突", "伊朗 以色列 冲突")


def test_subjects_close_rejects_unrelated():
    assert not S.subjects_close("OpenAI IPO", "Nvidia 财报")
    assert not S.subjects_close("OpenAI IPO", "")
    # 只共享一个泛 token、Jaccard 不够 → 不算同一条线
    assert not S.subjects_close("OpenAI 模型 发布", "Google 模型 财报")


def test_match_subject_picks_closest_or_none():
    cands = ["Nvidia 财报", "OpenAI IPO", "伊朗 以色列 冲突"]
    assert S.match_subject("OpenAI IPO 获批", cands) == "OpenAI IPO"  # 子集命中
    assert S.match_subject("DeepSeek V4 发布", cands) is None  # 都不接近 → 新主体


def test_match_subject_prefers_higher_overlap():
    cands = ["OpenAI 发布", "OpenAI IPO 传闻"]
    # "OpenAI IPO" 与第二个 token 重叠更多(子集),应选它而非泛的 "OpenAI 发布"
    assert S.match_subject("OpenAI IPO", cands) == "OpenAI IPO 传闻"


def test_extract_subject_parses_llm_json(monkeypatch):
    monkeypatch.setattr(
        S, "_complete_json", lambda *a, **k: json.dumps({"subject": "OpenAI IPO"})
    )
    out = S.extract_subject("OpenAI 据报道已秘密提交上市申请", domain="ai", settings=get_settings())
    assert out == "OpenAI IPO"


def test_extract_subject_raises_on_empty(monkeypatch):
    monkeypatch.setattr(S, "_complete_json", lambda *a, **k: json.dumps({"subject": "  "}))
    import pytest

    s = get_settings()
    s = s.model_copy(update={"threads": s.threads.model_copy(update={"json_schema_retry": 0})})
    with pytest.raises(RuntimeError):  # retry=0 → 1 次即抛,不真 sleep
        S.extract_subject("某条没有主体的新闻", settings=s)


# ── 语义召回(cosine + select_candidate_threads)──────────────────────────────

def test_cosine_basic():
    assert S.cosine([1, 0, 0], [1, 0, 0]) == 1.0
    assert S.cosine([1, 0, 0], [0, 1, 0]) == 0.0
    assert S.cosine([0, 0, 0], [1, 2, 3]) == 0.0  # 零向量 → 0(不炸)
    assert abs(S.cosine([1, 1, 0], [1, 0, 0]) - 0.70710678) < 1e-6


def test_select_candidates_lexical_only():
    """不传向量 = 纯词法,与旧行为一致(子集命中)。"""
    active = [_thr("t1", "OpenAI IPO"), _thr("t2", "Nvidia 财报")]
    out = S.select_candidate_threads("OpenAI IPO 获批", active, match_threshold=0.5)
    assert [t.thread_id for t in out] == ["t1"]


def test_select_candidates_semantic_rescues_lexical_miss():
    """治裂线的核心:词法漏掉的'同故事换措辞',语义把老线召回。"""
    # 词法:'Israel Iran peace deal' vs 'Israel Iran conflict' Jaccard=2/5=0.4 <0.5 → 漏
    assert not S.subjects_close("Israel Iran peace deal", "Israel Iran conflict", 0.5)
    active = [_thr("t1", "Israel Iran conflict"), _thr("t2", "Nvidia earnings")]
    out = S.select_candidate_threads(
        "Israel Iran peace deal", active, match_threshold=0.5,
        subject_vec=[1.0, 0.0, 0.0],
        thread_vecs={"t1": [0.95, 0.31, 0.0], "t2": [0.0, 1.0, 0.0]},  # t1 余弦≈0.95,t2=0
        semantic_threshold=0.70, semantic_top_k=5,
    )
    assert [t.thread_id for t in out] == ["t1"]  # 语义召回老线,B 再判是否真的接


def test_select_candidates_semantic_threshold_and_topk():
    """阈值滤掉不够像的;top_k 控候选规模(守'B 只看少数候选')。"""
    active = [_thr("a", "p"), _thr("b", "q"), _thr("c", "r"), _thr("d", "s")]  # 单 token,无词法命中
    out = S.select_candidate_threads(
        "z", active, match_threshold=0.5,
        subject_vec=[1.0, 0.0, 0.0],
        thread_vecs={  # 余弦 a≈0.90 b=0.80 c≈0.75 d≈0.50
            "a": [0.9, 0.44, 0.0], "b": [0.8, 0.6, 0.0],
            "c": [0.75, 0.66, 0.0], "d": [0.5, 0.87, 0.0],
        },
        semantic_threshold=0.70, semantic_top_k=2,
    )
    assert sorted(t.thread_id for t in out) == ["a", "b"]  # 过阈值 a/b/c,top_k=2 留最像两条


def test_select_candidates_union_dedups():
    """词法命中 + 语义也命中同一条 → 只算一次(按 thread_id 去重)。"""
    active = [_thr("t1", "OpenAI IPO")]
    out = S.select_candidate_threads(
        "OpenAI IPO 获批", active, match_threshold=0.5,
        subject_vec=[1.0, 0.0, 0.0], thread_vecs={"t1": [1.0, 0.0, 0.0]},
        semantic_threshold=0.70, semantic_top_k=5,
    )
    assert [t.thread_id for t in out] == ["t1"]  # 不重复
