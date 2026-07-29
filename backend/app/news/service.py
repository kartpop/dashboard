"""News orchestration — deterministic dispose around the curator LLM (goal 11).

This is the news counterpart to `router.service`: the LLM (`curator`) *proposes* ids,
this module *disposes* — it validates every returned id against the candidate set,
caps the count, code-random-samples the serendipity slots from the NON-picked pool,
interleaves them, and persists the feed. It also runs the weekly profile rewrite and
serves the feed to the view.

The whole path is **non-Google**: this module imports no Google client and passes no
Google data to any LLM. Every query is scoped by `user_id` (goal 8).
"""

from __future__ import annotations

import json
import logging
import random
import zoneinfo
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.errors import ApiError
from app.news import config, curator, ingest, profile
from app.news.models import (
    CANDIDATE,
    DROPPED,
    SELECTED,
    NewsFeedback,
    NewsItem,
    NewsProfile,
)
from app.news.schema import CurationResult

_log = logging.getLogger("news.service")

_IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_ist() -> str:
    return datetime.now(_IST).date().isoformat()


# ── Profile / feed-list accessors ─────────────────────────────────────────────


def get_or_create_profile(session: Session, user_id: int) -> NewsProfile:
    row = session.get(NewsProfile, user_id)
    if row is None:
        row = NewsProfile(user_id=user_id)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def feed_urls(row: NewsProfile) -> list[str]:
    """The user's RSS feed list — their override, or the code default set. (Editing it
    from the UI is goal 12; v1 uses this.)"""
    try:
        urls = json.loads(row.feeds_json or "[]")
    except json.JSONDecodeError:
        urls = []
    urls = [str(u).strip() for u in urls if isinstance(u, str) and u.strip()]
    return urls or config.default_feed_urls()


def set_profile(session: Session, user_id: int, profile_md: str) -> NewsProfile:
    """Persist a hand-edited profile, retaining the current one as the revert slot."""
    row = get_or_create_profile(session, user_id)
    row.profile_prev_md = row.profile_md
    row.profile_md = profile_md
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def revert_profile(session: Session, user_id: int) -> NewsProfile:
    """Swap the profile back to its retained previous version (one-slot history)."""
    row = get_or_create_profile(session, user_id)
    row.profile_md, row.profile_prev_md = row.profile_prev_md, row.profile_md
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


# ── Daily ingest + curation run ───────────────────────────────────────────────


def _upsert_candidates(
    session: Session, user_id: int, raw_items: list[ingest.RawItem], run_date: str
) -> list[NewsItem]:
    """Insert genuinely-new items as CANDIDATEs; skip any URL already ingested for this
    user (cross-run dedupe memory — a story shown/dropped before is not re-ingested).
    Returns the new candidates, newest-first, capped at MAX_CANDIDATES."""
    new_items: list[NewsItem] = []
    for raw in raw_items:
        if raw.canonical_url:
            exists = session.exec(
                select(NewsItem)
                .where(NewsItem.user_id == user_id)
                .where(NewsItem.canonical_url == raw.canonical_url)
                .limit(1)
            ).first()
            if exists is not None:
                continue
        item = NewsItem(
            user_id=user_id,
            source=raw.source,
            feed=raw.feed,
            title=raw.title,
            url=raw.url,
            canonical_url=raw.canonical_url,
            synopsis=raw.synopsis,
            published_at=raw.published_at,
            run_date=run_date,
            status=CANDIDATE,
        )
        session.add(item)
        new_items.append(item)
    session.commit()
    for item in new_items:
        session.refresh(item)

    new_items.sort(
        key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return new_items[: config.MAX_CANDIDATES]


def _dispose_curation(
    candidates: list[NewsItem],
    result: CurationResult,
    rng: random.Random,
) -> None:
    """Code-disposes the LLM's proposal, mutating the candidate rows in place.

    - Validate every returned id against the candidate set (an alien id is dropped).
    - Cap at CURATION_PICKS; keep the LLM's best-first order.
    - Fallback: if the LLM returned nothing usable, pick the newest CURATION_PICKS
      candidates by recency so the feed is never empty on a model hiccup.
    - Serendipity: code-random-sample SERENDIPITY_SLOTS from the NON-picked pool and
      flag them — chosen by code so the off-profile escape hatch is never itself
      profile-shaped.
    - Interleave serendipity into the picks (every SERENDIPITY_EVERY) and assign the
      display `score` (ascending). Everything not selected → DROPPED (dedupe memory).
    """
    by_id = {str(item.id): item for item in candidates}

    picked: list[NewsItem] = []
    seen: set[str] = set()
    for pick in result.picks:
        item = by_id.get(str(pick.id))
        if item is None or str(item.id) in seen:
            continue  # alien / duplicate id — dropped
        item.why_line = (pick.why or "").strip() or None
        item.domain = pick.domain
        picked.append(item)
        seen.add(str(item.id))
        if len(picked) >= config.CURATION_PICKS:
            break

    if not picked:
        # Recency fallback — candidates are already newest-first.
        for item in candidates[: config.CURATION_PICKS]:
            item.why_line = None
            item.domain = item.domain or "other"
            picked.append(item)
            seen.add(str(item.id))

    non_picked = [item for item in candidates if str(item.id) not in seen]
    k = min(config.SERENDIPITY_SLOTS, len(non_picked))
    serendipity = rng.sample(non_picked, k) if k else []
    serendipity_ids = {str(i.id) for i in serendipity}
    for item in serendipity:
        item.is_serendipity = True
        item.why_line = None  # off-profile by design — no profile-grounded why

    # Interleave: a serendipity item after every SERENDIPITY_EVERY picks, rest appended.
    ordered: list[NewsItem] = []
    ser = list(serendipity)
    for idx, item in enumerate(picked):
        ordered.append(item)
        if ser and (idx + 1) % config.SERENDIPITY_EVERY == 0:
            ordered.append(ser.pop(0))
    ordered.extend(ser)

    for score, item in enumerate(ordered):
        item.status = SELECTED
        item.score = float(score)

    selected_ids = {str(i.id) for i in ordered} | serendipity_ids
    for item in candidates:
        if str(item.id) not in selected_ids:
            item.status = DROPPED


async def run_daily(
    session: Session,
    user_id: int,
    rng: random.Random | None = None,
) -> dict:
    """Ingest all sources, curate with the LLM, dispose, and persist the feed for
    `user_id`. Returns a tally. A dead feed is skipped inside `ingest.fetch_all`; an
    LLM failure degrades to the recency fallback in `_dispose_curation`."""
    rng = rng or random.Random()
    prof = get_or_create_profile(session, user_id)
    run_date = _today_ist()

    raw_items = await ingest.fetch_all(feed_urls(prof))
    candidates = _upsert_candidates(session, user_id, raw_items, run_date)

    if candidates:
        result = await curator.curate(
            candidates, profile.effective_profile(prof.profile_md)
        )
        _dispose_curation(candidates, result, rng)
        session.add_all(candidates)

    prof.last_daily_at = _now()
    session.add(prof)
    session.commit()

    selected = [c for c in candidates if c.status == SELECTED]
    return {
        "run_date": run_date,
        "fetched": len(raw_items),
        "new_candidates": len(candidates),
        "selected": len(selected),
        "serendipity": sum(1 for c in selected if c.is_serendipity),
    }


# ── Weekly profile rewrite ────────────────────────────────────────────────────


async def run_weekly(session: Session, user_id: int) -> dict:
    """Rewrite the profile doc from the week's feedback (comments weighted heaviest),
    retaining the prior version. A failed/empty rewrite keeps the current profile."""
    prof = get_or_create_profile(session, user_id)
    since = _now() - timedelta(days=7)
    rows = session.exec(
        select(NewsFeedback)
        .where(NewsFeedback.user_id == user_id)
        .where(NewsFeedback.updated_at >= since)
    ).all()

    records: list[profile.FeedbackRecord] = []
    for fb in rows:
        if fb.vote == 0 and not (fb.comment or "").strip():
            continue
        item = session.get(NewsItem, fb.item_id)
        if item is None:
            continue
        records.append(
            profile.FeedbackRecord(title=item.title, vote=fb.vote, comment=fb.comment)
        )

    new_profile = await profile.rewrite(prof.profile_md, records)
    changed = False
    if new_profile:
        prof.profile_prev_md = prof.profile_md
        prof.profile_md = new_profile
        changed = True
    prof.last_weekly_at = _now()
    prof.updated_at = _now()
    session.add(prof)
    session.commit()
    return {"rewritten": changed, "feedback_count": len(records)}


# ── Feed view + feedback ──────────────────────────────────────────────────────


def feed_items(session: Session, user_id: int, limit_days: int = 7) -> list[NewsItem]:
    """The SELECTED items to render, newest run first then display order (score)."""
    cutoff = (datetime.now(_IST).date() - timedelta(days=limit_days)).isoformat()
    return session.exec(
        select(NewsItem)
        .where(NewsItem.user_id == user_id)
        .where(NewsItem.status == SELECTED)
        .where(NewsItem.run_date >= cutoff)
        .order_by(NewsItem.run_date.desc(), NewsItem.score.asc())
    ).all()


def feedback_map(session: Session, user_id: int, item_ids: list[int]) -> dict:
    if not item_ids:
        return {}
    rows = session.exec(
        select(NewsFeedback)
        .where(NewsFeedback.user_id == user_id)
        .where(NewsFeedback.item_id.in_(item_ids))
    ).all()
    return {r.item_id: r for r in rows}


def set_feedback(
    session: Session,
    user_id: int,
    item_id: int,
    vote: int,
    comment: str | None,
) -> NewsFeedback:
    """Upsert the (user, item) vote + optional comment. Item must belong to the user
    (no cross-tenant feedback by id)."""

    item = session.get(NewsItem, item_id)
    if item is None or item.user_id != user_id:
        raise ApiError(404, "news_item_not_found", "No news item with that id.")

    row = session.exec(
        select(NewsFeedback)
        .where(NewsFeedback.user_id == user_id)
        .where(NewsFeedback.item_id == item_id)
    ).first()
    if row is None:
        row = NewsFeedback(user_id=user_id, item_id=item_id)
    row.vote = max(-1, min(1, int(vote)))
    row.comment = (comment or "").strip() or None
    row.updated_at = _now()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def last_run_at(row: NewsProfile) -> str | None:
    return row.last_daily_at.isoformat() if row.last_daily_at else None
