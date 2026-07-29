---
paths: ["backend/app/news/**", "backend/app/routers/news.py", "frontend/src/news/**"]
---

# News pipeline safety (goal 11)

The News feature adds two runtime LLMs (the **curator** and the **profile rewriter**) to
a system that previously had exactly one (the router classifier). Read this before
editing anything under `backend/app/news/`. The whole pipeline is **non-Google** and
follows the same LLM-proposes / code-disposes ethos as the router.

## The hard contract (what the news LLMs may see)

- **Title + the feed's OWN synopsis — never a fetched article body.** The curator input
  is exactly `[{id, title, synopsis, source, published_at}]` + the profile doc
  (`curator.build_candidate_payload` / `build_prompt`). `synopsis` is the short summary
  the feed already ships (RSS `description`/`summary`, Guardian `trailText`), captured at
  ingest **HTML-stripped and length-capped** (`ingest.capture_synopsis`,
  `config.SYNOPSIS_MAX_CHARS ≈ 500`) so a full-text feed that dumps the whole article
  into `description`/`content:encoded` can never smuggle a body through the synopsis
  field. HN has no synopsis (empty).
- **No article-page HTTP fetch, ever.** Ingestion fetches only feed/section endpoints
  (`ingest.fetch_rss`/`fetch_hn`/`fetch_guardian`) — it never requests an item's own URL
  for its text. `item.url` is stored and rendered as an outbound link only. If you ever
  want richer per-item summaries, fetch + summarize only the ~15 winners — a future
  decision, out of scope here.
- **No Google data in any news prompt.** The news module imports **no** `app.google`
  client (`test_news.py::test_news_module_imports_no_google_client` pins this). The news
  path takes no Google creds anywhere. The AST write-dependency test is unchanged because
  news adds **zero** Google API calls.

## LLM-proposes / code-disposes

- **The curator only selects.** `curator.py` imports the Anthropic SDK, never `app.news.
  service` writes. It returns a `CurationResult` (ids + why + domain) and nothing else. A
  model error/refusal returns an empty result and the service falls back to a code-only
  recency selection — the feed is never empty because the model hiccupped.
- **Code validates every returned id** against the candidate set (`service._dispose_
  curation`): an id not in the input is dropped (never invented), the count is capped
  (`config.CURATION_PICKS`), and URLs/titles/anything rendered come from the DB rows the
  code fetched — never from LLM output. Same trust boundary as the router's path→id
  resolution.
- **Serendipity is code-random, not an LLM pick.** After the LLM selects, code
  `random.sample`s `config.SERENDIPITY_SLOTS` items from the **non-picked** pool and flags
  them `is_serendipity` — the anti-filter-bubble escape hatch can never itself be captured
  by the profile. Seeded-RNG unit-tested.
- **The profile rewriter** (`profile.rewrite`, weekly) sees only item **titles** + votes +
  comments (public feed metadata) and the current profile. A failed/empty rewrite keeps
  the current profile; the one previous version is retained for a one-click revert.

## Layering

- `ingest.py` — pure fetch + parse + dedupe (canonical URL + fuzzy title). Returns
  `RawItem`s; no DB. The fetchers (`_fetch_text`/`_fetch_json`) are module-level so tests
  monkeypatch them. One dead feed is logged + skipped (`_safe`), never fatal.
- `curator.py` / `profile.py` — the runtime LLMs (structured output / prose). No DB, no
  writes. Prompt builders are pure + unit-tested for the exact field set + synopsis cap.
- `service.py` — deterministic dispose: upserts candidates, validates ids, code-random
  serendipity, interleaves + scores, persists the feed; runs the weekly rewrite; serves
  the view; upserts feedback. Every query is `user_id`-scoped (goal 8).
- `scheduler.py` — in-process asyncio loop (same pattern as the router scheduler), daily
  curation + weekly rewrite gated by per-user bookmarks. Loads **no** Google creds. The
  `/news/fetch-now` endpoint calls `service.run_daily` directly.

## Access gating

News is a **per-user feature flag stored on the `allowed_email` row** (a JSON `features`
column, e.g. `{"news": true}`), toggled by the superuser in the admin UI (Settings →
Allowed emails → News checkbox). **Any superuser always has News on** and needs no row.
The flag mechanism is generic: add a `(key, label)` to `auth.service.FEATURES` and the
admin UI renders a checkbox for it automatically — no other change. `gating.is_news_
enabled(session, user)` delegates to `auth.service.is_feature_enabled(session, user,
"news")`; `routers/news.py::require_news_enabled` 403s every `/news` endpoint for a
non-enabled user; `/auth/me` reports `news_enabled` so the frontend hides the rail entry.
The earlier `NEWS_ENABLED_EMAILS` env var is **gone** — never reintroduce it. **Goal 12
generalises this per-user feature-flag surface.**

## Feed catalog

`config.DEFAULT_FEEDS` is a **code-shipped, hand-curated** name→URL map — the source
allowlist IS the authenticity guarantee, so it is reviewed at PR time, never fetched or
LLM-generated. A per-user override lives in `news_profile.feeds_json`; a UI chip editor
over the catalog is goal 12.
