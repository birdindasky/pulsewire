"""OSS Insight 涨速榜适配器(GitHub 摘星榜 github_board 的第 5 路供应源)。

上游:https://api.ossinsight.io/v1/trends/repos/?period=past_24_hours
(2026-07-05 实探:{"data": {"columns": [...], "rows": [{repo_name, description,
primary_language, stars, forks, pushes, total_score, collection_names, ...}]}},
值全是字符串,可为空串;**无任何日期列**)。

两个硬约束的解法(先核过 github_board/engine.py 榜池 SQL 再定):

1. AI 限定:API 无 topic/keyword 参数(2026-07-05 实测传 keyword=llm / topic=ai 被静默
   忽略,只有 language 真过滤),榜单是全 GitHub 不分领域;collection_names 实测 1/100
   有值,不可用 → 在适配器里做**保守词边界关键词过滤**(_AI_RE,词表见下,宁漏勿滥:
   词边界防 "daily"/"maintain" 里的 ai 子串误中;专有名词类是高精度 AI 标记)。
   过滤过不去的仓直接丢——源 id 才配叫 ai(榜池按源 id ILIKE ai/llm/agent 过滤)。

2. 日期 + facts.github.stars:榜池 SQL 硬要求 facts.github.stars 非空 **且**
   published_at 非空且在近 30 天窗内(github_board_recency_days,"近期推送过"防陈年
   项目)。facts.github 在本仓一律由适配器抓取时自带(enrich 阶段只从已入库 facts 派生,
   sources.yaml 的 enrich:[github] 是文档性标记,无代码消费)→ 本适配器按 repo 名回源
   GitHub /repos/{owner}/{repo} 取一手 pushed_at/created_at/stargazers_count/forks_count,
   published_at=pushed_at(与其余 4 路 github 搜索源同语义、同 facts 形状,涨速排序/
   数字回源零特判)。🔴 回源失败或拿不到日期的仓**直接丢弃**:绝不发 published_at=None
   的条目——流水线会兜底成抓取时间,= 2026-06-25 GitHub 榜 freshness 造假重演。
   回源全军覆没(有候选但一条没成)→ 抛异常按单源失败冒泡,不静默产空。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from pulsewire.config import get_settings
from pulsewire.obs import get_logger

from .base import RawItem

if TYPE_CHECKING:
    from pulsewire.config import Source
    from pulsewire.fetch.client import FetchClient

log = get_logger()

_GH_REPO_API = "https://api.github.com/repos/{}"
# 回源上限:榜单 100 行、实测 AI 命中约 20~40;30 顶格 ≈ 7.5s(api.github.com 限速 4/s),
# 行序按 total_score 降序(API 原序),截前 30 = 保最热。
_HYDRATE_CAP = 30

# AI 关键词(2026-07-05 卷二,宁漏勿滥):
# - 词边界组:ai/llm/gpt/rag/agent(ic)/mcp/genai/chatbot/copilot——\b 防 "daily"/"email"/
#   "maintain" 等子串误中;agent 在 2026 GitHub 趋势语境≈AI agent,接受 user-agent 类极小误中面。
# - 专有名词/短语组(子串即高精度):openai/anthropic/claude/deepseek/qwen/llama/mistral/
#   huggingface/langchain/ollama/pytorch/tensorflow/transformer/diffusion/multimodal +
#   machine learning / deep learning / language model / neural network / text-to-* / fine-tun。
# - 刻意不收:gemini(撞 gemini:// 协议)、whisper(通用词)、ml(太短)、embedding(泛)——
#   这些仓的描述几乎必带上面的高精度词,漏了算 4 路 topic 搜索源的活。
_AI_RE = re.compile(
    r"(?i)(?:\b(?:ai|llms?|gpt|rag|agents?|agentic|mcp|genai|chatbot|copilot)\b"
    r"|openai|anthropic|claude|deepseek|qwen|llama|mistral|huggingface|langchain|ollama"
    r"|pytorch|tensorflow|transformer|diffusion|multimodal"
    r"|machine learning|deep learning|language model|neural network"
    r"|text-to-(?:image|video|speech)|fine-tun)"
)


def _parse_trends(payload: str) -> list[dict]:
    """纯函数:trends 响应 → 合法行(dict 且 repo_name 形如 owner/repo)列表,保 API 原序。

    - 畸形 JSON → JSONDecodeError 冒泡(单源 fail-loud);
    - 结构不对(无 data.rows 数组)→ ValueError(API 变了要吵,不静默产空)。
    """
    data = json.loads(payload)
    rows = (data.get("data") or {}).get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("ossinsight trends 响应缺 data.rows 数组,API 结构变了?")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("repo_name") or "").strip()
        if not name or "/" not in name:
            continue
        out.append(row)
    return out


def _is_ai_repo(row: dict) -> bool:
    """保守 AI 判定:repo_name + description 词边界关键词(primary_language 无 AI 信号,不参与)。"""
    hay = f"{row.get('repo_name') or ''} {row.get('description') or ''}"
    return bool(_AI_RE.search(hay))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _repo_to_item(repo: dict, *, fallback_description: str | None = None) -> RawItem | None:
    """纯函数:GitHub /repos/{owner}/{repo} 响应 → RawItem;无一手日期 → None(调用方丢弃)。

    facts.github 形状(stars/forks/created_at)与 github.py 完全一致:榜池 SQL、涨速排序
    (created_at=天龄)、enrich 数字回源全部零特判复用。
    """
    full_name = repo.get("full_name")
    if not full_name:
        return None
    published = _parse_dt(repo.get("pushed_at")) or _parse_dt(repo.get("created_at"))
    if published is None:
        return None  # 🔴 无一手时间不发条目:流水线兜底=抓取时间=freshness 造假
    description = repo.get("description") or fallback_description or None
    facts = {
        "github": {
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            # created_at=仓库创建日期:涨速冷启动按"星/天龄"要它,缺了首日沉底(engine 返 0)
            "created_at": repo.get("created_at"),
        }
    }
    return RawItem(
        url=repo.get("html_url") or f"https://github.com/{full_name}",
        title=full_name,
        content=description,
        published_at=published,
        facts=facts,
    )


async def collect(source: Source, client: FetchClient) -> list[RawItem]:
    resp = await client.get(source.url)
    if resp.not_modified:
        return []
    rows = _parse_trends(resp.text)
    cands = [r for r in rows if _is_ai_repo(r)][:_HYDRATE_CAP]
    if not cands:
        log.info("ossinsight.no_ai_candidates", source=source.id, rows=len(rows))
        return []

    headers = {"Accept": "application/vnd.github+json"}
    token = get_settings().resolve_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: list[RawItem] = []
    failures = 0
    for row in cands:
        name = str(row["repo_name"]).strip()
        try:
            # use_conditional=False:躲 304 空身子边角(同 enrich 全文回源惯例);星数常变,304 也没意义
            r = await client.get(_GH_REPO_API.format(name), headers=headers, use_conditional=False)
            repo = json.loads(r.text)
        except Exception as exc:  # 单仓回源失败(404 改名/限速):log+skip,宁漏勿滥
            failures += 1
            log.warning("ossinsight.hydrate.failed", source=source.id, repo=name, error=str(exc))
            continue
        item = _repo_to_item(repo, fallback_description=row.get("description") or None)
        if item is None:
            failures += 1  # 考官次要发现:no_date 不计 failures 时"全 200 无日期"会静默产空
            log.warning("ossinsight.hydrate.no_date", source=source.id, repo=name)
            continue
        items.append(item)

    # 有候选但一条没回源成 → 冒泡当单源失败(fetch.source.failed 可见),绝不静默产空
    if not items and failures == len(cands):
        raise RuntimeError(f"ossinsight 回源 GitHub 全军覆没({failures}/{len(cands)})")
    log.info(
        "ossinsight.collected",
        source=source.id,
        rows=len(rows),
        ai_candidates=len(cands),
        hydrated=len(items),
        failures=failures,
    )
    return items
