"""`tasks_ready` — the gate every task-store endpoint passes through (goal 17).

An imported user costs nothing here (no credential load, no Google call). A user
not yet imported is imported inline, once; if that fails the request answers
`503 tasks_import_pending` and the panel offers Retry (the hourly sweep also retries).
"""

from __future__ import annotations

import logging

from fastapi import Depends
from sqlmodel import Session

from app.auth.deps import get_current_credentials, get_current_user
from app.auth.models import User
from app.db import get_session
from app.errors import ApiError
from app.tasks_store import importer

_log = logging.getLogger("tasks_store.deps")


async def ensure_ready(session: Session, user: User, creds=None) -> None:
    """Import `user` now if needed; raise 503 `tasks_import_pending` on failure.
    `creds` is loaded lazily, only when an import actually has to run."""
    if user.tasks_imported_at is not None:
        return
    try:
        if creds is None:
            creds = get_current_credentials(user, session)
        await importer.ensure_imported(session, creds, user.id)
    except ApiError:
        raise
    except Exception as exc:
        _log.exception("tasks import failed for user %s", user.id)
        raise ApiError(
            503,
            "tasks_import_pending",
            "Your tasks are still being imported. Try again in a moment.",
        ) from exc


async def tasks_ready(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    await ensure_ready(session, user)
    return user
