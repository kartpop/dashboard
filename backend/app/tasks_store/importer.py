"""One-time Google Tasks → local store import (goal 17).

The ONLY remaining caller of `app.google.tasks`. For one user it copies every list,
every open task, and the tasks completed in the last `COMPLETED_WINDOW_DAYS`, keeping
Google's ids as the local ids (so thread links and groups need no repointing), and
folds the user's legacy overlay rank/group onto the rows. One DB transaction per
user: any failure rolls back and leaves `tasks_imported_at` null, so it re-runs.

Triggers (no owner action needed): the startup + hourly sweep (`scheduler.py`), and
the `tasks_ready` dependency on first load. CLI escape hatch:

    uv run python -m app.tasks_store.importer --user <email> [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import Session, delete, select

from app.auth.models import User
from app.google import tasks as tasks_client
from app.overlay.models import TaskOverlay
from app.tasks_store import service as store
from app.tasks_store.models import Task, TaskList
from app.threads.models import ThreadStep

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

_log = logging.getLogger("tasks_store.importer")

COMPLETED_WINDOW_DAYS = 30

# One import at a time in this process: the startup sweep and a first-load import
# for the same user must not race (the second would collide on the copied ids).
_lock = asyncio.Lock()


@dataclass
class ImportReport:
    user_id: int
    lists: int = 0
    open: int = 0
    completed: int = 0
    skipped_completed: int = 0
    overlay_matched: int = 0
    pinned_created: int = 0
    open_steps_unlinked: int = 0  # open thread steps whose task wasn't imported
    dry_run: bool = False


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _due_date(value: str | None) -> date | None:
    # Google's `due` is date-only, sent as midnight UTC — the UTC date IS the date.
    dt = _parse_rfc3339(value)
    return dt.astimezone(timezone.utc).date() if dt else None


async def import_user(
    session: Session,
    creds: "Credentials",
    user_id: int,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> ImportReport:
    """Import one user's Google Tasks. Commits once (or rolls back on dry-run /
    failure). A user already imported is skipped unless `force`, which first deletes
    their local lists and tasks."""
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"no user {user_id}")
    report = ImportReport(user_id=user_id, dry_run=dry_run)
    if user.tasks_imported_at is not None and not force:
        return report

    raw_lists = await tasks_client.fetch_for_import(creds)

    try:
        if force:
            session.execute(delete(Task).where(Task.user_id == user_id))
            session.execute(delete(TaskList).where(TaskList.user_id == user_id))
            session.flush()

        overlays = {
            (row.tasklist_id, row.task_id): row
            for row in session.exec(
                select(TaskOverlay).where(TaskOverlay.user_id == user_id)
            ).all()
        }
        cutoff = datetime.now(timezone.utc) - timedelta(days=COMPLETED_WINDOW_DAYS)
        imported_ids: set[str] = set()

        for li, raw in enumerate(raw_lists):
            session.add(
                TaskList(id=raw["id"], user_id=user_id, title=raw["title"], position=li)
            )
            report.lists += 1
            ordered = sorted(raw["tasks"], key=lambda t: t["position"])
            for ti, t in enumerate(ordered):
                completed_at = _parse_rfc3339(t.get("completed"))
                is_done = t["status"] == "completed"
                if is_done and (completed_at is None or completed_at < cutoff):
                    report.skipped_completed += 1
                    continue
                ov = overlays.get((raw["id"], t["id"]))
                if ov is not None:
                    report.overlay_matched += 1
                session.add(
                    Task(
                        id=t["id"],
                        user_id=user_id,
                        tasklist_id=raw["id"],
                        title=t["title"],
                        notes=t.get("notes") or None,
                        status="completed" if is_done else "needsAction",
                        due=_due_date(t.get("due")),
                        completed_at=completed_at if is_done else None,
                        position=float(ti),
                        rank=ov.rank if ov else None,
                        group_id=ov.group_id if ov else None,
                    )
                )
                imported_ids.add(t["id"])
                if is_done:
                    report.completed += 1
                else:
                    report.open += 1
        session.flush()

        before = len(store.list_rows(session, user_id))
        store.ensure_pinned_lists(session, user_id)
        session.flush()
        report.pinned_created = len(store.list_rows(session, user_id)) - before

        # Open steps whose task didn't come across are removed by the next threads
        # reconcile (the goal-14a "deleted in Google" rule) — report them here.
        report.open_steps_unlinked = sum(
            1
            for s in session.exec(
                select(ThreadStep).where(
                    ThreadStep.user_id == user_id, ThreadStep.kind == "next"
                )
            ).all()
            if s.task_id and s.task_id not in imported_ids
        )

        if dry_run:
            session.rollback()
            return report
        user.tasks_imported_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
    except Exception:
        session.rollback()
        raise

    _log.info("imported user %s: %s", user_id, asdict(report))
    return report


async def ensure_imported(session: Session, creds: "Credentials", user_id: int) -> None:
    """Import `user_id` now unless already imported (serialised per process)."""
    user = session.get(User, user_id)
    if user is not None and user.tasks_imported_at is not None:
        return
    async with _lock:
        session.expire_all()
        user = session.get(User, user_id)
        if user is None or user.tasks_imported_at is not None:
            return
        await import_user(session, creds, user_id)


# ── CLI escape hatch ─────────────────────────────────────────────────────────


def _main() -> None:
    from pathlib import Path

    from dotenv import load_dotenv

    # Like app.main: local dev reads backend/.env (prod gets env from compose).
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    from app.db import engine
    from app.google import auth as google_auth

    parser = argparse.ArgumentParser(description="Import a user's Google Tasks.")
    parser.add_argument("--user", required=True, help="the user's email")
    parser.add_argument("--force", action="store_true", help="re-import (replaces)")
    parser.add_argument("--dry-run", action="store_true", help="count, write nothing")
    args = parser.parse_args()

    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == args.user)).first()
        if user is None:
            raise SystemExit(f"no user with email {args.user}")
        creds = google_auth.load_credentials(session, user)
        report = asyncio.run(
            import_user(session, creds, user.id, force=args.force, dry_run=args.dry_run)
        )
        print(asdict(report))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _main()
