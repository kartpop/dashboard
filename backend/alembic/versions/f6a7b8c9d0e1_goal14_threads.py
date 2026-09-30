"""goal 14: threads (thread, thread_step)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-29 12:00:00.000000

Two additive, entirely new tables backing the Threads panel. A thread is an ordered
list of steps; done steps are local history and the (at most one, always last) next
step links a Google Task in a pinned list. Two partial unique indexes back the
invariants cheaply on both SQLite and Postgres: one next step per thread, and a
Google task linked by at most one next step per user. Nothing existing changes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString
_NEXT_ONLY = sa.text("kind = 'next'")


def upgrade() -> None:
    op.create_table(
        "thread",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("title", _Str(length=300), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_thread_user_id", "thread", ["user_id"])

    op.create_table(
        "thread_step",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "thread_id", sa.Integer(), sa.ForeignKey("thread.id"), nullable=False
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("kind", _Str(length=8), nullable=False),
        sa.Column("label", _Str(length=1000), nullable=False),
        sa.Column("note", _Str(), nullable=False, server_default=""),
        sa.Column("occurred_on", sa.Date(), nullable=True),
        sa.Column("tasklist_id", _Str(length=100), nullable=True),
        sa.Column("task_id", _Str(length=100), nullable=True),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("via", _Str(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_thread_step_thread_id", "thread_step", ["thread_id"])
    op.create_index("ix_thread_step_user_id", "thread_step", ["user_id"])
    op.create_index(
        "uq_thread_step_one_next",
        "thread_step",
        ["thread_id"],
        unique=True,
        sqlite_where=_NEXT_ONLY,
        postgresql_where=_NEXT_ONLY,
    )
    op.create_index(
        "uq_thread_step_next_task",
        "thread_step",
        ["user_id", "task_id"],
        unique=True,
        sqlite_where=_NEXT_ONLY,
        postgresql_where=_NEXT_ONLY,
    )


def downgrade() -> None:
    op.drop_index("uq_thread_step_next_task", table_name="thread_step")
    op.drop_index("uq_thread_step_one_next", table_name="thread_step")
    op.drop_index("ix_thread_step_user_id", table_name="thread_step")
    op.drop_index("ix_thread_step_thread_id", table_name="thread_step")
    op.drop_table("thread_step")
    op.drop_index("ix_thread_user_id", table_name="thread")
    op.drop_table("thread")
