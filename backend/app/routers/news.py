"""News HTTP surface (goal 11).

The curated daily feed, feedback, the profile doc, and a manual fetch-now. Every
endpoint is gated by `require_news_enabled` (403 for non-enabled users, so the whole
resource is invisible to them) and scoped to `current_user`. No Google creds are taken
anywhere here — the news pipeline is entirely non-Google.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from app.auth.deps import get_current_user
from app.auth.models import User
from app.db import get_session
from app.errors import ApiError
from app.news import gating
from app.news import service as news_svc
from app.news.models import NewsItem, NewsProfile

router = APIRouter(prefix="/news", tags=["news"])


def require_news_enabled(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> User:
    """Gate every news endpoint behind the per-user News flag (goal 11). A non-enabled
    user gets 403 on every /news endpoint and no rail entry (the frontend reads
    `news_enabled` from /auth/me)."""
    if not gating.is_news_enabled(session, user):
        raise ApiError(403, "news_not_enabled", "News is not enabled for your account.")
    return user


def _item_out(item: NewsItem, fb) -> dict:
    return {
        "id": item.id,
        "source": item.source,
        "feed": item.feed,
        "title": item.title,
        "url": item.url,
        "synopsis": item.synopsis,
        "domain": item.domain,
        "why_line": item.why_line,
        "is_serendipity": item.is_serendipity,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "run_date": item.run_date,
        "vote": fb.vote if fb else 0,
        "comment": fb.comment if fb else None,
    }


@router.get("")
async def get_feed(
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """The curated feed: SELECTED items, newest run first, with the user's feedback
    merged in. The frontend groups by `run_date`."""
    items = news_svc.feed_items(session, user.id)
    fbmap = news_svc.feedback_map(session, user.id, [i.id for i in items])
    prof = news_svc.get_or_create_profile(session, user.id)
    return {
        "items": [_item_out(i, fbmap.get(i.id)) for i in items],
        "last_run_at": news_svc.last_run_at(prof),
    }


@router.post("/fetch-now")
async def fetch_now(
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """Run today's ingest + curation now (the manual trigger; same code path as the
    scheduled daily job). Dev/owner affordance, mirrors the router's route-now."""
    tally = await news_svc.run_daily(session, user.id)
    return {"tally": tally}


class FeedbackRequest(BaseModel):
    vote: int = 0  # +1 / -1 / 0
    comment: Optional[str] = None


@router.post("/{item_id}/feedback")
async def post_feedback(
    item_id: int,
    body: FeedbackRequest,
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """Upsert a 👍/👎 + optional comment for one item (the weekly rewrite's input)."""
    row = news_svc.set_feedback(session, user.id, item_id, body.vote, body.comment)
    return {"item_id": row.item_id, "vote": row.vote, "comment": row.comment}


def _profile_out(row: NewsProfile) -> dict:
    return {
        "profile": row.profile_md,
        "has_prev": bool((row.profile_prev_md or "").strip()),
        "last_daily_at": row.last_daily_at.isoformat() if row.last_daily_at else None,
        "last_weekly_at": (
            row.last_weekly_at.isoformat() if row.last_weekly_at else None
        ),
    }


@router.get("/profile")
async def get_profile(
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """The user's markdown profile doc (what the curator reads) + revert availability."""
    return _profile_out(news_svc.get_or_create_profile(session, user.id))


class ProfileUpdate(BaseModel):
    profile: str


@router.put("/profile")
async def put_profile(
    body: ProfileUpdate,
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """Save a hand-edited profile (retaining the current one as the revert slot)."""
    return _profile_out(news_svc.set_profile(session, user.id, body.profile))


@router.post("/profile/revert")
async def revert_profile(
    user: User = Depends(require_news_enabled),
    session: Session = Depends(get_session),
):
    """Restore the retained previous profile version (one-slot history)."""
    return _profile_out(news_svc.revert_profile(session, user.id))
