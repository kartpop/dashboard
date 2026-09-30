"""Read-only access to the account's Google Tasks lists and tasks.

This is the only module that talks to the Google Tasks API directly (see
CLAUDE.md hard constraint: read paths call the Google API client directly,
never via MCP/LLM).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.google._paging import list_all

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

# Sentinel so write wrappers can tell "field omitted" from "explicitly cleared".
_UNSET: Any = object()


def _tasks_service(creds: "Credentials"):
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _reshape_task(task: dict) -> dict:
    """Map a raw Google Tasks resource to our small, stable shape."""
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "status": task.get("status", "needsAction"),
        "due": task.get("due"),
        "notes": task.get("notes"),
        # Subtasks render flat (goal 4a MVP); `parent` lets the client guard
        # against ever dropping or duplicating a child task.
        "parent": task.get("parent"),
        # RFC3339 completion timestamp (goal 14): threads reconcile dates a
        # completed next step by it. Absent for open tasks.
        "completed": task.get("completed"),
    }


def _fetch_task_lists(creds: "Credentials") -> list[dict]:
    service = _tasks_service(creds)

    task_lists = []
    for task_list in list_all(service.tasklists()):
        tasks = list_all(
            service.tasks(),
            tasklist=task_list["id"],
            showCompleted=True,
            showHidden=True,
        )
        task_lists.append(
            {
                "id": task_list["id"],
                "title": task_list.get("title", ""),
                "tasks": [_reshape_task(task) for task in tasks],
            }
        )
    return task_lists


async def get_task_lists(creds: "Credentials") -> list[dict]:
    """Return every task list in the account, each with its tasks."""
    return await asyncio.to_thread(_fetch_task_lists, creds)


def _fetch_tasklist_refs(creds: "Credentials") -> list[dict]:
    service = _tasks_service(creds)
    return [
        {"id": tl["id"], "title": tl.get("title", "")}
        for tl in list_all(service.tasklists())
    ]


async def get_tasklist_refs(creds: "Credentials") -> list[dict]:
    """Return every task list as `{id, title}` only — no tasks fetched (goal 14:
    threads resolves a pinned list id without paging every list's tasks)."""
    return await asyncio.to_thread(_fetch_tasklist_refs, creds)


# ── Read helper used by the writes service ────────────────────────────────────


def _get_task(creds: "Credentials", tasklist_id: str, task_id: str) -> dict | None:
    service = _tasks_service(creds)
    try:
        task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    except HttpError as exc:
        # An unknown / malformed task id is "not found" — Google returns 404 for
        # some ids and 400 for others, so treat both as None (a single-task GET
        # has no other meaning for a bad id). Lets callers raise a clean 404.
        if exc.resp.status in (400, 404):
            return None
        raise
    return _reshape_task(task)


async def get_task(creds: "Credentials", tasklist_id: str, task_id: str) -> dict | None:
    """Fetch a single task; return None if it does not exist (404)."""
    return await asyncio.to_thread(_get_task, creds, tasklist_id, task_id)


# ── Thin write wrappers (one Google call each; no orchestration) ──────────────


def _update_due_date(
    creds: "Credentials", tasklist_id: str, task_id: str, due: str | None
) -> None:
    service = _tasks_service(creds)
    if due is not None:
        service.tasks().patch(
            tasklist=tasklist_id, task=task_id, body={"due": due}
        ).execute()
        return
    # GOOGLE QUIRK: the Tasks API ignores a null `due` on patch, so clearing the
    # due date requires a full get-then-update (the one exception to "one API call").
    task = service.tasks().get(tasklist=tasklist_id, task=task_id).execute()
    task.pop("due", None)
    service.tasks().update(tasklist=tasklist_id, task=task_id, body=task).execute()


async def update_due_date(
    creds: "Credentials", tasklist_id: str, task_id: str, due: str | None
) -> None:
    """Set (patch) or clear (get+update) the Google due date for a task."""
    await asyncio.to_thread(_update_due_date, creds, tasklist_id, task_id, due)


def _insert_task(creds: "Credentials", tasklist_id: str, body: dict) -> dict:
    service = _tasks_service(creds)
    task = service.tasks().insert(tasklist=tasklist_id, body=body).execute()
    return _reshape_task(task)


async def insert_task(creds: "Credentials", tasklist_id: str, body: dict) -> dict:
    """Insert a new task into a list and return its reshaped representation."""
    return await asyncio.to_thread(_insert_task, creds, tasklist_id, body)


def _delete_task(creds: "Credentials", tasklist_id: str, task_id: str) -> None:
    service = _tasks_service(creds)
    service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()


async def delete_task(creds: "Credentials", tasklist_id: str, task_id: str) -> None:
    # Exactly two sanctioned callers (see .claude/rules/writes.md):
    #   1. app.writes.service.move — after a confirmed successful insert.
    #   2. app.writes.service.delete — the user delete endpoint, after the
    #      frontend's undo window has closed.
    await asyncio.to_thread(_delete_task, creds, tasklist_id, task_id)


def _update_task_content(
    creds: "Credentials",
    tasklist_id: str,
    task_id: str,
    title: Any = _UNSET,
    notes: Any = _UNSET,
    status: Any = _UNSET,
) -> dict:
    service = _tasks_service(creds)
    body: dict = {}
    if title is not _UNSET:
        body["title"] = title
    if notes is not _UNSET:
        # Google clears `notes` when patched with an empty string.
        body["notes"] = notes or ""
    if status is not _UNSET:
        body["status"] = status
        # GOOGLE QUIRK: patching status back to needsAction does NOT always clear
        # the stored `completed` timestamp; send completed=None to force it.
        if status == "needsAction":
            body["completed"] = None
    task = (
        service.tasks().patch(tasklist=tasklist_id, task=task_id, body=body).execute()
    )
    return _reshape_task(task)


async def update_task_content(
    creds: "Credentials",
    tasklist_id: str,
    task_id: str,
    title: Any = _UNSET,
    notes: Any = _UNSET,
    status: Any = _UNSET,
) -> dict:
    """Patch a task's content fields (title / notes / status). Only the fields
    explicitly provided are sent; completion rides the `status` field."""
    return await asyncio.to_thread(
        _update_task_content, creds, tasklist_id, task_id, title, notes, status
    )


# ── Tasklist (list-level) write — the only write to the tasklists resource ─────


def _update_tasklist(creds: "Credentials", tasklist_id: str, title: str) -> dict:
    service = _tasks_service(creds)
    tl = (
        service.tasklists().patch(tasklist=tasklist_id, body={"title": title}).execute()
    )
    return {"id": tl["id"], "title": tl.get("title", "")}


async def update_tasklist(creds: "Credentials", tasklist_id: str, title: str) -> dict:
    """Rename a task list (the one write to the tasklists resource, goal 4a)."""
    return await asyncio.to_thread(_update_tasklist, creds, tasklist_id, title)
