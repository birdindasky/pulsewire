"""summaries 加软删标记 pruned_at(2026-06-15 二⑦)

为什么:prune_summaries 原来物理 DELETE 旧总结——某块重试耗尽跳过的条目、或被挤出本轮的条目,
其总结直接消失、无法追溯/恢复(丢数据没痕迹)。改成软删:打 pruned_at 时间戳保留行,
get_summaries 过滤掉 pruned_at IS NOT NULL 的,重新产出同条目时 upsert 复活(置回 NULL)。
本迁移纯新增 1 个可空列,可 downgrade。

Revision ID: 0008_summary_soft_delete
Revises: 0007_thread_cluster_provenance
Create Date: 2026-06-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_summary_soft_delete"
down_revision: Union[str, None] = "0007_thread_cluster_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "summaries",
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("summaries", "pruned_at")
