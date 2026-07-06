"""交付的统一契约:投递载荷 + 渠道结果。

渠道分发按"幂等键挡重复、单渠道失败不拖垮、不假装发成功"的行为规格实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DeliverPayload:
    interest_key: str
    title: str  # 日报标题(= 兴趣)
    date_str: str  # YYYY-MM-DD
    digest: str  # 概述
    items: list[dict]  # [{id, headline, tldr, insight, source, url, needs_review, category, tracking_days?, thread_id?}]
    #                    tracking_days/thread_id 仅当该条属于多天在追线时出现(「持续关注」徽标)
    image_path: str | None = None  # 详读长图 PNG 绝对路径(没有则文字交付)
    overview_image_path: str | None = None  # 速读卡 PNG 绝对路径
    github: list[dict] = field(default_factory=list)  # GitHub 开源热榜 [{id,headline,tldr,insight,stars,url,...}]
    github_image_path: str | None = None  # 开源热榜 PNG 绝对路径
    # 飞书自建应用图片推送用:本次要发的 PNG 绝对路径(有序:各领域速读卡 + 热榜图)
    image_paths: list[str] = field(default_factory=list)
    # 多领域聚合(webapp 用):[{key,label,digest,items}],含主领域 AI;bio/geo 各一项。
    # feishu/微信只发主领域(items/digest),不读 domains,避免一天四张卡刷屏。
    domains: list[dict] = field(default_factory=list)
    # 事件线(webapp「在追」视图):[{thread_id,name,domain,summary,heat,days,timeline:[{date,headline,url,source}]}]。
    # 只 webapp 读;跨天演进的派生数据,空 = 暂无跨天线(正常,非错误)。
    threads: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class ChannelResult:
    channel: str
    status: str  # sent | skipped | failed
    reason: str = ""
    extra: dict = field(default_factory=dict)
