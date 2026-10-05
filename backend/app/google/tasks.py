"""Read-only Google Tasks access for the one-time import (goal 17).

Since goal 17 the dashboard's own DB is the source of truth for tasks; this module
is read ONLY by `app.tasks_store.importer` (AST-pinned in the tests). It is deleted
in goal 17b once every user has been imported. Read paths call the Google API
client directly (CLAUDE.md hard constraint) — never MCP or an LLM.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from googleapiclient.discovery import build

from app.google._paging import list_all

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


def _tasks_service(creds: "Credentials"):
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def _reshape_for_import(task: dict) -> dict:
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "status": task.get("status", "needsAction"),
        "due": task.get("due"),
        "notes": task.get("notes"),
        "completed": task.get("completed"),
        # Google's manual order within the list (a zero-padded sortable string).
        "position": task.get("position") or "",
    }


def _fetch_for_import(creds: "Credentials") -> list[dict]:
    service = _tasks_service(creds)
    task_lists = []
    for task_list in list_all(service.tasklists(), maxResults=100):
        tasks = list_all(
            service.tasks(),
            tasklist=task_list["id"],
            showCompleted=True,
            showHidden=True,
            maxResults=100,
        )
        task_lists.append(
            {
                "id": task_list["id"],
                "title": task_list.get("title", ""),
                "tasks": [_reshape_for_import(t) for t in tasks],
            }
        )
    return task_lists


async def fetch_for_import(creds: "Credentials") -> list[dict]:
    """Every list with every task (open + completed, incl. hidden) — one pass,
    100 per page. The importer filters completed tasks by age."""
    return await asyncio.to_thread(_fetch_for_import, creds)
