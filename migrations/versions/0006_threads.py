"""事件线 step 1:扩展 threads + 建 thread_clusters(只建表不写入)

跨天盯梢功能的数据地基(见 docs/DESIGN.md §4):
- threads 占位表加列:subject(A层主体键)/domain/status/summary/heat/first_seen_at/last_seen_at。
- 新建 thread_clusters:线↔簇挂载,兼判定日志(link_reason/confidence/subject 留痕,支撑 --rebuild)。
本迁移纯新增、可 downgrade,不动现有逻辑。

Revision ID: 0006_threads
Revises: 0005_timeline_stars
Create Date: 2026-06-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_threads"
down_revision: Union[str, None] = "0005_timeline_stars"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # threads 加列(status/heat 非空给 server_default,空表也安全)
    op.add_column("threads", sa.Column("subject", sa.String(128), nullable=True))
    op.add_column("threads", sa.Column("domain", sa.String(32), nullable=True))
    op.add_column("threads", sa.Column("status", sa.String(16), nullable=False, server_default="active"))
    op.add_column("threads", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("threads", sa.Column("heat", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("threads", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("threads", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_threads_subject", "threads", ["subject"])
    op.create_index("ix_threads_domain", "threads", ["domain"])

    # thread_clusters:线↔簇挂载 + 判定日志
    op.create_table(
        "thread_clusters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("cluster_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("link_reason", sa.String(16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.thread_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.cluster_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("thread_id", "cluster_id", name="uq_thread_cluster"),
    )
    op.create_index("ix_thread_clusters_thread_id", "thread_clusters", ["thread_id"])
    op.create_index("ix_thread_clusters_cluster_id", "thread_clusters", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_thread_clusters_cluster_id", table_name="thread_clusters")
    op.drop_index("ix_thread_clusters_thread_id", table_name="thread_clusters")
    op.drop_table("thread_clusters")
    op.drop_index("ix_threads_domain", table_name="threads")
    op.drop_index("ix_threads_subject", table_name="threads")
    for col in ("last_seen_at", "first_seen_at", "heat", "summary", "status", "domain", "subject"):
        op.drop_column("threads", col)
