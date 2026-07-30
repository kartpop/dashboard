"""goal 12: dev view (dev_config, dev_doc_cursor, dev_issue_draft)

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-07-30 09:00:00.000000

Three additive, entirely new tables backing the Dev view (meeting notes → GitHub issue
drafts). Nothing existing changes — the dev pipeline stores its config, per-doc cursor,
and issue drafts in its own tables; the PAT lives Fernet-encrypted in `dev_config`
alongside the Google-token posture. No prior column is touched (the `dev` feature flag
rides the existing `allowed_email.features` JSON column added in goal 11).
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    op.create_table(
        "dev_config",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("pat_encrypted", _Str(), nullable=True),
        sa.Column("sources_json", _Str(), nullable=False, server_default="[]"),
        sa.Column("repos_json", _Str(), nullable=False, server_default="[]"),
        sa.Column("projects_json", _Str(), nullable=False, server_default="{}"),
        sa.Column("last_scan_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "dev_doc_cursor",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("doc_id", _Str(), nullable=False),
        sa.Column("last_processed_entry_ts", sa.DateTime(), nullable=True),
        sa.Column("boundary_entry_keys", _Str(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dev_doc_cursor_user_id", "dev_doc_cursor", ["user_id"])
    op.create_index("ix_dev_doc_cursor_doc_id", "dev_doc_cursor", ["doc_id"])

    op.create_table(
        "dev_issue_draft",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("title", _Str(), nullable=False),
        sa.Column("body", _Str(), nullable=False, server_default=""),
        sa.Column("repo", _Str(), nullable=False, server_default=""),
        sa.Column("status", _Str(length=20), nullable=False, server_default="draft"),
        sa.Column("sources", _Str(), nullable=False, server_default="[]"),
        sa.Column("project_node_id", _Str(), nullable=True),
        sa.Column("project_title", _Str(), nullable=True),
        sa.Column("issue_url", _Str(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("issue_node_id", _Str(), nullable=True),
        sa.Column(
            "project_attached", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dev_issue_draft_user_id", "dev_issue_draft", ["user_id"])
    op.create_index("ix_dev_issue_draft_status", "dev_issue_draft", ["status"])


def downgrade() -> None:
    op.drop_index("ix_dev_issue_draft_status", "dev_issue_draft")
    op.drop_index("ix_dev_issue_draft_user_id", "dev_issue_draft")
    op.drop_table("dev_issue_draft")
    op.drop_index("ix_dev_doc_cursor_doc_id", "dev_doc_cursor")
    op.drop_index("ix_dev_doc_cursor_user_id", "dev_doc_cursor")
    op.drop_table("dev_doc_cursor")
    op.drop_table("dev_config")
