"""summaries + digests: 统一总结 + 结构化对账(阶段 5)

Revision ID: 0003_summaries
Revises: 0002_rankings
Create Date: 2026-06-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_summaries"
down_revision: Union[str, None] = "0002_rankings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "summaries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("interest_key", sa.String(32), nullable=False),
        sa.Column(
            "item_id",
            sa.String(64),
            sa.ForeignKey("items.item_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cluster_id", sa.String(64)),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("summary_raw", sa.Text(), nullable=False),
        sa.Column("summary_rendered", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("used_source_ids", JSONB()),
        sa.Column("unresolved", JSONB()),
        sa.Column("suspect", JSONB()),
        sa.Column("backend", sa.String(8), nullable=False),
        sa.Column("model", sa.String(64)),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("interest_key", "item_id", name="uq_summary_interest_item"),
    )
    op.create_index("ix_summaries_interest_key", "summaries", ["interest_key"])

    op.create_table(
        "digests",
        sa.Column("interest_key", sa.String(32), primary_key=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("backend", sa.String(8), nullable=False),
        sa.Column("model", sa.String(64)),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("digests")
    op.drop_index("ix_summaries_interest_key", table_name="summaries")
    op.drop_table("summaries")
