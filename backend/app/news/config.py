"""News configuration + the code-shipped feed catalog (goal 11).

Everything tunable about the news pipeline lives here. The feed catalog is a static,
hand-curated map in the repo — the authenticity mechanism is "the owner chose the
sources", so it is code-reviewed, never fetched or LLM-generated. Editing a user's
feed list from the UI is goal 12; v1 uses this default set (or a per-user override
stored in `news_profile.feeds_json`).
"""

from __future__ import annotations

import os

# The curator + profile-rewriter run on the same small/cheap model as the router —
# this is selection + light prose, not reasoning.
NEWS_MODEL = os.environ.get("NEWS_MODEL", "claude-haiku-4-5")

# Output-budget cap for the LLM calls (structured output is small; prose rewrite fits).
NEWS_MAX_TOKENS = int(os.environ.get("NEWS_MAX_TOKENS", "2048"))

# The feed's own synopsis is length-capped at ingest (HTML stripped) so a full-text
# feed that dumps the whole article into description/content:encoded can never smuggle
# a body through the "synopsis" field. The hard line the news-LLM contract draws.
SYNOPSIS_MAX_CHARS = int(os.environ.get("NEWS_SYNOPSIS_MAX_CHARS", "500"))

# How many items the curator is asked to pick, and how many code-random serendipity
# slots are added from the non-picked pool. ~15 total keeps the daily list short.
CURATION_PICKS = int(os.environ.get("NEWS_CURATION_PICKS", "12"))
SERENDIPITY_SLOTS = int(os.environ.get("NEWS_SERENDIPITY_SLOTS", "3"))

# Serendipity is interleaved into the picks every Nth slot (frontend shows a ✨ badge)
# so the off-profile items can't be skipped as a block.
SERENDIPITY_EVERY = int(os.environ.get("NEWS_SERENDIPITY_EVERY", "4"))

# Ceiling on how many freshly-ingested candidates the curator is shown per run — keeps
# the prompt bounded when many feeds are healthy. Newest-first truncation.
MAX_CANDIDATES = int(os.environ.get("NEWS_MAX_CANDIDATES", "120"))

# Hacker News: how many top-story ids to consider (Firebase) — the front page is ~30.
HN_TOP_N = int(os.environ.get("NEWS_HN_TOP_N", "30"))

# The Guardian Open Platform developer key (free). Absent → the Guardian source is
# skipped (logged), never a crash. Sections requested are science + technology.
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")
GUARDIAN_SECTIONS = ("science", "technology")

# Per-feed fetch timeout (seconds). One slow/dead feed is logged and skipped.
FETCH_TIMEOUT = float(os.environ.get("NEWS_FETCH_TIMEOUT", "12"))

# ── Scheduler cadence (in-process asyncio loop, same pattern as the router) ────
# Disable with NEWS_SCHEDULER_ENABLED=0. The loop wakes on this interval and runs the
# daily / weekly jobs when their bookmarks say they are due (see scheduler.py).
SCHEDULER_ENABLED = os.environ.get("NEWS_SCHEDULER_ENABLED", "1") not in (
    "0",
    "false",
    "",
)
SCHEDULER_INTERVAL = float(os.environ.get("NEWS_SCHEDULER_INTERVAL", "1800"))
# The daily run fires when it is at/after this IST hour AND ≥ DAILY_MIN_HOURS since the
# last run. The first run (no bookmark) fires immediately regardless of the hour.
DAILY_HOUR_IST = int(os.environ.get("NEWS_DAILY_HOUR_IST", "7"))
DAILY_MIN_HOURS = float(os.environ.get("NEWS_DAILY_MIN_HOURS", "20"))
WEEKLY_MIN_HOURS = float(os.environ.get("NEWS_WEEKLY_MIN_HOURS", "168"))


# ── The code-shipped default feed catalog ─────────────────────────────────────
# name → RSS feed URL. Hand-curated; the source allowlist IS the authenticity
# guarantee. Google News RSS *search* feeds are best-effort extras (no SLA). Extend
# in code, reviewed at PR time — goal 12 adds a per-user chip editor over this catalog.
DEFAULT_FEEDS: dict[str, str] = {
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "MIT Technology Review": "https://www.technologyreview.com/feed/",
    "Quanta Magazine": "https://www.quantamagazine.org/feed/",
    "Nature News": "https://www.nature.com/nature.rss",
    "IEEE Spectrum": "https://spectrum.ieee.org/feeds/feed.rss",
    "arXiv cs.AI": "https://rss.arxiv.org/rss/cs.AI",
    "arXiv cs.LG": "https://rss.arxiv.org/rss/cs.LG",
    "Anthropic": "https://www.anthropic.com/news/rss.xml",
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    # Google News RSS search feeds (best-effort, no SLA) for topical breadth.
    "Google News · AI": (
        "https://news.google.com/rss/search?q=artificial+intelligence"
        "+when:1d&hl=en-US&gl=US&ceid=US:en"
    ),
}


def default_feed_urls() -> list[str]:
    return list(DEFAULT_FEEDS.values())
