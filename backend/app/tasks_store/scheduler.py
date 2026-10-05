"""Import sweep (goal 17): imports every not-yet-imported user shortly after boot,
then re-checks hourly so a transient failure (a 429, a token hiccup) heals without
the user having to log in. Once everyone is imported each tick is one cheap query.
Disable with `TASKS_IMPORT_SWEEP_ENABLED=0`.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlmodel import Session, select

from app.auth.models import User
from app.db import engine
from app.google import auth as google_auth
from app.tasks_store import importer

_log = logging.getLogger("tasks_store.scheduler")

_ENABLED = os.environ.get("TASKS_IMPORT_SWEEP_ENABLED", "1") not in ("0", "false", "")
_STARTUP_DELAY = float(os.environ.get("TASKS_IMPORT_STARTUP_DELAY", "10"))
_INTERVAL = float(os.environ.get("TASKS_IMPORT_SWEEP_INTERVAL", "3600"))

_task: asyncio.Task | None = None


async def sweep() -> dict:
    """Import each pending user with THEIR credentials; one failure never blocks the
    rest. Returns a tally."""
    tally = {"imported": 0, "failed": 0, "skipped": 0}
    with Session(engine) as session:
        pending = session.exec(
            select(User).where(User.tasks_imported_at.is_(None))  # type: ignore[union-attr]
        ).all()
        for user in pending:
            if not user.refresh_token_encrypted:
                tally["skipped"] += 1
                continue
            try:
                creds = google_auth.load_credentials(session, user)
                await importer.ensure_imported(session, creds, user.id)
                tally["imported"] += 1
            except Exception:
                _log.exception("tasks import sweep: user %s failed", user.id)
                tally["failed"] += 1
    if any(tally.values()):
        _log.info("tasks import sweep: %s", tally)
    return tally


async def _loop() -> None:
    await asyncio.sleep(_STARTUP_DELAY)
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a transient failure kill the loop
            _log.exception("tasks import sweep tick failed")
        await asyncio.sleep(_INTERVAL)


def start() -> None:
    global _task
    if not _ENABLED or _task is not None:
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
