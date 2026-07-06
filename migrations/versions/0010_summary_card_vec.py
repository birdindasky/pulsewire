"""语义问答(v2 主线B②):summaries 加 card_vec(卡向量)+ produced_by(来源隔离)

为什么:档案从字面搜→大白话问(见 docs/DESIGN.md §3)。card_vec=每张已发布卡的语义向量
(embed_passage(headline+tldr+insight)),问答按它召回;produced_by 区分 pulsewire 卡 vs
旧系统遗留(summaries 无来源字段、interest_key 是 hash,只能新加列隔离,闭 codex M1)。
**全 additive:只给 summaries 加 2 列、nullable、不改任何旧表/PK,downgrade=drop,零有损。**

Revision ID: 0010_summary_card_vec
Revises: 0009_events
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

EMBEDDING_DIM = 1024

revision: str = "0010_summary_card_vec"
down_revision: Union[str, None] = "0009_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("summaries", sa.Column("card_vec", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("summaries", sa.Column("produced_by", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("summaries", "produced_by")
    op.drop_column("summaries", "card_vec")
