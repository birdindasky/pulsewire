"""判决缓存(S1):judgments 表,判官裁决按内容+prompt 哈希落库复用

为什么:events 选稿的多道 LLM 判官(水货/够格/话题/分板/同事件)在 30 天召回窗里逐日
重判**同样的**条目,天天白烧 DeepSeek(审查 f20:event_judge 837 次/天占 47%)。把裁决按
`(item_hash, judge_name, prompt_hash)` 落库:下一轮同条目、prompt 没改 → 直接读缓存不调 LLM,
≈ 余额寿命翻倍 + rank 提速,且裁决逐字复用(选稿不变)。prompt_hash 变=换 key 自然失效。

**全 additive**:只加一张新表,不动任何旧表/PK。downgrade=drop table,零有损。
判官缓存默认关(config `rank.event_pool.judgment_cache_enabled`),A/B 证选稿零变化再开。

Revision ID: 0011_judgments
Revises: 0010_summary_card_vec
Create Date: 2026-07-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_judgments"
down_revision: Union[str, None] = "0010_summary_card_vec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "judgments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("item_hash", sa.String(64), nullable=False),   # 喂给判官的确切文本的哈希
        sa.Column("judge_name", sa.String(32), nullable=False),  # magnitude/worthiness/topic/board/same_event
        sa.Column("prompt_hash", sa.String(16), nullable=False),  # 判官 system prompt(+口径)哈希=失效键
        sa.Column("verdict", JSONB(), nullable=False),            # 缓存的裁决(bool/字符串/结构,JSONB;与模型一致)
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("item_hash", "judge_name", "prompt_hash", name="uq_judgment_key"),
    )
    # 预载扫描:按 (judge_name, prompt_hash) 一次拉本轮候选的已有裁决。
    op.create_index("ix_judgments_scan", "judgments", ["judge_name", "prompt_hash"])


def downgrade() -> None:
    op.drop_index("ix_judgments_scan", table_name="judgments")
    op.drop_table("judgments")
