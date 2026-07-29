"""goal 11: news feed (news_item, news_feedback, news_profile)

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-28 09:00:00.000000

Three additive, entirely new tables backing the curated daily News feed. Nothing
existing changes — the news pipeline is non-Google and self-contained, so this
migration only creates tables and touches no prior column.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    op.create_table(
        "news_item",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("source", _Str(length=20), nullable=False),
        sa.Column("feed", _Str(length=200), nullable=False, server_default=""),
        sa.Column("title", _Str(), nullable=False),
        sa.Column("url", _Str(), nullable=False),
        sa.Column("canonical_url", _Str(), nullable=False, server_default=""),
        sa.Column("synopsis", _Str(), nullable=False, server_default=""),
        sa.Column("domain", _Str(length=20), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("run_date", _Str(length=10), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("why_line", _Str(), nullable=True),
        sa.Column(
            "is_serendipity", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "status", _Str(length=20), nullable=False, server_default="candidate"
        ),
    )
    op.create_index("ix_news_item_user_id", "news_item", ["user_id"])
    op.create_index("ix_news_item_source", "news_item", ["source"])
    op.create_index("ix_news_item_canonical_url", "news_item", ["canonical_url"])
    op.create_index("ix_news_item_run_date", "news_item", ["run_date"])
    op.create_index("ix_news_item_status", "news_item", ["status"])

    op.create_table(
        "news_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "item_id", sa.Integer(), sa.ForeignKey("news_item.id"), nullable=False
        ),
        sa.Column("vote", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment", _Str(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_news_feedback_user_id", "news_feedback", ["user_id"])
    op.create_index("ix_news_feedback_item_id", "news_feedback", ["item_id"])

    op.create_table(
        "news_profile",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("profile_md", _Str(), nullable=False, server_default=""),
        sa.Column("profile_prev_md", _Str(), nullable=False, server_default=""),
        sa.Column("feeds_json", _Str(), nullable=False, server_default="[]"),
        sa.Column("last_daily_at", sa.DateTime(), nullable=True),
        sa.Column("last_weekly_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("news_profile")
    op.drop_index("ix_news_feedback_item_id", "news_feedback")
    op.drop_index("ix_news_feedback_user_id", "news_feedback")
    op.drop_table("news_feedback")
    op.drop_index("ix_news_item_status", "news_item")
    op.drop_index("ix_news_item_run_date", "news_item")
    op.drop_index("ix_news_item_canonical_url", "news_item")
    op.drop_index("ix_news_item_source", "news_item")
    op.drop_index("ix_news_item_user_id", "news_item")
    op.drop_table("news_item")
