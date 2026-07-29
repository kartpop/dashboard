"""News persistence (goal 11).

Three per-user tables backing the curated daily feed:

- `news_item` — one ingested article (RSS / Hacker News / Guardian), deduped, with
  the capped feed-provided synopsis and the curator's why-line + domain label. The
  news pipeline is **entirely non-Google**: nothing here touches a Google client.
- `news_feedback` — one 👍/👎 (+ optional comment) per (user, item); upserted.
- `news_profile` — the per-user markdown profile doc the curator reads and the
  weekly job rewrites (one previous version retained for a one-click revert), plus
  the per-user feed list and the daily/weekly run bookmarks the scheduler reads.

Row-scoping (goal 8): every table carries `user_id`; every query filters by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel

# ── news_item.status ──────────────────────────────────────────────────────────
# A freshly ingested candidate the curator has not yet ruled on.
CANDIDATE = "candidate"
# Chosen for the feed — either an LLM pick or a code-random serendipity slot.
SELECTED = "selected"
# Ingested + deduped but not chosen this run. Kept (not deleted) so the same URL is
# not re-ingested and re-shown on a later run (dedupe memory).
DROPPED = "dropped"

# ── news_item.source ──────────────────────────────────────────────────────────
SOURCE_RSS = "rss"
SOURCE_HN = "hn"
SOURCE_GUARDIAN = "guardian"

# ── news_item.domain (the curator's coarse topic label) ───────────────────────
DOMAINS = ("frontier-models", "science", "technology", "other")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NewsItem(SQLModel, table=True):
    __tablename__ = "news_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # row-scoping (goal 8)
    source: str = Field(max_length=20, index=True)  # rss | hn | guardian
    feed: str = Field(default="", max_length=200)  # human feed name / origin
    title: str = Field()
    url: str = Field()
    # Canonicalized URL used for dedupe (tracking params stripped, scheme/host lowered).
    canonical_url: str = Field(default="", index=True)
    # The feed's OWN short summary — RSS description/summary, Guardian trailText — HTML
    # stripped and length-capped at ingest so a full-text feed cannot smuggle an article
    # body through this field. HN carries none (empty). The ONLY text beyond the title the
    # curator ever sees; the pipeline never fetches an article page for its body.
    synopsis: str = Field(default="")
    # The curator's coarse topic label (frontier-models | science | technology | other).
    domain: Optional[str] = Field(default=None, max_length=20)
    published_at: Optional[datetime] = Field(default=None)
    ingested_at: datetime = Field(default_factory=_utcnow, nullable=False)
    # The IST calendar day of the run that ingested this item — the frontend groups
    # the feed under `Today · …` / `Yesterday` headers by this.
    run_date: str = Field(default="", max_length=10, index=True)
    # Display order within a run (curator picks + interleaved serendipity), ascending.
    score: Optional[float] = Field(default=None)
    why_line: Optional[str] = Field(
        default=None
    )  # the LLM's one-line "why this matters"
    # A code-random off-profile pick (the anti-filter-bubble slot), NOT an LLM choice.
    is_serendipity: bool = Field(default=False)
    status: str = Field(default=CANDIDATE, max_length=20, index=True)


class NewsFeedback(SQLModel, table=True):
    __tablename__ = "news_feedback"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # row-scoping (goal 8)
    item_id: int = Field(foreign_key="news_item.id", index=True)
    # +1 = 👍, -1 = 👎, 0 = cleared. Comments carry far more signal than the thumbs
    # and are first-class input to the weekly profile rewrite.
    vote: int = Field(default=0)
    comment: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)


class NewsProfile(SQLModel, table=True):
    __tablename__ = "news_profile"

    # One row per user; the user id doubles as the primary key.
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    # The human-readable markdown profile the curator reads. Hand-editable; the weekly
    # job rewrites it from accumulated feedback. Empty → the curator falls back to a
    # sensible default interest set (see profile.effective_profile).
    profile_md: str = Field(default="")
    # The one previous version retained, so a bad rewrite is one manual revert away.
    profile_prev_md: str = Field(default="")
    # JSON list of RSS feed URLs. Empty ("[]") → the code-shipped default set (feeds.py).
    # Editing this from the UI is goal 12; v1 edits go through the DB/seed.
    feeds_json: str = Field(default="[]")
    # Scheduler bookmarks: when the daily / weekly job last completed for this user.
    last_daily_at: Optional[datetime] = Field(default=None)
    last_weekly_at: Optional[datetime] = Field(default=None)
    updated_at: datetime = Field(default_factory=_utcnow, nullable=False)
