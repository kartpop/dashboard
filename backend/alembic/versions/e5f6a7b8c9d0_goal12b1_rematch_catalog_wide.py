"""goal 12b.1: re-queue matched-empty drafts for catalog-wide matching

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-20 12:00:00.000000

Matching widened from the draft's own repo to the whole catalog (a synthesiser
mis-tag matched against a repo whose issues could never match). Any non-settled draft
already settled as matched-empty ("[]") was judged under the old per-repo scope — reset
it to NULL so the next scan re-judges it against every catalog repo, exactly once.
Drafts with real stored matches keep them; filed/dismissed drafts are untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE dev_issue_draft SET related_issues = NULL "
            "WHERE related_issues = '[]' AND status IN ('draft', 'saved')"
        )
    )


def downgrade() -> None:
    # One-way data touch-up: restoring the pre-widening "[]" markers would only stop
    # the (idempotent, once-per-draft) re-match — nothing to restore.
    pass
