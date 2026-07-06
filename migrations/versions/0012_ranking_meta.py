"""已剪记忆(clip memory):rankings 加 meta(闸判定的下游随行数据)

为什么:已剪记忆闸在 rank 时用**事件全体成员簇**对账在追线台账,但 rankings 只存 rep_cluster_id
——连续第 3 天起代表簇往往是新簇,summarize(在 threads 归线之前跑)拿 rep_cluster_id 二次查账
必然漏掉前情 → ③增量写稿静默失效。正解:闸判定时把 prev_report(既往天数/最近已剪日/前情)
写进 rankings.meta 随行下游,summarize 直接读、零二次对账(读到的就是闸真实看到的)。
**全 additive:只给 rankings 加 1 列、nullable、不改任何旧表/PK,downgrade=drop,零有损。**

Revision ID: 0012_ranking_meta
Revises: 0011_judgments
Create Date: 2026-07-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012_ranking_meta"
down_revision: Union[str, None] = "0011_judgments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("rankings", sa.Column("meta", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("rankings", "meta")
