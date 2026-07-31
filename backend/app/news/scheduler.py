"""In-process news scheduler (goal 11) — same pattern as the router scheduler.

A single asyncio task started from the FastAPI lifespan wakes on an interval and runs
the daily curation + weekly profile rewrite for each news-enabled user when their
bookmarks say they are due. No Celery/broker (single-user local app). The manual
`fetch-now` endpoint calls `service.run_daily` directly, so dev never waits on the
timer. Disable with `NEWS_SCHEDULER_ENABLED=0`.

The news pipeline is non-Google, so — unlike the router scheduler — this loads NO
Google credentials; it only needs the user rows and the news-enabled allowlist.
"""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.auth.models import User
from app.db import engine
from app.news import config, gating
from app.news import service as news_svc

_log = logging.getLogger("news.scheduler")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_task: asyncio.Task | None = None


def _hours_since(when: datetime | None) -> float | None:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def _daily_due(last_at: datetime | None) -> bool:
    """First run (no bookmark) is always due; otherwise at/after the IST hour AND at
    least DAILY_MIN_HOURS since the last run."""
    elapsed = _hours_since(last_at)
    if elapsed is None:
        return True
    if elapsed < config.DAILY_MIN_HOURS:
        return False
    return datetime.now(_IST).hour >= config.DAILY_HOUR_IST


def _weekly_due(last_at: datetime | None) -> bool:
    elapsed = _hours_since(last_at)
    return elapsed is None or elapsed >= config.WEEKLY_MIN_HOURS


async def _tick_all_users() -> None:
    with Session(engine) as session:
        users = session.exec(select(User)).all()
        for user in users:
            if not gating.is_news_enabled(session, user):
                continue
            prof = news_svc.get_or_create_profile(session, user.id)
            try:
                if _daily_due(prof.last_daily_at):
                    tally = await news_svc.run_daily(session, user.id)
                    _log.info("news daily (user %s): %s", user.id, tally)
                if _weekly_due(prof.last_weekly_at):
                    tally = await news_svc.run_weekly(session, user.id)
                    _log.info("news weekly (user %s): %s", user.id, tally)
            except Exception:
                _log.exception("news scheduler: run failed for user %s", user.id)


async def _loop() -> None:
    # Let the app finish booting before the first (possibly heavy) tick, so news ingest
    # never competes with startup/healthcheck. The router scheduler sleeps a full
    # interval first; news keeps the first run timely with a short grace delay instead.
    await asyncio.sleep(config.SCHEDULER_STARTUP_DELAY)
    while True:
        try:
            await _tick_all_users()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a transient failure kill the loop
            _log.exception("news scheduler tick failed")
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
