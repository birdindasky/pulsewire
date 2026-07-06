"""ORM 表定义(数据契约)。阶段 1 落地核心表;threads/people 为 v2 占位。"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import EMBEDDING_DIM, Base


class Cluster(Base):
    """同事件簇(跨源近重复合并)。cluster_id 由首条派生,跨天稳定。"""

    __tablename__ = "clusters"

    cluster_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 大事判定:不同源数
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Item(Base):
    """新闻条目。item_id 由 规范化URL+内容指纹 生成(不让模型编)。"""

    __tablename__ = "items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False, index=True)  # 信源注册表 id
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("clusters.cluster_id", ondelete="SET NULL"), index=True
    )
    lang: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 富化事实(value + source_id)、原始负载等,半结构化挂这里
    facts: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    embedding: Mapped["Embedding"] = relationship(
        back_populates="item", uselist=False, cascade="all, delete-orphan"
    )


class Embedding(Base):
    """条目向量(pgvector),语义近重复用。"""

    __tablename__ = "embeddings"

    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.item_id", ondelete="CASCADE"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[Item] = relationship(back_populates="embedding")


class ItemTimeline(Base):
    """时间维度新闻模型:同一条新闻跨天/反复出现的轨迹。"""

    __tablename__ = "item_timeline"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    rank: Mapped[int | None] = mapped_column(Integer)
    trigger_type: Mapped[str | None] = mapped_column(String(16))
    stars: Mapped[int | None] = mapped_column(BigInteger)  # GitHub repo 当跑 star 快照(增速排序算 delta 用)


class Run(Base):
    """每次跑的检查点(可断点续跑)。run_id + trigger_type。"""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)  # daily | event
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    # running | succeeded | failed | partial
    stage: Mapped[str | None] = mapped_column(String(32))  # 最后完成的阶段,用于续跑
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Delivery(Base):
    """投递记录。唯一键 = cluster_id + channel + trigger_type,挡重复推。"""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint(
            "cluster_id", "channel", "trigger_type", name="uq_delivery_idempotency"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cluster_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # wechat | feishu | webapp
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)  # daily | event
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="sent", nullable=False)
    # sent | skipped | failed
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Ranking(Base):
    """兴趣精排结果(阶段 4)。唯一键 = interest_key + item_id,重跑可幂等覆盖。

    interest_key 由"自然语言兴趣 + 标签"派生(不让模型编);分数可回溯到召回/规则/LLM 各层。
    """

    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint("interest_key", "item_id", name="uq_ranking_interest_item"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interest_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    interest: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[str | None] = mapped_column(String(64))
    recall_score: Mapped[float] = mapped_column(Float, nullable=False)  # 语义召回相似度
    rule_score: Mapped[float] = mapped_column(Float, nullable=False)  # 规则粗排分
    rerank_score: Mapped[float | None] = mapped_column(Float)  # LLM 相关度(rule 后端时为空)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)  # 终分(排序依据)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 名次(1 起)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # deepseek | rule
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 闸判定的下游随行数据(迁移 0012,nullable):目前只有 clip(已剪记忆:既往天数/最近已剪日/前情),
    # summarize 直接读它做③增量写稿——rank 时闸用**事件全体成员簇**对账,rankings 只存 rep_cluster_id,
    # 下游二次对账会漏(连续第3天起代表簇多为新簇),故判定结果必须随行。
    meta: Mapped[dict | None] = mapped_column(JSONB)


class Summary(Base):
    """条目级总结(阶段 5)。唯一键 = interest_key + item_id,重跑幂等覆盖。

    每条目两层文本:tldr(一句话速读)+ insight(详细白话解读);各存 _raw(含 {Fn} 占位)
    与 _rendered(verify 替换真实数字后的成稿)。数字回源对账:used_source_ids 记核对通过的来源;
    unresolved/suspect 记需复核处(任一段命中即 needs_review)。
    """

    __tablename__ = "summaries"
    __table_args__ = (
        UniqueConstraint("interest_key", "item_id", name="uq_summary_interest_item"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    interest_key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[str | None] = mapped_column(String(64))
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    tldr_raw: Mapped[str] = mapped_column(Text, nullable=False)  # 速读,含 {Fn} 占位
    tldr_rendered: Mapped[str] = mapped_column(Text, nullable=False)  # 速读,占位替换为真实数字
    insight_raw: Mapped[str] = mapped_column(Text, nullable=False)  # 详读,含 {Fn} 占位
    insight_rendered: Mapped[str] = mapped_column(Text, nullable=False)  # 详读,占位替换为真实数字
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | needs_review
    used_source_ids: Mapped[dict | None] = mapped_column(JSONB)  # 核对通过的来源 id
    unresolved: Mapped[dict | None] = mapped_column(JSONB)  # 映射不到的占位
    suspect: Mapped[dict | None] = mapped_column(JSONB)  # 未走占位的裸数字(无来源)
    backend: Mapped[str] = mapped_column(String(8), nullable=False)  # api | cli
    model: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    # 软删标记(2026-06-15 二⑦):prune 不再物理删,打 pruned_at 时间戳保留;get_summaries 过滤掉,
    # 重新产出同条目时 upsert 复活(置回 NULL)。丢数据可追溯、可恢复。
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 语义问答(v2 主线B②,见 docs/DESIGN.md §3):card_vec=卡向量(embed_passage(headline+tldr+insight)),
    # 大白话问历史靠它召回;produced_by 区分 pulsewire vs 旧系统遗留(防召混)。均 additive、nullable。
    card_vec: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    produced_by: Mapped[str | None] = mapped_column(String(16))  # pulsewire | 旧系统回填遗留值


class Digest(Base):
    """日报全局概述(阶段 5)。每个兴趣一行,重跑覆盖。"""

    __tablename__ = "digests"

    interest_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    backend: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ----- 事件线(跨天盯梢)+ 人物雷达占位。见 docs/DESIGN.md §4 ----- #
class Thread(Base):
    """事件线:把跨天、措辞各异但属同一故事的多个簇串成一条线(连续剧)。

    [step 1] 本阶段只建表不写入;判定/归线逻辑在 step 2-3。挂载单元是 cluster(见 ThreadCluster),
    item→cluster→thread 三层。线是可重算的派生数据(thread_clusters 留判定痕迹,支持 --rebuild)。
    """

    __tablename__ = "threads"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)  # AI 给这条线起的展示标题
    subject: Mapped[str | None] = mapped_column(String(128), index=True)  # A 层主体短语(归一化,匹配键)
    domain: Mapped[str | None] = mapped_column(String(32), index=True)  # 所属领域 ai/github/bio/geo
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # active(在追) | dormant(久无进展折叠) | closed
    summary: Mapped[str | None] = mapped_column(Text)  # 这条线"现状一句话"(B 每次刷新)
    heat: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 热度(挂载簇/源累计),排序用
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ThreadCluster(Base):
    """线 ↔ 簇 挂载,同时是判定日志(支撑 --rebuild 重算)。一个簇在一条线里只挂一次。"""

    __tablename__ = "thread_clusters"
    __table_args__ = (UniqueConstraint("thread_id", "cluster_id", name="uq_thread_cluster"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.thread_id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[str] = mapped_column(
        ForeignKey("clusters.cluster_id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))
    subject: Mapped[str | None] = mapped_column(String(128))  # 判定时抽到的主体(留痕)
    link_reason: Mapped[str | None] = mapped_column(String(16))  # subject | judge | new
    confidence: Mapped[float | None] = mapped_column(Float)  # B 判官置信度(A 直接命中记 1.0)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 耐久落痕(0007):挂线时冻结"当天进展",时间轴从这里读——不随 summaries 删除而丢失。
    headline: Mapped[str | None] = mapped_column(Text)  # 当天 headline(演进史的点文案)
    url: Mapped[str | None] = mapped_column(Text)  # 当天原文链接(点开读原文)
    source: Mapped[str | None] = mapped_column(Text)  # 当天来源
    progress_date: Mapped[str | None] = mapped_column(String(10))  # 进展日期 YYYY-MM-DD(解耦 runs FK)


class Person(Base):
    """[v2] 人物访谈雷达。"""

    __tablename__ = "people"

    person_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─── 选稿引擎 v2「事件池」(迁移 0009;见 docs/DESIGN.md §1)─────────
# 全部 additive:只新增、不改旧表 PK,downgrade=drop,零有损。仅 rank.engine=events 时写入;
# legacy 路径完全不碰这些表。事件 = 簇(clusters)的进一步全局合并。
class Event(Base):
    """事件:一件真实发生的事 + 其所有报道(跨域跨源,簇的合并)。

    身份稳定(serial autoincrement),成员增删**不改 event_id**(回应 codex N1);跨跑续接靠
    subject_phrase + subject_vec 质心匹配,不靠成员 hash。仅 rank.engine=events 写入。
    """

    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"))  # 首次生成于哪次跑
    canonical_headline: Mapped[str | None] = mapped_column(Text)  # 规范标题(取代表成员)
    representative_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("items.item_id", ondelete="SET NULL")
    )  # 喂 summarize 的代表成员(柱④;必属某真实簇→cluster_id 合法,闭 codex M1)
    primary_domain: Mapped[str | None] = mapped_column(String(32), index=True)  # ai/bio/geo/github
    subject_phrase: Mapped[str | None] = mapped_column(String(256), index=True)  # 聚类匹配键(复用 threads 主体短语)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)  # active/dormant
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    peak_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 主峰(新鲜度硬窗基准)
    distinct_source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # 源族折叠后不同源数
    weighted_source_score: Mapped[float | None] = mapped_column(Float)  # 按源权威加权
    velocity: Mapped[float | None] = mapped_column(Float)  # 加速度(近6h/近24h源增量)
    heat_score: Mapped[float | None] = mapped_column(Float)  # 综合热度(主排序轴)
    relevance: Mapped[dict | None] = mapped_column(JSONB)  # 对各板块兴趣的相关度(闸用)
    magnitude_floor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # 白名单实体一手首发
    subject_vec: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))  # 主体短语质心(跨跑续接/去重)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventMember(Base):
    """事件 ↔ 成员报道(挂到现有 clusters/items)。"""

    __tablename__ = "event_members"
    __table_args__ = (UniqueConstraint("event_id", "cluster_id", name="uq_event_cluster"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[str | None] = mapped_column(ForeignKey("clusters.cluster_id", ondelete="CASCADE"), index=True)
    item_id: Mapped[str | None] = mapped_column(ForeignKey("items.item_id", ondelete="SET NULL"))  # 该簇代表 item
    source: Mapped[str | None] = mapped_column(String(128))
    source_weight: Mapped[float | None] = mapped_column(Float)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_origin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # 是否首发源


class EventHeatTrace(Base):
    """事件热度轨迹(跨跑落点,给加速度 + 在追时间轴)。"""

    __tablename__ = "event_heat_trace"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.event_id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    distinct_source_count: Mapped[int | None] = mapped_column(Integer)
    heat_score: Mapped[float | None] = mapped_column(Float)


class RepoKey(Base):
    """GitHub 规范实体(owner/repo);GitHub 榜涨速按它攒快照(repo_timeline),新仓也能算 delta。"""

    __tablename__ = "repo_key"

    repo_key: Mapped[str] = mapped_column(String(255), primary_key=True)  # owner/repo 规范化(小写、去 .git)
    first_board_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 首次上榜(新仓首日涨速口径)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RepoTimeline(Base):
    """GitHub 涨速快照(按 repo_key,非 item_timeline;闭 codex N2:item_timeline.item_id 非空 FK 塞不进 owner/repo)。"""

    __tablename__ = "repo_timeline"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_key: Mapped[str] = mapped_column(
        ForeignKey("repo_key.repo_key", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    stars: Mapped[int | None] = mapped_column(BigInteger)


class Judgment(Base):
    """判决缓存(S1):LLM 判官裁决按内容+prompt 哈希落库,跨天/跨轮复用不重判(治 f20 白烧钱)。

    唯一键 (item_hash, judge_name, prompt_hash):同条目 + 同判官 + 同 prompt → 直接读裁决不调 LLM;
    prompt 改则 prompt_hash 变 = 换 key 自然失效。verdict 存判官原始裁决(bool/字符串/结构)。
    """

    __tablename__ = "judgments"
    __table_args__ = (UniqueConstraint("item_hash", "judge_name", "prompt_hash", name="uq_judgment_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    judge_name: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
