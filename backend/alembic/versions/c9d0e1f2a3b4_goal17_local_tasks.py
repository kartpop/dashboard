"""goal 17: the dashboard owns tasks (task_list, task, user.tasks_imported_at)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-05 12:00:00.000000

Additive only. Two new tables hold each user's task lists and tasks (rank/group
folded in from the overlay), and `user.tasks_imported_at` marks the one-time Google
Tasks import. No data is copied here — the import needs Google calls and per-user
credentials, so it runs in the app (`app.tasks_store.importer`), never in Alembic.
`task_overlay` stays (the importer reads it; it is the rollback path) until goal 17b.
"""

from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    op.create_table(
        "task_list",
        sa.Column("id", _Str(length=100), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("title", _Str(length=1024), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_task_list_user_id", "task_list", ["user_id"])

    op.create_table(
        "task",
        sa.Column("id", _Str(length=100), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column(
            "tasklist_id",
            _Str(length=100),
            sa.ForeignKey("task_list.id"),
            nullable=False,
        ),
        sa.Column("title", _Str(), nullable=False),
        sa.Column("notes", _Str(), nullable=True),
        sa.Column("status", _Str(length=16), nullable=False),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("rank", sa.Float(), nullable=True),
        sa.Column(
            "group_id", sa.Integer(), sa.ForeignKey("task_group.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_task_user_id", "task", ["user_id"])
    op.create_index("ix_task_tasklist_id", "task", ["tasklist_id"])

    with op.batch_alter_table("user") as batch:
        batch.add_column(sa.Column("tasks_imported_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user") as batch:
        batch.drop_column("tasks_imported_at")
    op.drop_index("ix_task_tasklist_id", table_name="task")
    op.drop_index("ix_task_user_id", table_name="task")
    op.drop_table("task")
    op.drop_index("ix_task_list_user_id", table_name="task_list")
    op.drop_table("task_list")
