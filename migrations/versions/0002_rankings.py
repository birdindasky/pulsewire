"""rankings: 兴趣精排结果(阶段 4)

Revision ID: 0002_rankings
Revises: 0001_initial
Create Date: 2026-06-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_rankings"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rankings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("interest_key", sa.String(32), nullable=False),
        sa.Column("interest", sa.Text(), nullable=False),
        sa.Column("tags", JSONB()),
        sa.Column(
            "item_id",
            sa.String(64),
            sa.ForeignKey("items.item_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cluster_id", sa.String(64)),
        sa.Column("recall_score", sa.Float(), nullable=False),
        sa.Column("rule_score", sa.Float(), nullable=False),
        sa.Column("rerank_score", sa.Float()),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("interest_key", "item_id", name="uq_ranking_interest_item"),
    )
    op.create_index("ix_rankings_interest_key", "rankings", ["interest_key"])


def downgrade() -> None:
    op.drop_index("ix_rankings_interest_key", table_name="rankings")
    op.drop_table("rankings")
