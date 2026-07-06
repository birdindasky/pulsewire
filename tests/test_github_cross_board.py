"""GitHub 榜跨板**同项目**去重(2026-06-25 三考 dedup 一票否决:Omnigent 在 AI 板 + GitHub 榜各一次)。

_name_token_overlap:repo 显著名 token 撞上其它板成稿标题词集 → 判同项目,热榜不重复。
治"新闻板讲某 repo + 热榜又单列该 repo"——URL 法抓不到(新闻条目 URL 非 github)。
"""
from __future__ import annotations

from pulsewire.github_board.engine import _name_token_overlap


def test_omnigent_cross_board_hit():
    # AI 板成稿『Databricks 开源 Omnigent 框架』→ 词集含 omnigent;热榜 omnigent-ai/omnigent 撞上
    words = {"databricks", "开源", "omnigent"}  # (中文已被 re.split 滤掉,这里模拟最终词集)
    assert _name_token_overlap("omnigent-ai/omnigent", words) == {"omnigent"}


def test_unrelated_repo_not_hit():
    words = {"databricks", "omnigent", "openai"}
    assert _name_token_overlap("someowner/quasar-engine", words) == set()  # quasar 不在词集 → 留


def test_partial_token_not_hit():
    # deepseek-reasonix:新闻提了公司名 deepseek 但没 reasonix → 须全部命中,故不剔(治误伤)
    words = {"deepseek", "v4", "推理"}
    assert _name_token_overlap("esengine/deepseek-reasonix", words) == set()


def test_generic_name_never_cross_hit():
    # 全通用词名(ai/agent 都在 _GH_NAME_STOP)→ 无显著 token → 永不跨板误命中
    words = {"omnigent", "agent", "ai", "code"}
    assert _name_token_overlap("foo/ai-agent", words) == set()


def test_distinctive_name_hits_exact_word():
    words = {"langchain", "升级", "平台"}
    assert _name_token_overlap("langchain-ai/langchain", words) == {"langchain"}


def test_empty_words_no_hit():
    assert _name_token_overlap("omnigent-ai/omnigent", set()) == set()


def test_ambiguous_name_unrelated_owner_not_hit():
    # 2026-06-26 hermes 误剔:AI 板讲 Nous 的 Hermes → 词集含 hermes;
    # 但热榜是陌生 owner(fathah/ekkolearnai)的同名 repo → 仅撞名,不剔。
    words = {"nous", "research", "hermes"}
    assert _name_token_overlap("fathah/hermes-desktop", words) == set()
    assert _name_token_overlap("ekkolearnai/hermes-studio", words) == set()
    assert _name_token_overlap("ekkolearnai/hermes-web-ui", words) == set()


def test_ambiguous_name_with_owner_corroboration_hits():
    # owner 显著名也出现在其它板成稿(同一出品方两处登)→ 仍判同项目去重。
    words = {"acme", "hermes", "发布"}
    assert _name_token_overlap("acme/hermes", words) == {"hermes"}


def test_owner_without_distinctive_token_falls_back():
    # owner 全是通用词/无显著 token(如 'ai')→ 退回只按 repo 名(老行为),不强求 owner 佐证。
    words = {"quasar", "升级"}
    assert _name_token_overlap("ai/quasar", words) == {"quasar"}
