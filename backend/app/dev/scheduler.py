"""In-process dev scheduler (goal 12) — same pattern as the news/router schedulers.

A single asyncio task started from the FastAPI lifespan wakes on an interval and runs
the daily notes → issue-draft scan for each **dev-enabled** user when their bookmark
says they are due. No Celery/broker (single-user local app). The manual `scan-now`
endpoint calls `service.run_scan` directly, so dev never waits on the timer. Disable
with `DEV_SCHEDULER_ENABLED=0`.

**Cost gating (the whole point of the flag).** The per-user loop `continue`s past any
user failing `gating.is_dev_enabled` **before** loading Google creds, reading a Doc, or
spending an opus token — exactly as `news.scheduler` gates on `is_news_enabled`. It also
skips users whose config is incomplete (no token / no source / no repo), so the opus
call only ever fires for a flagged, configured user.
"""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.auth.models import User
from app.db import engine
from app.dev import config, gating
from app.dev import service as dev_svc
from app.google import auth as google_auth

_log = logging.getLogger("dev.scheduler")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_task: asyncio.Task | None = None


def _hours_since(when: datetime | None) -> float | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def _daily_due(last_at: datetime | None) -> bool:
    """First run (no bookmark) is due once past the IST hour; otherwise at/after the IST
    hour AND at least DAILY_MIN_HOURS since the last run."""
    elapsed = _hours_since(last_at)
    if elapsed is not None and elapsed < config.DAILY_MIN_HOURS:
        return False
    return datetime.now(_IST).hour >= config.DAILY_HOUR_IST


async def _tick_all_users() -> None:
    with Session(engine) as session:
        users = session.exec(select(User)).all()
        for user in users:
            # Cost gate: skip unflagged users BEFORE any creds/Doc/LLM work (mirrors
            # news.scheduler's is_news_enabled continue).
            if not gating.is_dev_enabled(session, user):
                continue
            cfg = dev_svc.get_or_create_config(session, user.id)
            if not dev_svc.is_config_complete(session, cfg):
                continue
            if not _daily_due(cfg.last_scan_at):
                continue
            try:
                creds = google_auth.load_credentials(session, user)
            except Exception:
                _log.exception("dev scheduler: creds load failed for user %s", user.id)
                continue
            try:
                tally = await dev_svc.run_scan(session, user, creds)
                _log.info("dev scan (user %s): %s", user.id, tally)
            except Exception:
                _log.exception("dev scheduler: scan failed for user %s", user.id)


async def _loop() -> None:
    while True:
        try:
            await _tick_all_users()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a transient failure kill the loop
            _log.exception("dev scheduler tick failed")
        await asyncio.sleep(config.SCHEDULER_INTERVAL)


def start() -> None:
    global _task
    if not config.SCHEDULER_ENABLED or _task is not None:
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
