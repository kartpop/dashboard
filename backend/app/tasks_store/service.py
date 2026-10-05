"""Local task store (goal 17): the ONLY module that queries or mutates task rows.

Every function is scoped by `user_id`. Reads hand back the same dict shape the old
Google reshape produced (`due` / `completed` as RFC3339 strings), plus the folded-in
`rank` / `group_id`, so the overlay merge and the frontend see no difference.
Orchestration (validation, bucket rules) stays in `app.writes.service`.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timezone

from sqlmodel import Session, func, select

from app.tasks_store.models import Task, TaskList

# The two lists the dashboard renders (frontend PINNED_LIST_TITLES); every user is
# guaranteed both, created empty when the import didn't bring them.
PINNED_LIST_TITLES = ("My Tasks", "Follow-ups")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    """An app-minted id for a task or list created after the import."""
    return secrets.token_urlsafe(16)


# ── Wire shape ────────────────────────────────────────────────────────────────


def due_to_wire(due: date | None) -> str | None:
    """A stored date → the RFC3339 midnight-UTC string Google used for `due`."""
    return f"{due.isoformat()}T00:00:00.000Z" if due else None


def due_from_key(due_date: str | None) -> date | None:
    """A "YYYY-MM-DD" request value (or None → no date) → a stored date."""
    return date.fromisoformat(due_date) if due_date else None


def _completed_to_wire(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite hands naive datetimes back
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def to_wire(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "due": due_to_wire(task.due),
        "notes": task.notes,
        "parent": None,  # subtasks render flat; the store keeps no hierarchy
        "completed": _completed_to_wire(task.completed_at),
        "rank": task.rank,
        "group_id": task.group_id,
    }


# ── Reads ─────────────────────────────────────────────────────────────────────


def list_rows(session: Session, user_id: int) -> list[TaskList]:
    return list(
        session.exec(
            select(TaskList)
            .where(TaskList.user_id == user_id)
            .order_by(TaskList.position, TaskList.title)
        ).all()
    )


def get_tasklist_refs(session: Session, user_id: int) -> list[dict]:
    return [{"id": tl.id, "title": tl.title} for tl in list_rows(session, user_id)]


def get_task_lists(session: Session, user_id: int) -> list[dict]:
    """Every list with its tasks (open and completed), in the old raw shape."""
    tasks_by_list: dict[str, list[dict]] = {}
    for task in session.exec(
        select(Task)
        .where(Task.user_id == user_id)
        .order_by(Task.position, Task.created_at)
    ).all():
        tasks_by_list.setdefault(task.tasklist_id, []).append(to_wire(task))
    return [
        {"id": tl.id, "title": tl.title, "tasks": tasks_by_list.get(tl.id, [])}
        for tl in list_rows(session, user_id)
    ]


def get_list(session: Session, user_id: int, tasklist_id: str) -> TaskList | None:
    tl = session.get(TaskList, tasklist_id)
    return tl if tl is not None and tl.user_id == user_id else None


def get_task(
    session: Session, user_id: int, tasklist_id: str, task_id: str
) -> Task | None:
    task = session.get(Task, task_id)
    if task is None or task.user_id != user_id or task.tasklist_id != tasklist_id:
        return None
    return task


# ── Writes (no commit — the caller owns the transaction) ─────────────────────


def top_position(session: Session, user_id: int, tasklist_id: str) -> float:
    """A position above every task in the list (new tasks land on top, as in
    Google)."""
    lowest = session.exec(
        select(func.min(Task.position)).where(
            Task.user_id == user_id, Task.tasklist_id == tasklist_id
        )
    ).one()
    return (lowest if lowest is not None else 0.0) - 1.0


def add_task(
    session: Session,
    user_id: int,
    tasklist_id: str,
    *,
    title: str,
    notes: str | None = None,
    due: date | None = None,
    rank: float | None = None,
) -> Task:
    task = Task(
        id=new_id(),
        user_id=user_id,
        tasklist_id=tasklist_id,
        title=title,
        notes=notes or None,
        due=due,
        position=top_position(session, user_id, tasklist_id),
        rank=rank,
    )
    session.add(task)
    return task


def set_status(task: Task, status: str) -> None:
    task.status = status
    task.completed_at = _now() if status == "completed" else None


def ensure_pinned_lists(session: Session, user_id: int) -> None:
    """Create whichever pinned list is missing (matched by title, case-insensitive)."""
    rows = list_rows(session, user_id)
    have = {(tl.title or "").strip().lower() for tl in rows}
    next_pos = max((tl.position for tl in rows), default=-1.0) + 1.0
    for title in PINNED_LIST_TITLES:
        if title.lower() not in have:
            session.add(
                TaskList(id=new_id(), user_id=user_id, title=title, position=next_pos)
            )
            next_pos += 1.0
