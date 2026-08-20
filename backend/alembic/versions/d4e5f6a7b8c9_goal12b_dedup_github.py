"""goal 12b: dedup against live GitHub — draft kind, comment target, related matches

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-08-11 12:00:00.000000

`dev_issue_draft` gains the match-and-convert columns: `kind` (free-text, default
"issue"; "comment" files as one comment on an existing issue), the comment target
(`target_issue_number` / `target_issue_url`, set by code from a validated match), and
`related_issues` (JSON array of validated matches; NULL = not yet matched — the
matcher's once-per-draft NULL-guard). Filed comment drafts reuse the existing
`issue_url` / `issue_number` columns, so nothing else changes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    # server_default backfills existing rows to "issue"; new rows get the model default.
    op.add_column(
        "dev_issue_draft",
        sa.Column("kind", _Str(length=20), nullable=False, server_default="issue"),
    )
    op.add_column(
        "dev_issue_draft",
        sa.Column("target_issue_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "dev_issue_draft", sa.Column("target_issue_url", _Str(), nullable=True)
    )
    op.add_column("dev_issue_draft", sa.Column("related_issues", _Str(), nullable=True))


def downgrade() -> None:
    op.drop_column("dev_issue_draft", "related_issues")
    op.drop_column("dev_issue_draft", "target_issue_url")
    op.drop_column("dev_issue_draft", "target_issue_number")
    op.drop_column("dev_issue_draft", "kind")
