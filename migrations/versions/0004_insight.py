"""summaries: 两层文本 tldr + insight(措辞白话化 + 详读)

把单一 summary_* 拆成 tldr_*(一句话速读)+ insight_*(详细白话解读),各含 _raw/_rendered。
旧 summary 内容 backfill 进 tldr 保留历史;insight 留空待下次 run 重生成。

Revision ID: 0004_insight
Revises: 0003_summaries
Create Date: 2026-06-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_insight"
down_revision: Union[str, None] = "0003_summaries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 加新列(NOT NULL + server_default '' 让既有行可填),随后 backfill tldr ← 旧 summary
    op.add_column("summaries", sa.Column("tldr_raw", sa.Text(), nullable=False, server_default=""))
    op.add_column("summaries", sa.Column("tldr_rendered", sa.Text(), nullable=False, server_default=""))
    op.add_column("summaries", sa.Column("insight_raw", sa.Text(), nullable=False, server_default=""))
    op.add_column("summaries", sa.Column("insight_rendered", sa.Text(), nullable=False, server_default=""))
    op.execute("UPDATE summaries SET tldr_raw=summary_raw, tldr_rendered=summary_rendered")
    op.drop_column("summaries", "summary_raw")
    op.drop_column("summaries", "summary_rendered")
    # 去掉建表时的 server_default(应用层总会显式写值,不依赖默认)
    for col in ("tldr_raw", "tldr_rendered", "insight_raw", "insight_rendered"):
        op.alter_column("summaries", col, server_default=None)


def downgrade() -> None:
    op.add_column("summaries", sa.Column("summary_raw", sa.Text(), nullable=False, server_default=""))
    op.add_column("summaries", sa.Column("summary_rendered", sa.Text(), nullable=False, server_default=""))
    # 有损补救:回滚要丢掉 insight 列,但 insight 文本(详读深度解读)是更值钱的内容——
    # 不只取 tldr,而是 tldr + insight 合并进 summary,免回滚时把深度解读静默丢光。
    op.execute(
        """
        UPDATE summaries SET
          summary_raw = CASE WHEN insight_raw <> ''
                             THEN tldr_raw || E'\n\n' || insight_raw ELSE tldr_raw END,
          summary_rendered = CASE WHEN insight_rendered <> ''
                                  THEN tldr_rendered || E'\n\n' || insight_rendered
                                  ELSE tldr_rendered END
        """
    )
    op.alter_column("summaries", "summary_raw", server_default=None)
    op.alter_column("summaries", "summary_rendered", server_default=None)
    op.drop_column("summaries", "insight_rendered")
    op.drop_column("summaries", "insight_raw")
    op.drop_column("summaries", "tldr_rendered")
    op.drop_column("summaries", "tldr_raw")
