"""事件线 step 4/5:thread_clusters 加耐久落痕列(headline/url/source/progress_date)

为什么(见 docs/DESIGN.md §4):
- summaries 每跑被删(只留本轮选中),时间轴若从 summaries 取 headline,旧日进展点会消失。
- 故在挂线时把"当天 headline/url/source/进展日期"落痕进 thread_clusters,时间轴从落痕读:
  既耐久(不随 summary 删除丢失),又是"当天原话"(演进史本该冻结当天措辞)。
- progress_date 独立存,解耦 runs FK(归档重放 --rebuild 的老日期在 runs 表里并不存在)。
本迁移纯新增 4 个可空列,可 downgrade。

Revision ID: 0007_thread_cluster_provenance
Revises: 0006_threads
Create Date: 2026-06-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_thread_cluster_provenance"
down_revision: Union[str, None] = "0006_threads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("thread_clusters", sa.Column("headline", sa.Text(), nullable=True))
    op.add_column("thread_clusters", sa.Column("url", sa.Text(), nullable=True))
    op.add_column("thread_clusters", sa.Column("source", sa.Text(), nullable=True))
    op.add_column("thread_clusters", sa.Column("progress_date", sa.String(10), nullable=True))


def downgrade() -> None:
    for col in ("progress_date", "source", "url", "headline"):
        op.drop_column("thread_clusters", col)
