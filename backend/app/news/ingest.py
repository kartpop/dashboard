"""News ingest — deterministic, non-Google, no article-body fetch (goal 11).

Pulls three $0 source types and normalizes them into a common `RawItem`: curated
RSS feeds (`feedparser`), the Hacker News API (Algolia front page — free, no key),
and the Guardian Open Platform (free developer key). It fetches only **feed/section
endpoints** — never an individual article URL for its body — and captures only the
short synopsis the feed already ships (RSS description/summary, Guardian trailText),
HTML-stripped and length-capped so a full-text feed cannot smuggle a body through.

This module is pure fetch + parse + dedupe: it returns `RawItem`s and never touches
the database (the service upserts them). One dead feed is logged and skipped — it
never kills the run. The parsers (`parse_rss`, `parse_hn`, `parse_guardian`) take raw
payloads so they unit-test with fixtures and no network.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from app.news import config
from app.news.models import SOURCE_GUARDIAN, SOURCE_HN, SOURCE_RSS

_log = logging.getLogger("news.ingest")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")
# Tracking / campaign query params dropped when canonicalizing a URL for dedupe.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "cmpid",
    "fbclid",
    "gclid",
    "ref",
    "at_medium",
    "at_campaign",
    "guccounter",
}


@dataclass
class RawItem:
    """One normalized, pre-dedupe article. `synopsis` is already capped/stripped."""

    source: str
    feed: str
    title: str
    url: str
    synopsis: str = ""
    published_at: datetime | None = None
    canonical_url: str = field(default="")
    norm_title: str = field(default="")

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.url = (self.url or "").strip()
        self.synopsis = capture_synopsis(self.synopsis)
        self.canonical_url = canonical_url(self.url)
        self.norm_title = _norm_title(self.title)


# ── Normalization helpers (pure) ──────────────────────────────────────────────


def capture_synopsis(raw: str | None) -> str:
    """Strip HTML tags + collapse whitespace, then hard-cap at SYNOPSIS_MAX_CHARS.

    The cap is the load-bearing guard: some RSS feeds put the WHOLE article in
    `description`/`content:encoded`. Capping here means the "synopsis" field can never
    become an article body — the news-LLM contract (title + capped synopsis, no fetched
    body) holds by construction, not by trust in the feed."""
    if not raw:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()
    if len(text) <= config.SYNOPSIS_MAX_CHARS:
        return text
    return text[: config.SYNOPSIS_MAX_CHARS].rstrip() + "…"


def canonical_url(url: str) -> str:
    """Canonicalize a URL for dedupe: lowercase scheme/host, drop tracking params,
    strip a trailing slash + fragment. Best-effort; a malformed URL returns as-is."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.netloc:
        return url.strip()
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _norm_title(title: str) -> str:
    return _NONWORD_RE.sub(" ", (title or "").lower()).strip()


def _to_utc(dt_struct: Any) -> datetime | None:
    """feedparser `*_parsed` struct_time (UTC) → aware datetime, or None."""
    if not dt_struct:
        return None
    try:
        return datetime(*dt_struct[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Parsers (pure — take raw payloads, return RawItems) ───────────────────────


def parse_rss(content: str, feed_name: str) -> list[RawItem]:
    """Parse an RSS/Atom feed body into RawItems. `description`/`summary` is the
    feed-provided synopsis (capped in `RawItem.__post_init__`)."""
    parsed = feedparser.parse(content)
    out: list[RawItem] = []
    for e in parsed.entries:
        title = getattr(e, "title", "") or ""
        link = getattr(e, "link", "") or ""
        if not title or not link:
            continue
        synopsis = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        published = _to_utc(getattr(e, "published_parsed", None)) or _to_utc(
            getattr(e, "updated_parsed", None)
        )
        out.append(
            RawItem(
                source=SOURCE_RSS,
                feed=feed_name,
                title=title,
                url=link,
                synopsis=synopsis,
                published_at=published,
            )
        )
    return out


def parse_hn(hits: list[dict]) -> list[RawItem]:
    """Parse Algolia HN `search` hits. HN stories carry no synopsis (title + URL only);
    an Ask/Show HN with no external URL is skipped (nothing to link out to)."""
    out: list[RawItem] = []
    for h in hits:
        title = h.get("title") or ""
        url = h.get("url") or ""
        if not title or not url:
            continue
        out.append(
            RawItem(
                source=SOURCE_HN,
                feed="Hacker News",
                title=title,
                url=url,
                synopsis="",  # HN has no standfirst — title-only to the curator
                published_at=_parse_iso(h.get("created_at")),
            )
        )
    return out


def parse_guardian(payload: dict) -> list[RawItem]:
    """Parse a Guardian content-API response. `fields.trailText` is the standfirst —
    the feed-provided synopsis; requested via show-fields=trailText."""
    results = ((payload or {}).get("response") or {}).get("results") or []
    out: list[RawItem] = []
    for r in results:
        title = r.get("webTitle") or ""
        url = r.get("webUrl") or ""
        if not title or not url:
            continue
        trail = (r.get("fields") or {}).get("trailText") or ""
        out.append(
            RawItem(
                source=SOURCE_GUARDIAN,
                feed="The Guardian",
                title=title,
                url=url,
                synopsis=trail,
                published_at=_parse_iso(r.get("webPublicationDate")),
            )
        )
    return out


# ── Fetchers (network — module-level so tests monkeypatch them) ───────────────


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=config.FETCH_TIMEOUT, follow_redirects=True
    ) as client:
        resp = await client.get(url, headers={"User-Agent": "dashboard-news/1.0"})
        resp.raise_for_status()
        return resp.text


async def _fetch_json(url: str) -> Any:
    async with httpx.AsyncClient(
        timeout=config.FETCH_TIMEOUT, follow_redirects=True
    ) as client:
        resp = await client.get(url, headers={"User-Agent": "dashboard-news/1.0"})
        resp.raise_for_status()
        return resp.json()


async def fetch_rss(url: str, feed_name: str) -> list[RawItem]:
    return parse_rss(await _fetch_text(url), feed_name)


async def fetch_hn() -> list[RawItem]:
    url = f"https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={config.HN_TOP_N}"
    payload = await _fetch_json(url)
    return parse_hn((payload or {}).get("hits") or [])


async def fetch_guardian() -> list[RawItem]:
    if not config.GUARDIAN_API_KEY:
        return []
    out: list[RawItem] = []
    for section in config.GUARDIAN_SECTIONS:
        url = "https://content.guardianapis.com/search?" + urlencode(
            {
                "section": section,
                "show-fields": "trailText",
                "page-size": "20",
                "order-by": "newest",
                "api-key": config.GUARDIAN_API_KEY,
            }
        )
        out.extend(parse_guardian(await _fetch_json(url)))
    return out


# ── Orchestration ─────────────────────────────────────────────────────────────


async def _safe(label: str, coro) -> list[RawItem]:
    """Run one source fetch; a failure is logged and yields [] — one dead feed never
    kills the run (the acceptance-criteria 'survives a dead feed' guarantee)."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — per-source best-effort
        _log.warning("news ingest: source %s failed: %s", label, exc)
        return []


async def fetch_all(feed_urls: list[str]) -> list[RawItem]:
    """Fetch every configured source concurrently and return the deduped RawItems.

    `feed_urls` are RSS feed URLs (per-user list or the code default set); HN and the
    Guardian are added unconditionally (Guardian no-ops without a key). Each source is
    isolated by `_safe`, so a 404/timeout on one feed is skipped, not fatal."""
    tasks: list[Any] = [
        _safe(url, fetch_rss(url, _feed_name_for(url))) for url in feed_urls
    ]
    tasks.append(_safe("hacker-news", fetch_hn()))
    tasks.append(_safe("guardian", fetch_guardian()))
    results = await asyncio.gather(*tasks)
    items: list[RawItem] = [it for group in results for it in group]
    return dedupe(items)


def _feed_name_for(url: str) -> str:
    """Reverse-lookup a friendly feed name from the catalog; fall back to the host."""
    for name, feed_url in config.DEFAULT_FEEDS.items():
        if feed_url == url:
            return name
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def dedupe(items: list[RawItem], *, title_ratio: float = 0.9) -> list[RawItem]:
    """Drop cross-source duplicates: same canonical URL, or a near-identical title.

    Keeps the FIRST occurrence (source order: RSS feeds, then HN, then Guardian). A
    later item is a duplicate if its canonical URL was already seen OR its normalized
    title is ≥ `title_ratio` similar to a kept item's — the two ways the same story
    shows up under two sources."""
    kept: list[RawItem] = []
    seen_urls: set[str] = set()
    for it in items:
        if it.canonical_url and it.canonical_url in seen_urls:
            continue
        if it.norm_title and any(
            SequenceMatcher(None, it.norm_title, k.norm_title).ratio() >= title_ratio
            for k in kept
        ):
            continue
        kept.append(it)
        if it.canonical_url:
            seen_urls.add(it.canonical_url)
    return kept
