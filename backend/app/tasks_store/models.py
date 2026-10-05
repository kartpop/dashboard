"""The dashboard's own task store (goal 17) — the source of truth for tasks.

Imported rows keep their Google ids as local ids (so thread links and groups need no
repointing); tasks created after the import get app-minted ids. Ids are strings on
the wire and in the DB, exactly as before. `rank` / `group_id` are the old overlay
columns, folded in — `task_overlay` is legacy, read only by the importer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskList(SQLModel, table=True):
    __tablename__ = "task_list"

    id: str = Field(primary_key=True, max_length=100)
    user_id: int = Field(foreign_key="user.id", index=True)
    title: str = Field(default="", max_length=1024)
    position: float = Field(default=0.0)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class Task(SQLModel, table=True):
    __tablename__ = "task"

    id: str = Field(primary_key=True, max_length=100)
    user_id: int = Field(foreign_key="user.id", index=True)
    tasklist_id: str = Field(foreign_key="task_list.id", index=True, max_length=100)
    title: str = Field(default="")
    notes: Optional[str] = Field(default=None)
    status: str = Field(default="needsAction", max_length=16)  # | "completed"
    due: Optional[date] = Field(default=None)  # date-only, like Google's `due`
    completed_at: Optional[datetime] = Field(default=None)
    # List order for unranked tasks (Google's manual order on import; new tasks go
    # on top). Ranked tasks order by `rank` within their bucket, as before.
    position: float = Field(default=0.0)
    rank: Optional[float] = Field(default=None)
    group_id: Optional[int] = Field(default=None, foreign_key="task_group.id")
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)
