"""github_board 测试:owner/repo 去重键解析(纯函数)+ 排除名单过滤(需 DB)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pulsewire.config import get_settings
from pulsewire.github_board.engine import _repo_key, _select_trending
from pulsewire.store import repo


def test_repo_key_extracts_owner_repo():
    assert _repo_key("https://github.com/langchain-ai/deepagents") == "langchain-ai/deepagents"
    # 带子路径/查询也只取 owner/repo
    assert _repo_key("https://github.com/google/skills/tree/main") == "google/skills"
    # 大小写归一(去重不分大小写)
    assert _repo_key("https://github.com/OpenAI/Plugins") == "openai/plugins"


def test_repo_key_rejects_non_repo_urls():
    assert _repo_key("https://github.com/onlyowner") is None
    assert _repo_key("https://example.com/a/b") is None
    assert _repo_key("") is None


def _board_settings(exclude):
    base = get_settings()
    # 大 limit + 超大 stars,确保不被真实库里其它高星条目挤出 top-N(测试只关心排除生效)
    rank = base.rank.model_copy(update={"github_board_exclude": exclude,
                                        "github_board_limit": 500,
                                        "github_board_recency_days": 365})
    return base.model_copy(update={"rank": rank})


@pytest.mark.asyncio
async def test_select_trending_drops_excluded_repo(db_session, clean_github_candidates):
    """排除名单内的 owner/repo 不进热榜(大小写不敏感);名单外照常上榜。"""
    now = datetime.now(timezone.utc)
    # 源必须是注册表里真实存在、enabled、且匹配 ILIKE 'ai/llm/agent' 的源(2026-06-15 一⑥ 加了注册表过滤)
    reg = "github-search-ai-agents"
    excluded_id = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/acme-labs/self-repo",
        title="self-repo", published_at=now, facts={"github": {"stars": 9_999_999_999}},
    )
    kept_id = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/langchain-ai/langgraph",
        title="LangGraph", published_at=now, facts={"github": {"stars": 9_999_999_998}},
    )

    # 排除 acme-labs/self-repo(故意大小写不同,验证归一):只剩 langgraph
    picked = await _select_trending(db_session, _board_settings(["ACME-LABS/Self-Repo"]))
    ids = {item_id for item_id, _ in picked}
    assert kept_id in ids
    assert excluded_id not in ids

    # 不排除时:两个都在(确认上面是排除生效而非数据没进库)
    picked_all = await _select_trending(db_session, _board_settings([]))
    ids_all = {item_id for item_id, _ in picked_all}
    assert excluded_id in ids_all and kept_id in ids_all


@pytest.mark.asyncio
async def test_select_trending_ranks_by_star_velocity_not_absolute(db_session, clean_github_candidates):
    """按"星/天龄"涨速代理排,而非绝对总星:新仓冲高星排前、老巨仓沉底;缺 created_at 垫底。"""
    now = datetime.now(timezone.utc)

    async def mk(owner_repo, stars, created_days_ago):
        facts = {"github": {"stars": stars}}
        if created_days_ago is not None:
            facts["github"]["created_at"] = (now - timedelta(days=created_days_ago)).isoformat()
        return await repo.upsert_item(
            db_session, source="github-search-ai-agents", url=f"https://github.com/{owner_repo}",
            title=owner_repo, published_at=now, facts=facts,
        )

    old_giant = await mk("acmevel/oldgiant", 50_000, 1000)   # 50/天:总星最高却最老
    fresh_hot = await mk("acmevel/freshhot", 3_000, 10)      # 300/天:涨得最猛
    fresh_modest = await mk("acmevel/freshmodest", 800, 4)   # 200/天:星少但新且快
    no_created = await mk("acmevel/nocreated", 99_999, None)  # 缺 created_at → 涨速 0,垫底

    picked = await _select_trending(db_session, _board_settings([]))
    order = [iid for iid, _ in picked]
    pos = {iid: order.index(iid) for iid in (old_giant, fresh_hot, fresh_modest, no_created)}
    # 涨速:freshhot(300) > freshmodest(200) > oldgiant(50) > nocreated(0)
    # 与绝对总星顺序(nocreated 99999 > oldgiant 50000 > freshhot 3000 > freshmodest 800)几乎相反
    # —— 证明排的是涨速不是总量,新仓真能压过老巨仓
    assert pos[fresh_hot] < pos[fresh_modest] < pos[old_giant] < pos[no_created]


@pytest.mark.asyncio
async def test_select_trending_drops_orphan_undated_and_stale(db_session, clean_github_candidates):
    """2026-06-15 一⑥(防 ECC 假 21.5 万星顶第一):孤儿源(匹配 ILIKE 但不在注册表)/
    无 published_at / 超出新鲜度窗的条目都不得上榜,哪怕星数巨高;合法注册源照常上榜。"""
    now = datetime.now(timezone.utc)
    reg = "github-search-ai-agents"  # 注册表真实存在、enabled、匹配 ILIKE 'agent'

    # 候选池隔离由 clean_github_candidates 夹具接管(事务内清 github 候选,随 rollback 恢复);
    # 本测试保留 good=100/orphan=21.5万 的历史语义(docstring 防 ECC 假 21.5 万星),靠真清表测得准。
    good = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/legit/repo-ai",
        title="legit", published_at=now, facts={"github": {"stars": 100}},
    )
    # 孤儿源 slug 故意含 'ai'(能过 ILIKE),只靠"不在注册表"被挡 —— 隔离验证注册表过滤本身生效
    orphan = await repo.upsert_item(
        db_session, source="ecc-ai-orphan-removed", url="https://github.com/ecc/ai-fake",
        title="ECC", published_at=now, facts={"github": {"stars": 215_446}},
    )
    undated = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/legit/undated-ai",
        title="undated", published_at=None, facts={"github": {"stars": 999_999}},
    )
    stale = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/legit/stale-ai",
        title="stale", published_at=now - timedelta(days=400),
        facts={"github": {"stars": 888_888}},
    )

    picked = await _select_trending(db_session, _board_settings([]))
    ids = {iid for iid, _ in picked}
    assert good in ids        # 注册 + 有日期 + 新鲜 → 上榜
    assert orphan not in ids  # 源不在注册表 → 出局(哪怕 21.5 万星)
    assert undated not in ids  # published_at 为 NULL → 出局
    assert stale not in ids   # 超出新鲜度窗(_board_settings recency=365 天)→ 出局


@pytest.mark.asyncio
async def test_select_trending_cross_board_dedup(db_session, clean_github_candidates):
    """2026-06-15 二⑤:已进别领域日报(其它 interest_key 的 rankings)的 repo 不在 GH 榜重复刷。"""
    now = datetime.now(timezone.utc)
    reg = "github-search-ai-agents"
    in_ai = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/acme/in-ai-digest-ai",
        title="已在AI日报", published_at=now, facts={"github": {"stars": 5000}},
    )
    gh_only = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/acme/gh-only-ai",
        title="只在GH榜", published_at=now, facts={"github": {"stars": 4000}},
    )
    # 把 in_ai 排进某个非 ghboard 领域的 rankings(模拟 AI 日报已选它)
    await repo.upsert_ranking(
        db_session, interest_key="int_cross_test", interest="AI", tags=[], item_id=in_ai,
        cluster_id=None, recall_score=0.5, rule_score=0.5, rerank_score=0.5,
        final_score=0.5, rank=1, provider="deepseek", run_id=None,
    )
    picked = await _select_trending(db_session, _board_settings([]))
    ids = {iid for iid, _ in picked}
    assert gh_only in ids     # 只在 GH 榜的照常上榜
    assert in_ai not in ids   # 已在别领域日报的被跨板块去重掉(run_id 缺省=不限定,压制全部)


@pytest.mark.asyncio
async def test_cross_board_dedup_scoped_to_run_id(db_session, clean_github_candidates):
    """2026-06-15 二⑤ 加固:跨板块去重按 run_id 限定 → 已下线领域的陈旧 rankings 不永久压制 repo。"""
    now = datetime.now(timezone.utc)
    reg = "github-search-ai-agents"
    # 这个 repo 进了"别的 run(陈旧)"的某领域 rankings;按本次 run_now 限定时不该压制它
    stale_ranked = await repo.upsert_item(
        db_session, source=reg, url="https://github.com/acme/stale-ranked-ai",
        title="陈旧领域排过", published_at=now, facts={"github": {"stars": 7000}},
    )
    await repo.upsert_ranking(
        db_session, interest_key="int_stale", interest="AI", tags=[], item_id=stale_ranked,
        cluster_id=None, recall_score=0.5, rule_score=0.5, rerank_score=0.5,
        final_score=0.5, rank=1, provider="deepseek", run_id=None,  # 非本次 run 的旧 ranking
    )
    picked = await _select_trending(db_session, _board_settings([]), run_id="run_now")
    assert stale_ranked in {iid for iid, _ in picked}  # 非本次 run 的 ranking 不压制(已 scope 到 run_now)
