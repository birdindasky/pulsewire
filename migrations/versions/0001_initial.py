"""initial schema: items / clusters / embeddings / timeline / runs / deliveries / threads / people

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "clusters",
        sa.Column("cluster_id", sa.String(64), primary_key=True),
        sa.Column("first_item_id", sa.String(64), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stage", sa.String(32)),
        sa.Column("error", sa.Text()),
        sa.Column("meta", JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text()),
        sa.Column("content_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "cluster_id",
            sa.String(64),
            sa.ForeignKey("clusters.cluster_id", ondelete="SET NULL"),
        ),
        sa.Column("lang", sa.String(16)),
        sa.Column("category", sa.String(64)),
        sa.Column("region", sa.String(32)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("facts", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_items_source", "items", ["source"])
    op.create_index("ix_items_normalized_url", "items", ["normalized_url"])
    op.create_index("ix_items_content_fingerprint", "items", ["content_fingerprint"])
    op.create_index("ix_items_cluster_id", "items", ["cluster_id"])
    op.create_index("ix_items_published_at", "items", ["published_at"])

    op.create_table(
        "embeddings",
        sa.Column(
            "item_id",
            sa.String(64),
            sa.ForeignKey("items.item_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # 余弦近邻索引(语义去重)
    op.execute(
        "CREATE INDEX ix_embeddings_hnsw ON embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "item_timeline",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "item_id",
            sa.String(64),
            sa.ForeignKey("items.item_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("rank", sa.Integer()),
        sa.Column("trigger_type", sa.String(16)),
    )
    op.create_index("ix_item_timeline_item_id", "item_timeline", ["item_id"])

    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("trigger_type", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="sent"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "cluster_id", "channel", "trigger_type", name="uq_delivery_idempotency"
        ),
    )

    # v2 占位表
    op.create_table(
        "threads",
        sa.Column("thread_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "people",
        sa.Column("person_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("weight", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("people")
    op.drop_table("threads")
    op.drop_table("deliveries")
    op.drop_index("ix_item_timeline_item_id", table_name="item_timeline")
    op.drop_table("item_timeline")
    op.execute("DROP INDEX IF EXISTS ix_embeddings_hnsw")
    op.drop_table("embeddings")
    for ix in (
        "ix_items_published_at",
        "ix_items_cluster_id",
        "ix_items_content_fingerprint",
        "ix_items_normalized_url",
        "ix_items_source",
    ):
        op.drop_index(ix, table_name="items")
    op.drop_table("items")
    op.drop_table("runs")
    op.drop_table("clusters")
