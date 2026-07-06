"""选稿引擎 v2「事件池」:events / event_members / event_heat_trace / repo_key / repo_timeline

为什么:从根重做选稿核(见 docs/DESIGN.md §1,五柱),需"事件"成为一等公民(簇的全局合并)+ GitHub 涨速按
repo_key 攒快照。**全部 additive:只新增 5 表、不改任何旧表 PK,downgrade=drop,零有损**。仅
rank.engine=events 时写入;legacy 路径完全不碰。设计见 docs/DESIGN.md §1。

Revision ID: 0009_events
Revises: 0008_summary_soft_delete
Create Date: 2026-06-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

EMBEDDING_DIM = 1024

revision: str = "0009_events"
down_revision: Union[str, None] = "0008_summary_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # events:一件真实发生的事(簇的全局合并)。event_id serial 稳定身份(成员增删不改)。
    op.create_table(
        "events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("canonical_headline", sa.Text(), nullable=True),
        sa.Column("representative_item_id", sa.String(64), nullable=True),
        sa.Column("primary_domain", sa.String(32), nullable=True),
        sa.Column("subject_phrase", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("peak_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("distinct_source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("weighted_source_score", sa.Float(), nullable=True),
        sa.Column("velocity", sa.Float(), nullable=True),
        sa.Column("heat_score", sa.Float(), nullable=True),
        sa.Column("relevance", JSONB(), nullable=True),
        sa.Column("magnitude_floor", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subject_vec", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["representative_item_id"], ["items.item_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_events_primary_domain", "events", ["primary_domain"])
    op.create_index("ix_events_subject_phrase", "events", ["subject_phrase"])

    # event_members:事件 ↔ 成员簇/item
    op.create_table(
        "event_members",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_id", sa.String(64), nullable=True),
        sa.Column("item_id", sa.String(64), nullable=True),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("source_weight", sa.Float(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_origin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.cluster_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_id", "cluster_id", name="uq_event_cluster"),
    )
    op.create_index("ix_event_members_event_id", "event_members", ["event_id"])
    op.create_index("ix_event_members_cluster_id", "event_members", ["cluster_id"])

    # event_heat_trace:热度轨迹(跨跑落点)
    op.create_table(
        "event_heat_trace",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("distinct_source_count", sa.Integer(), nullable=True),
        sa.Column("heat_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.event_id"], ondelete="CASCADE"),
    )
    op.create_index("ix_event_heat_trace_event_id", "event_heat_trace", ["event_id"])

    # repo_key:GitHub 规范实体(owner/repo)
    op.create_table(
        "repo_key",
        sa.Column("repo_key", sa.String(255), primary_key=True),
        sa.Column("first_board_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # repo_timeline:GitHub 涨速快照(按 repo_key,非 item_timeline)
    op.create_table(
        "repo_timeline",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("repo_key", sa.String(255), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("stars", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["repo_key"], ["repo_key.repo_key"], ondelete="CASCADE"),
    )
    op.create_index("ix_repo_timeline_repo_key", "repo_timeline", ["repo_key"])


def downgrade() -> None:
    op.drop_index("ix_repo_timeline_repo_key", table_name="repo_timeline")
    op.drop_table("repo_timeline")
    op.drop_table("repo_key")
    op.drop_index("ix_event_heat_trace_event_id", table_name="event_heat_trace")
    op.drop_table("event_heat_trace")
    op.drop_index("ix_event_members_cluster_id", table_name="event_members")
    op.drop_index("ix_event_members_event_id", table_name="event_members")
    op.drop_table("event_members")
    op.drop_index("ix_events_subject_phrase", table_name="events")
    op.drop_index("ix_events_primary_domain", table_name="events")
    op.drop_table("events")
