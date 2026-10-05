"""Write orchestration: task writes (local store, goal 17) and the notes writer.

Task writes go to the dashboard's own task store (`app.tasks_store`) — no Google
Tasks call is reachable from here since goal 17. This module owns validation and
the bucket rules (idempotent reschedule, group-in-destination-bucket); the store owns
the rows. The notes writer (`append_note`) is still a Google Docs write. See
`.claude/rules/writes.md`.
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

from app.errors import ApiError
from app.google import docs as docs_client
from app.overlay import service as overlay_svc
from app.tasks_store import service as store

_NO_DATE = "NO_DATE"
_UNSET: Any = object()

_log = logging.getLogger("writes.service")
_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

# Docs whose folder-ancestry has been verified once — the gate is idempotent and a
# doc can't leave its folder mid-process, so we cache the confirmation per doc id.
_ancestry_ok: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_task(session: Session, user_id: int, tasklist_id: str, task_id: str):
    task = store.get_task(session, user_id, tasklist_id, task_id)
    if task is None:
        raise ApiError(404, "task_not_found", "Task not found.")
    return task


def _require_group_in(
    session: Session, user_id: int, group_id: int | None, tasklist_id: str, bucket: str
) -> None:
    if group_id is None:
        return
    grp = overlay_svc.get_group(session, user_id, group_id, tasklist_id)
    if grp is None or grp.bucket_key != bucket:
        raise ApiError(
            422,
            "group_wrong_bucket",
            "group_id must reference a group in the destination bucket.",
        )


async def reschedule(
    session: Session,
    user_id: int,
    tasklist_id: str,
    task_id: str,
    due_date: str | None,
    rank: float | None,
    group_id: int | None,
) -> dict:
    """Reschedule a task across date-buckets (due date + rank + group).

    `group_id` must reference a group in the destination bucket (422 otherwise); it
    is always set explicitly (None ungroups). Same-bucket is a no-op on the due.
    """
    task = _require_task(session, user_id, tasklist_id, task_id)
    target_bucket = due_date or _NO_DATE
    _require_group_in(session, user_id, group_id, tasklist_id, target_bucket)

    task.due = store.due_from_key(due_date)
    if rank is not None:
        task.rank = rank
    task.group_id = group_id
    task.updated_at = _now()
    session.commit()
    session.refresh(task)

    return {
        "tasklist_id": tasklist_id,
        "task_id": task_id,
        "due": store.due_to_wire(task.due),
        "rank": task.rank,
        "group_id": task.group_id,
    }


async def move(
    session: Session,
    user_id: int,
    tasklist_id: str,
    task_id: str,
    target_list_id: str,
    rank: float | None,
    due_date: Any = _UNSET,
    group_id: int | None = None,
) -> dict:
    """Move a task to another list — an in-place update; the task keeps its id.

    Goal 6 (cross-list drag): the drop may also change the date bucket and land in
    a destination group.
      - `due_date is _UNSET` → preserve the task's due (menu/same-bucket drop); an
        explicit value (a "YYYY-MM-DD" str, or None to clear → NO_DATE) overrides it.
      - `group_id` must reference a group in the destination `(target_list, bucket)`
        (422 otherwise); None = ungrouped.

    The response keeps `new_task_id` (always equal to `task_id` since goal 17) so
    existing clients need no change.
    """
    if target_list_id == tasklist_id:
        raise ApiError(400, "same_list", "Task is already in that list.")

    task = _require_task(session, user_id, tasklist_id, task_id)
    if store.get_list(session, user_id, target_list_id) is None:
        raise ApiError(404, "list_not_found", "Target list not found.")

    if due_date is _UNSET:
        new_due = task.due
    else:
        new_due = store.due_from_key(due_date)
    target_bucket = new_due.isoformat() if new_due else _NO_DATE
    _require_group_in(session, user_id, group_id, target_list_id, target_bucket)

    task.position = store.top_position(session, user_id, target_list_id)
    task.tasklist_id = target_list_id
    task.due = new_due
    task.rank = rank
    task.group_id = group_id
    task.updated_at = _now()
    session.commit()
    session.refresh(task)

    return {
        "target_list_id": target_list_id,
        "new_task_id": task.id,
        "rank": task.rank,
        "group_id": task.group_id,
    }


# ── Content CRUD (goal 4a) ─────────────────────────────────────────────────────


async def create_task(
    session: Session,
    user_id: int,
    tasklist_id: str,
    title: str,
    rank: float | None,
    notes: str | None = None,
    due_date: str | None = None,
) -> dict:
    """Create a task. Returns the merged task shape so the client can
    insert-from-response (no refetch)."""
    if not title.strip():
        raise ApiError(400, "empty_title", "Task title must not be empty.")
    if store.get_list(session, user_id, tasklist_id) is None:
        raise ApiError(404, "list_not_found", "Task list not found.")

    task = store.add_task(
        session,
        user_id,
        tasklist_id,
        title=title,
        notes=notes,
        due=store.due_from_key(due_date),
        rank=rank,
    )
    session.commit()
    session.refresh(task)
    return {**store.to_wire(task), "type": "task"}


async def update_content(
    session: Session,
    user_id: int,
    tasklist_id: str,
    task_id: str,
    title: Any = _UNSET,
    notes: Any = _UNSET,
    status: Any = _UNSET,
) -> dict:
    """Patch a task's content fields (title / notes / status). Only fields
    explicitly provided are written; completion rides `status`."""
    if title is not _UNSET and not str(title).strip():
        raise ApiError(400, "empty_title", "Task title must not be empty.")
    if status is not _UNSET and status not in ("needsAction", "completed"):
        raise ApiError(400, "bad_status", "status must be needsAction or completed.")

    task = _require_task(session, user_id, tasklist_id, task_id)
    if title is not _UNSET:
        task.title = title
    if notes is not _UNSET:
        task.notes = notes or None
    if status is not _UNSET and status != task.status:
        store.set_status(task, status)
    task.updated_at = _now()
    session.commit()
    session.refresh(task)
    return {**store.to_wire(task), "type": "task"}


async def delete(
    session: Session,
    user_id: int,
    tasklist_id: str,
    task_id: str,
) -> dict:
    """Delete a task. Immediate on the backend — the ~5s deferral + undo is a
    frontend concern (an undo means this endpoint is never called)."""
    task = _require_task(session, user_id, tasklist_id, task_id)
    session.delete(task)
    session.commit()
    return {"tasklist_id": tasklist_id, "task_id": task_id, "deleted": True}


async def rename_list(
    session: Session, user_id: int, tasklist_id: str, title: str
) -> dict:
    """Rename a task list."""
    if not title.strip():
        raise ApiError(400, "empty_title", "List title must not be empty.")
    tl = store.get_list(session, user_id, tasklist_id)
    if tl is None:
        raise ApiError(404, "list_not_found", "Task list not found.")
    tl.title = title
    tl.updated_at = _now()
    session.commit()
    return {"id": tl.id, "title": tl.title}


# ── Notes writer (goal 7) ──────────────────────────────────────────────────────
#
# The second live Google surface: the auto-router appends a captured note VERBATIM
# to the top of one configured Doc. Insert-only — no delete, no overwrite, no status
# write. `append_note` is a **router-only** caller (writes.md); doc/folder ids come
# from config, never from LLM output. See docs/goals/architecture/drive-access-scoping.md.


def format_note_heading(dt: datetime) -> str:
    """The locked timestamp format, e.g. `6-July-2026, 8:41 PM IST`.

    Built from date/time components (not `%-d`/`%-I`) so it is identical on macOS
    and Linux. `dt` is expected in IST; its wall-clock components are used as-is.
    """
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.day}-{dt.strftime('%B')}-{dt.year}, {hour}:{dt.minute:02d} {ampm} IST"


async def _assert_in_notes_folder(
    creds: "Credentials", doc_id: str, folder_id: str | None
) -> None:
    """Folder-ancestry gate: verify `doc_id`'s parent chain reaches `folder_id`.

    Fail-closed — a missing folder id, an unreachable doc, or an error anywhere in
    the walk means we do NOT write (the caller leaves the entry re-routable). Cached
    per doc id after the first success (a doc can't leave its folder mid-process).
    """
    if not folder_id:
        raise ApiError(
            500, "notes_folder_unset", "The user's notes folder is not configured."
        )
    if doc_id in _ancestry_ok:
        return
    try:
        reached = await _walk_to_folder(creds, doc_id, folder_id)
    except Exception as exc:
        raise ApiError(
            502,
            "notes_ancestry_check_failed",
            "Could not verify the notes Doc's folder.",
        ) from exc
    if not reached:
        raise ApiError(
            422,
            "notes_doc_outside_folder",
            "The configured notes Doc is not inside the user's notes folder.",
        )
    _ancestry_ok.add(doc_id)


async def _walk_to_folder(
    creds: "Credentials", file_id: str, folder_id: str, max_depth: int = 10
) -> bool:
    """Walk up `file_id`'s parents (bounded) looking for `folder_id`.

    The common case — a bootstrap-created doc sitting directly in the folder —
    resolves on the first hop. Deeper nesting relies on the intermediate folders
    being app-visible; if a hand-made ancestor is unreadable the walk raises and
    the gate fails closed (correct: we couldn't prove containment)."""
    seen: set[str] = set()
    frontier = [file_id]
    for _ in range(max_depth):
        parents: list[str] = []
        for fid in frontier:
            if fid in seen:
                continue
            seen.add(fid)
            parents.extend(await docs_client.get_parents(creds, fid))
        if folder_id in parents:
            return True
        if not parents:
            return False
        frontier = parents
    return False


async def append_note(
    creds: "Credentials",
    doc_id: str,
    folder_id: str | None,
    body_text: str,
    summary: str | None = None,
    keywords: list[str] | None = None,
) -> dict:
    """Append a verbatim note to the top of the configured Doc under an H3 timestamp.

    Router-only write. Insert-only — never deletes or overwrites the Doc. The
    ancestry gate runs first (fail-closed); a Docs error is surfaced as an ApiError
    so the entry stays re-routable (route-once marks routed only on success).

    `summary` (H4 one-liner) and `keywords` (optional H5 line) are the only
    LLM-authored lines (goal 9); the raw `body_text` stays verbatim. Empty/missing
    summary or keywords degrade to the earlier shapes.
    """
    await _assert_in_notes_folder(creds, doc_id, folder_id)
    heading = format_note_heading(datetime.now(_IST))
    try:
        await docs_client.insert_note(
            creds, doc_id, heading, body_text, summary, keywords
        )
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            502, "google_docs_write_failed", "Could not append the note to the Doc."
        ) from exc
    _log.info("appended note to Doc %s under heading %r", doc_id, heading)
    return {"doc_id": doc_id, "heading": heading}
