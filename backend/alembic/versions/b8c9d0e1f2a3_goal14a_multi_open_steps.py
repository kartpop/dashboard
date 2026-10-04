"""goal 14a: several open steps per thread

Revision ID: b8c9d0e1f2a3
Revises: f6a7b8c9d0e1
Create Date: 2026-10-02 12:00:00.000000

Drops goal 14's one-next-step-per-thread partial unique index so a thread can hold
any number of open (`kind = 'next'`) steps. The one-link-per-task index stays. No
data changes. Downgrade recreates the index and fails if any thread holds two or
more open steps — accepted, downgrade is a dev-only path.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEXT_ONLY = sa.text("kind = 'next'")


def upgrade() -> None:
    op.drop_index("uq_thread_step_one_next", table_name="thread_step")


def downgrade() -> None:
    op.create_index(
        "uq_thread_step_one_next",
        "thread_step",
        ["thread_id"],
        unique=True,
        sqlite_where=_NEXT_ONLY,
        postgresql_where=_NEXT_ONLY,
    )
