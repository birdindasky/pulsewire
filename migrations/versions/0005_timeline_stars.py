"""item_timeline 加 stars 列:为 GitHub 热榜「真·增速排序」铺路

热榜现按绝对 stars 排;真增速需跨天 star 增量。先在 item_timeline 落每跑 star 快照
(observed_at + stars + rank),数据攒够(≥2 天)后增速排序才能算 delta。本迁移只加列,
不动现有逻辑。

Revision ID: 0005_timeline_stars
Revises: 0004_insight
Create Date: 2026-06-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_timeline_stars"
down_revision: Union[str, None] = "0004_insight"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("item_timeline", sa.Column("stars", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("item_timeline", "stars")
