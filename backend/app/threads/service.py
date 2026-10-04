"""Threads service (goal 14): storage, reconcile (Google → threads), and link writes.

A thread's next ("open") steps — any number since goal 14a — are real Google Tasks
in the two pinned lists; Google is the source of truth for their title / notes /
due. Ranks keep every done step before every open step (the open block); the API
serves done steps by rank, then open steps by due. `list_threads` reconciles every
linked step against a fresh fetch before responding:

  - linked task completed          → that step becomes done (dated by completion) and
                                     joins the end of the done history
  - done step's task back to open  → open again, at the end of the open block, unless
                                     another open step links that task (undo)
  - linked task gone               → that step is removed (dangles if it was the last)
  - linked task still open         → refresh the cached label / note / due / list

Google writes go through `app.writes.service` ONLY, and only these four:
`create_task`, `update_content`, `reschedule`, `move` (AST-pinned in the tests).
Threads **never** deletes a Google task — unlinking a next step or archiving a
thread leaves the task in its list. Google write first, DB write second, same
request: an orphan task in a list is the accepted failure mode (see writes.md).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from app.errors import ApiError
from app.google import tasks as tasks_client
from app.google.calendar import today_ist
from app.overlay.service import _IST
from app.threads.models import Thread, ThreadStep
from app.writes import service as writes_svc

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

_log = logging.getLogger("threads.service")

_UNSET: Any = object()

# Which pinned list says whose move it is: My Tasks = mine, Follow-ups = theirs.
# Titles match the dashboard's PINNED_LIST_TITLES (and router.service's).
LIST_TITLES = {"mine": "My Tasks", "follow": "Follow-ups"}
LIST_KEYS = tuple(LIST_TITLES)

_GAP = 1000.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_date(rfc3339: str | None) -> date | None:
    if not rfc3339:
        return None
    dt = datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
    return dt.astimezone(_IST).date()


def _pinned_ids(lists: list[dict]) -> dict[str, str]:
    """`{"mine": list_id, "follow": list_id}` for whichever pinned lists exist."""
    by_title = {(tl.get("title") or "").strip().lower(): tl["id"] for tl in lists}
    return {
        key: by_title[title.lower()]
        for key, title in LIST_TITLES.items()
        if title.lower() in by_title
    }


def _list_key(tasklist_id: str | None, pinned: dict[str, str]) -> str | None:
    for key, lid in pinned.items():
        if lid == tasklist_id:
            return key
    return None


async def _resolve_list_id(creds: "Credentials", key: str) -> str:
    if key not in LIST_TITLES:
        raise ApiError(400, "bad_list", "list must be 'mine' or 'follow'.")
    try:
        refs = await tasks_client.get_tasklist_refs(creds)
    except Exception as exc:
        raise ApiError(
            502, "google_tasks_unavailable", "Could not fetch Google Tasks lists."
        ) from exc
    list_id = _pinned_ids(refs).get(key)
    if list_id is None:
        raise ApiError(
            422,
            "pinned_list_missing",
            f"No Google task list named '{LIST_TITLES[key]}'.",
        )
    return list_id


# ── Reads ──────────────────────────────────────────────────────────────────────


def _get_thread(session: Session, user_id: int, thread_id: int) -> Thread:
    thread = session.get(Thread, thread_id)
    if thread is None or thread.user_id != user_id:
        raise ApiError(404, "thread_not_found", "Thread not found.")
    return thread


def _steps(session: Session, user_id: int, thread_id: int) -> list[ThreadStep]:
    return list(
        session.exec(
            select(ThreadStep)
            .where(ThreadStep.user_id == user_id, ThreadStep.thread_id == thread_id)
            .order_by(ThreadStep.position)
        ).all()
    )


def _get_step(
    session: Session, user_id: int, thread_id: int, step_id: int
) -> ThreadStep:
    step = session.get(ThreadStep, step_id)
    if step is None or step.user_id != user_id or step.thread_id != thread_id:
        raise ApiError(404, "step_not_found", "Step not found.")
    return step


def _open_steps(steps: list[ThreadStep]) -> list[ThreadStep]:
    return [s for s in steps if s.kind == "next"]


def _display_order(steps: list[ThreadStep]) -> list[ThreadStep]:
    """Done steps by rank, then the open block by due ascending (undated last),
    ties by rank (creation order)."""
    done = sorted((s for s in steps if s.kind != "next"), key=lambda s: s.position)
    opened = sorted(
        _open_steps(steps),
        key=lambda s: (s.due is None, s.due or date.max, s.position),
    )
    return done + opened


def _done_slot(step: ThreadStep, siblings: list[ThreadStep]) -> float:
    """Rank for a step joining the END of the done history: after the last done
    step, before the first remaining open step."""
    first_open = min((s.position for s in _open_steps(siblings)), default=None)
    done = [s.position for s in siblings if s.kind != "next"]
    if first_open is None:
        last = max(done, default=None)
        return step.position if last is None or last < step.position else last + _GAP
    lower = [p for p in done if p < first_open]
    return (max(lower) + first_open) / 2 if lower else first_open - _GAP


def serialize_step(step: ThreadStep) -> dict:
    is_next = step.kind == "next"
    return {
        "id": step.id,
        "kind": step.kind,
        "label": step.label,
        "note": step.note or "",
        "occurred_on": step.occurred_on.isoformat() if step.occurred_on else None,
        "list": step.via if is_next else None,
        "due": step.due.isoformat() if step.due else None,
        "tasklist_id": step.tasklist_id,
        "task_id": step.task_id,
        "via": None if is_next else step.via,
    }


def serialize_thread(thread: Thread, steps: list[ThreadStep]) -> dict:
    done_dates = [s.occurred_on for s in steps if s.kind == "done" and s.occurred_on]
    created = thread.created_at
    if created.tzinfo is None:  # SQLite hands naive datetimes back
        created = created.replace(tzinfo=timezone.utc)
    last_moved = max(done_dates) if done_dates else created.astimezone(_IST).date()
    return {
        "id": thread.id,
        "title": thread.title,
        "archived": thread.archived_at is not None,
        "created_at": created.isoformat(),
        "last_moved_on": last_moved.isoformat(),
        "steps": [serialize_step(s) for s in _display_order(steps)],
    }


def thread_payload(session: Session, user_id: int, thread: Thread) -> dict:
    return serialize_thread(thread, _steps(session, user_id, thread.id))


# ── Reconcile (Google → threads) ─────────────────────────────────────────────


def _refresh_cache(step: ThreadStep, task: dict, list_id: str, key: str | None) -> bool:
    """Copy Google's title / notes / due / list onto a live next step."""
    fresh = {
        "label": task.get("title") or step.label,
        "note": task.get("notes") or "",
        "due": _ist_date(task.get("due")),
        "tasklist_id": list_id,
        "via": key or step.via,
    }
    changed = any(getattr(step, k) != v for k, v in fresh.items())
    for k, v in fresh.items():
        setattr(step, k, v)
    return changed


def _flip_done(
    step: ThreadStep,
    task: dict,
    occurred_on: date | None,
    siblings: list[ThreadStep],
) -> None:
    """An open step whose task completed becomes done: dated, with a final snapshot,
    and re-ranked to the end of the done history (before any remaining open step)."""
    step.position = _done_slot(step, siblings)
    step.kind = "done"
    step.occurred_on = occurred_on or today_ist()
    step.label = task.get("title") or step.label
    step.note = task.get("notes") or ""
    step.due = None
    step.updated_at = _now()


def reconcile(
    session: Session, user_id: int, steps: list[ThreadStep], raw_lists: list[dict]
) -> list[ThreadStep]:
    """Apply Google's state to every linked step (see module docstring). Returns the
    surviving steps in position order; commits once if anything changed."""
    pinned = _pinned_ids(raw_lists)
    index: dict[str, tuple[str, dict]] = {}
    for tl in raw_lists:
        for task in tl.get("tasks", []):
            index[task["id"]] = (tl["id"], task)

    changed = False
    survivors: list[ThreadStep] = []
    completed: list[tuple[ThreadStep, dict]] = []
    for step in steps:
        if step.kind == "next" and step.task_id:
            hit = index.get(step.task_id)
            if hit is None:
                # Deleted in Google (or moved outside the dashboard — a v0 limit).
                session.delete(step)
                changed = True
                continue
            list_id, task = hit
            if task.get("status") == "completed":
                if step.via is None:
                    step.via = _list_key(list_id, pinned)
                completed.append((step, task))
            elif _refresh_cache(step, task, list_id, _list_key(list_id, pinned)):
                step.updated_at = _now()
                changed = True
        survivors.append(step)

    by_thread: dict[int, list[ThreadStep]] = {}
    for step in survivors:
        by_thread.setdefault(step.thread_id, []).append(step)

    # Completions join the done history in the order they were completed.
    completed.sort(key=lambda c: c[1].get("completed") or "")
    for step, task in completed:
        siblings = [s for s in by_thread[step.thread_id] if s is not step]
        _flip_done(step, task, _ist_date(task.get("completed")), siblings)
        changed = True

    # Undo after a reconcile: a done step whose task is open again rejoins the open
    # block at its end — unless another open step links that task (one link wins).
    live_links = {s.task_id for s in survivors if s.kind == "next" and s.task_id}
    for step in survivors:
        if step.kind != "done" or not step.task_id or step.task_id in live_links:
            continue
        hit = index.get(step.task_id)
        if hit is None or hit[1].get("status") != "needsAction":
            continue
        list_id, task = hit
        step.kind = "next"
        step.occurred_on = None
        step.position = max(s.position for s in by_thread[step.thread_id]) + _GAP
        _refresh_cache(step, task, list_id, _list_key(list_id, pinned))
        step.updated_at = _now()
        live_links.add(step.task_id)
        changed = True

    survivors.sort(key=lambda s: s.position)
    if changed:
        session.commit()
    return survivors


async def list_threads(
    session: Session, creds: "Credentials", user_id: int
) -> list[dict]:
    """All of the user's threads (active + archived), reconciled against Google.

    A failed Google fetch degrades to the cached step state (logged) rather than
    failing the panel — the next poll reconciles.
    """
    threads = session.exec(
        select(Thread).where(Thread.user_id == user_id).order_by(Thread.id)
    ).all()
    steps = list(
        session.exec(
            select(ThreadStep)
            .where(ThreadStep.user_id == user_id)
            .order_by(ThreadStep.position)
        ).all()
    )
    if any(s.task_id for s in steps):
        try:
            raw_lists = await tasks_client.get_task_lists(creds)
        except Exception:
            _log.exception(
                "threads reconcile: Google Tasks fetch failed; serving cache"
            )
        else:
            steps = reconcile(session, user_id, steps, raw_lists)

    by_thread: dict[int, list[ThreadStep]] = {}
    for step in steps:
        by_thread.setdefault(step.thread_id, []).append(step)
    return [serialize_thread(t, by_thread.get(t.id, [])) for t in threads]


# ── Thread CRUD ────────────────────────────────────────────────────────────────


def _clean(text: str, what: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ApiError(400, f"empty_{what}", f"{what.capitalize()} must not be empty.")
    return cleaned


def create_thread(session: Session, user_id: int, title: str) -> dict:
    thread = Thread(user_id=user_id, title=_clean(title, "title"))
    session.add(thread)
    session.commit()
    session.refresh(thread)
    return serialize_thread(thread, [])


def update_thread(
    session: Session,
    user_id: int,
    thread_id: int,
    title: Any = _UNSET,
    archived: Any = _UNSET,
) -> dict:
    """Rename / archive / restore. Archiving never touches the linked Google task."""
    thread = _get_thread(session, user_id, thread_id)
    if title is not _UNSET and title is not None:
        thread.title = _clean(title, "title")
    if archived is not _UNSET and archived is not None:
        if archived and thread.archived_at is None:
            thread.archived_at = _now()
        elif not archived:
            thread.archived_at = None
    thread.updated_at = _now()
    session.commit()
    session.refresh(thread)
    return thread_payload(session, user_id, thread)


def _touch(session: Session, thread_id: int) -> None:
    thread = session.get(Thread, thread_id)
    if thread is not None:
        thread.updated_at = _now()


# ── Steps ──────────────────────────────────────────────────────────────────────


def add_step(
    session: Session,
    user_id: int,
    thread_id: int,
    label: str,
    note: str | None = None,
    occurred_on: date | None = None,
) -> dict:
    """Log a done step. It lands BEFORE the first open step, if there is one
    (midpoint rank), so the open block stays last; else after everything."""
    thread = _get_thread(session, user_id, thread_id)
    steps = _steps(session, user_id, thread_id)
    first_open = min((s.position for s in _open_steps(steps)), default=None)
    if first_open is not None:
        before = [s.position for s in steps if s.position < first_open]
        position = (max(before) + first_open) / 2 if before else first_open - _GAP
    else:
        position = (steps[-1].position + _GAP) if steps else _GAP
    session.add(
        ThreadStep(
            thread_id=thread_id,
            user_id=user_id,
            position=position,
            kind="done",
            label=_clean(label, "label"),
            note=note or "",
            occurred_on=occurred_on or today_ist(),
        )
    )
    _touch(session, thread_id)
    session.commit()
    return thread_payload(session, user_id, thread)


async def set_next(
    session: Session,
    creds: "Credentials",
    user_id: int,
    thread_id: int,
    label: str,
    list_key: str,
    due: date | None,
    note: str | None = None,
) -> dict:
    """Create the Google task in the pinned list, THEN append a linked open step to
    the open block (a thread may hold any number since goal 14a).

    A `create_task` failure writes no row; a DB failure after a successful create
    is logged (orphan task, accepted).
    """
    thread = _get_thread(session, user_id, thread_id)
    steps = _steps(session, user_id, thread_id)
    title = _clean(label, "label")
    list_id = await _resolve_list_id(creds, list_key)

    created = await writes_svc.create_task(
        session,
        creds,
        user_id,
        tasklist_id=list_id,
        title=title,
        rank=None,
        notes=note or None,
        due_date=due.isoformat() if due else None,
    )
    try:
        session.add(
            ThreadStep(
                thread_id=thread_id,
                user_id=user_id,
                position=(steps[-1].position + _GAP) if steps else _GAP,
                kind="next",
                label=created.get("title") or title,
                note=created.get("notes") or "",
                tasklist_id=list_id,
                task_id=created["id"],
                due=_ist_date(created.get("due")) or due,
                via=list_key,
            )
        )
        _touch(session, thread_id)
        session.commit()
    except Exception as exc:
        session.rollback()
        _log.exception(
            "threads: created Google task %s but could not link it to thread %s",
            created.get("id"),
            thread_id,
        )
        raise ApiError(
            500,
            "thread_link_failed",
            "The task was created in Google Tasks but could not be linked to the "
            "thread.",
        ) from exc
    return thread_payload(session, user_id, thread)


async def update_step(
    session: Session,
    creds: "Credentials",
    user_id: int,
    thread_id: int,
    step_id: int,
    label: Any = _UNSET,
    note: Any = _UNSET,
    occurred_on: Any = _UNSET,
    due: Any = _UNSET,
    list_key: Any = _UNSET,
) -> dict:
    """Done step: a local edit. Next step: label/note → `update_content`, due →
    `reschedule`, list → `move` (which also carries a due change on its insert leg
    and repoints the link). Unchanged fields are skipped; each successful Google
    write is committed before the next, so a later failure leaves the cache true."""
    thread = _get_thread(session, user_id, thread_id)
    step = _get_step(session, user_id, thread_id, step_id)

    if step.kind == "done":
        if label is not _UNSET and label is not None:
            step.label = _clean(label, "label")
        if note is not _UNSET and note is not None:
            step.note = note
        if occurred_on is not _UNSET and occurred_on is not None:
            step.occurred_on = occurred_on
        step.updated_at = _now()
        session.commit()
        return thread_payload(session, user_id, thread)

    content: dict[str, Any] = {}
    if label is not _UNSET and label is not None:
        cleaned = _clean(label, "label")
        if cleaned != step.label:
            content["title"] = cleaned
    if note is not _UNSET and note is not None and note != (step.note or ""):
        content["notes"] = note
    due_changed = due is not _UNSET and due != step.due
    list_changed = (
        list_key is not _UNSET and list_key is not None and list_key != step.via
    )

    if content:
        updated = await writes_svc.update_content(
            session,
            creds,
            user_id,
            tasklist_id=step.tasklist_id,
            task_id=step.task_id,
            **content,
        )
        step.label = updated.get("title") or step.label
        step.note = updated.get("notes") or ""
        step.updated_at = _now()
        session.commit()

    if list_changed:
        target_id = await _resolve_list_id(creds, list_key)
        old_list, old_id = step.tasklist_id, step.task_id
        res = await writes_svc.move(
            session,
            creds,
            user_id,
            tasklist_id=old_list,
            task_id=old_id,
            target_list_id=target_id,
            rank=None,
            due_date=(due.isoformat() if due else None)
            if due_changed
            else writes_svc._UNSET,
        )
        repoint_link(session, user_id, old_list, old_id, target_id, res["new_task_id"])
        session.refresh(step)
        step.via = list_key
        if due_changed:
            step.due = due
        step.updated_at = _now()
        session.commit()
    elif due_changed:
        await writes_svc.reschedule(
            session,
            creds,
            user_id,
            tasklist_id=step.tasklist_id,
            task_id=step.task_id,
            due_date=due.isoformat() if due else None,
            rank=None,
            group_id=None,
        )
        step.due = due
        step.updated_at = _now()
        session.commit()

    return thread_payload(session, user_id, thread)


async def complete_step(
    session: Session,
    creds: "Credentials",
    user_id: int,
    thread_id: int,
    step_id: int,
) -> dict:
    """Mark an open step done: complete its Google task, then flip the step (its
    siblings stay open)."""
    thread = _get_thread(session, user_id, thread_id)
    step = _get_step(session, user_id, thread_id, step_id)
    if step.kind != "next":
        raise ApiError(400, "not_next", "Only an open step can be marked done.")
    updated = await writes_svc.update_content(
        session,
        creds,
        user_id,
        tasklist_id=step.tasklist_id,
        task_id=step.task_id,
        status="completed",
    )
    siblings = [s for s in _steps(session, user_id, thread_id) if s.id != step.id]
    _flip_done(step, updated, today_ist(), siblings)
    _touch(session, thread_id)
    session.commit()
    return thread_payload(session, user_id, thread)


def delete_step(session: Session, user_id: int, thread_id: int, step_id: int) -> dict:
    """Done step: delete it. Next step: UNLINK only — the row goes, the Google task
    stays in its list (threads never deletes a Google task)."""
    thread = _get_thread(session, user_id, thread_id)
    step = _get_step(session, user_id, thread_id, step_id)
    unlinked = step.kind == "next"
    session.delete(step)
    _touch(session, thread_id)
    session.commit()
    return {**thread_payload(session, user_id, thread), "unlinked": unlinked}


def repoint_link(
    session: Session,
    user_id: int,
    old_list: str,
    old_id: str,
    new_list: str,
    new_id: str,
) -> int:
    """A dashboard move re-mints the task id (insert-then-delete), so follow it: any
    step linked to the old id now points at the new one. Called by the tasks `move`
    endpoint and by `update_step`'s list switch. Returns the number of rows moved.
    The next reconcile refreshes the step's `via` from the new list."""
    rows = session.exec(
        select(ThreadStep).where(
            ThreadStep.user_id == user_id,
            ThreadStep.tasklist_id == old_list,
            ThreadStep.task_id == old_id,
        )
    ).all()
    for row in rows:
        row.tasklist_id = new_list
        row.task_id = new_id
        row.updated_at = _now()
    if rows:
        session.commit()
    return len(rows)
