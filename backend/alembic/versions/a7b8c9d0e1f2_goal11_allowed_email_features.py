"""goal 11: per-user feature flags on allowed_email

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-07-28 12:00:00.000000

Adds a `features` JSON-text column to `allowed_email` so the superuser can toggle
per-user features (e.g. News) from the admin UI. Replaces the NEWS_ENABLED_EMAILS env
var. Additive and backfilled with the empty object, so existing invites keep every
feature off (superusers are always-on in code and need no row).
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    op.add_column(
        "allowed_email",
        sa.Column("features", _Str(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("allowed_email", "features")
