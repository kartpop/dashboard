"""Threads tables (goal 14): the story behind each task.

A `Thread` is a titled, ordered list of `ThreadStep`s. Done steps are local history;
the (at most one, always last) `next` step links a live Google Task in one of the
two pinned lists. Google is the source of truth for a next step's title / notes /
due — the row keeps the link plus a cached copy, refreshed by the reconcile on
`GET /threads` (see `app.threads.service`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Thread(SQLModel, table=True):
    __tablename__ = "thread"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    title: str = Field(max_length=300)
    archived_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


# Partial unique indexes (SQLite ≥ 3.8 and Postgres both support `WHERE`): one live
# next step per thread, and a Google task linked by at most one next step per user.
_NEXT_ONLY = text("kind = 'next'")


class ThreadStep(SQLModel, table=True):
    __tablename__ = "thread_step"
    __table_args__ = (
        Index(
            "uq_thread_step_one_next",
            "thread_id",
            unique=True,
            sqlite_where=_NEXT_ONLY,
            postgresql_where=_NEXT_ONLY,
        ),
        Index(
            "uq_thread_step_next_task",
            "user_id",
            "task_id",
            unique=True,
            sqlite_where=_NEXT_ONLY,
            postgresql_where=_NEXT_ONLY,
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: int = Field(foreign_key="thread.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    # Float rank, midpoint insertion (like the overlay). The next step is always max.
    position: float
    kind: str = Field(max_length=8)  # "done" | "next"
    label: str = Field(max_length=1000)
    note: str = Field(default="")
    occurred_on: Optional[date] = Field(default=None)  # set for done (IST date)
    # The Google link: set for next; retained on a done step that came from a task.
    tasklist_id: Optional[str] = Field(default=None, max_length=100)
    task_id: Optional[str] = Field(default=None, max_length=100)
    due: Optional[date] = Field(default=None)  # cached IST due of a next step
    # "mine" | "follow": the pinned list the linked task is (next) / was (done) in.
    # Refreshed by reconcile while next; frozen when it completes (the "via" caption).
    via: Optional[str] = Field(default=None, max_length=8)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)
