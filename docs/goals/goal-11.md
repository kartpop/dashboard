# Goal 11 — News: curated daily feed + feedback-driven profile

**One line:** A new top-level **News view** (first second view in the app — a collapsed left
nav rail ships with it) fed by a **$0 ingest layer** — curated RSS feeds + the Hacker News
API + the Guardian API — on a **daily cron**; an LLM filters on **title + the feed's own
synopsis (never a fetched article body)**, against a per-user **news profile doc** and
surfaces ~15 items with a one-line
"why this matters to you", of which **~3 slots are reserved for off-profile serendipity
picks chosen by code, not the LLM**; each card takes **👍/👎 + an optional comment**, and a
**weekly cron rewrites the profile doc** from the accumulated feedback.

## Intent / acceptance bar

Every morning the News tab has a fresh, short, honest list: ~15 items spanning science,
technology, and frontier-model developments, drawn from sources I chose (so authenticity is
by construction, not by trust in an aggregator), each with one line on why it was picked.
Three or so of them are deliberately *not* what my profile predicts — the anti-filter-bubble
slots — and are visibly badged as such. I thumb items up or down, occasionally with a
comment ("too incremental — I only care about capability jumps"), and within a week the
picks visibly reflect that feedback because the profile doc — a human-readable markdown blob
I can also edit by hand — was rewritten from it. Decisions use the title **plus the synopsis the
feed already ships** (RSS `description`/`summary`, Guardian `trailText`) — because a headline
alone is often catchy-but-hollow and the publisher's own standfirst reads truer — but the
pipeline **never fetches an article body**; it only forwards metadata the feed handed us for
free. The whole thing costs ~$0 in API fees and pennies in LLM tokens. Nothing about the
existing safety posture moves: the news LLM steps see only public feed metadata (title +
synopsis) and the profile doc — **never Google data** — item URLs/ids are never taken from LLM output (the LLM
selects from ids the code gave it), the router write set is untouched, and no Google scope
changes.

## What ships

- **1. Ingest layer (deterministic code, daily cron).** A `news` backend module with an
  in-process asyncio scheduler (same pattern as the goal-5 router scheduler — **not**
  APScheduler; no new dependency, as-built) that runs once daily (IST morning) plus a
  manual `fetch-now` endpoint for testing:
  - **Curated RSS feeds** (`feedparser`): a per-user feed list stored in `news_profile`
    (a dedicated per-user table rather than `user_settings`, as-built — keeps all news
    persistence in the news module),
    seeded with a code-shipped default set (e.g. Ars Technica, The Verge, MIT Tech Review,
    Quanta, Nature News, IEEE Spectrum, arXiv cs.AI/cs.LG, the Anthropic/OpenAI/DeepMind
    blogs, Google News RSS *search* feeds as best-effort extras). Editing the list from the
    UI is **goal 12**; v1 edits go through the DB/seed.
  - **Hacker News** via the official Firebase/Algolia API (front-page + top stories — free,
    no key).
  - **The Guardian Open Platform** (free developer key, `GUARDIAN_API_KEY` env): 1–2
    section calls (science, technology); request `show-fields=trailText` for the standfirst.
    Headlines + feed metadata only — no article body.
  - **Synopsis capture (feed-provided, capped).** For each item, keep the short summary the
    feed already carries — RSS `description`/`summary`, Guardian `trailText`; HN has none
    (left empty). **Length-capped** (~500 chars, HTML stripped) at ingest so a full-text
    feed (some put the whole article in `content:encoded`/`description`) can never smuggle a
    body through this field. This is the only text beyond the title the curator ever sees.
  - **Dedupe** by canonical URL + fuzzy title match across sources; upsert into a
    `news_item` table (Alembic migration): `id, user_id, source, feed, title, url,
    synopsis, domain, published_at, ingested_at, score, why_line, is_serendipity, status`
    (`synopsis` = the capped feed summary; `domain` = the curator's topic label, §2). Fetch
    failures per feed are logged and skipped — one dead feed never kills the run.
  - **NewsData.io is explicitly deferred** — an optional later add for broader mainstream
    category sweep (200 req/day free tier); not in this goal.
- **2. Curation step (the news LLM — title + synopsis, no fetched body).** One batched call
  per user per day (`claude-haiku-4-5`, structured output): input is the deduped candidate
  list as `[{id, title, synopsis, source, published_at}]` — `synopsis` being the capped
  feed-provided summary from §1, empty when the source gives none — plus the profile doc.
  The synopsis is what lets the LLM see past a clickbait headline to what the piece is
  actually about. Output per selected id is `{id, why, domain}`: ~12 ids, a one-line why,
  and a coarse `domain` label (`frontier-models` | `science` | `technology` | `other`)
  used for optional grouping in §4. **LLM-proposes / code-disposes, same ethos as the
  router:** code validates every returned id against the candidate set (an id not in the
  input is dropped), caps the count, and stores score/why/domain on the rows. URLs, titles,
  and anything rendered come from the DB rows the code fetched — never from LLM output.
  - **Serendipity slots are code, not LLM:** after the LLM picks, code random-samples ~3
    items from the *non-picked* remainder of the pool and flags them `is_serendipity`.
    Chosen by code so the escape hatch from the profile can never itself be captured by the
    profile.
- **3. Feedback + weekly profile rewrite.** A `news_feedback` table (`item_id, user_id,
  vote, comment, created_at`); `POST /news/{item_id}/feedback` upserts a vote + optional
  comment. A **weekly** scheduler job feeds the current profile doc + the week's votes and
  comments to the LLM and stores the rewritten profile; the **previous version is kept**
  (single-slot history) so a bad rewrite is one manual revert away. The profile doc is
  markdown in `user_settings` — human-readable and hand-editable; a minimal raw-text editor
  for it lives *inside the News view* for now (a small "Profile" drawer/section — the full
  settings treatment with braindump + LLM-recreate is **goal 12**).
- **4. Frontend: the nav rail + the News view.** The app gains its first second view:
  - A **collapsed left nav rail**: Home (today's dashboard, unchanged) and News; **login /
    settings anchor at the bottom-left** of the rail. The existing settings page is reached
    from there but is otherwise **untouched** — the settings-modal restructure is goal 12.
  - **News view = a vertical list of cards, grouped by run-day.** A date header spines each
    run (`Today · Mon 28 Jul`, then `Yesterday`, …); newest run first. A view header shows
    the last-run time + a manual **Fetch now** (dev/owner affordance, same spirit as
    Route now).
  - **Card anatomy** (repeated down the list, built to *skim*):
    - **Headline is the hero and the link** — it opens the **original article at the source,
      new tab** (the card is a launcher, not a reader; there is no in-app article view).
    - Source + relative published time on the top line; the LLM **why-line** as muted
      secondary text (the stored `synopsis` is available to the card but the why-line stays
      the primary sub-text — showing both is a later density call, not v1).
    - Feedback sits quiet on the bottom row: 👍/👎 toggles + a **collapsed 💬 comment**
      (click to expand a small box; persist on blur/submit, optimistic) — so the resting
      view is a clean skim, not a stack of open textareas.
  - **Serendipity is interleaved, not siloed** — the ~3 off-profile items sit inline in the
    day's list, each wearing a **✨ badge**, so they can't be skipped as a block. That
    honesty toward the anti-filter-bubble intent is the whole reason they're code-picked.
  - **Domain tag stored, not yet grouped.** The §2 `domain` label rides on each card
    (available for a small caption/pill) but v1 stays a single flat chronological list —
    topic sub-headers / filter chips are a deliberate later polish, not built here (a ~15-
    item list doesn't need sectioning yet).
- **5. Access gating.** *(As-built — revised from the original `NEWS_ENABLED_EMAILS` env
  sketch.)* News is a **per-user feature flag stored on the `allowed_email` row** (JSON
  `features` column, e.g. `{"news": true}`), toggled by the superuser in the admin UI
  (Settings → Allowed emails → News checkbox). **Any superuser always has News on.** The
  mechanism is generic (`auth.service.FEATURES` registry → the UI renders a checkbox per
  entry), so goal 12 extends rather than replaces it. There is **no env var** for news
  access. `gating.is_news_enabled(session, user)` → `auth.service.is_feature_enabled`;
  `require_news_enabled` 403s non-enabled users; `/auth/me` reports `news_enabled`.
- **6. Guardrail artifacts in lockstep.** The AST write-dependency test is unchanged in
  surface and re-asserted (news adds **zero** Google API calls — it is entirely
  non-Google); a new `news.md` rule (or a `router.md` section) records the news-LLM
  contract: input is title + capped feed synopsis only (**no fetched article body — the
  pipeline makes no HTTP request to an article page**), no Google data in any news prompt,
  ids validated against the candidate set, serendipity is code-random. Unit tests pin the
  prompt-builder (asserts only `{id, title, synopsis, source, published_at}` + profile are
  serialized, and the synopsis is length-capped) and the id-validation dispose step.

## Draft decisions (2026-07-28)

*Drafted from the brainstorm session; overturn in planning if wrong.*

- **RSS + HN + Guardian only; no paid/quota'd aggregator.** SerpAPI/Google News scraping
  rejected (250/mo free ceiling, inherits Google's personalization); NewsData.io deferred
  as an optional breadth add. Source allowlist = the authenticity mechanism.
- **The LLM sees title + feed synopsis; it never sees a fetched body — a hard contract.**
  The synopsis the feed already ships is fair game (and improves decisions — headlines lie,
  standfirsts less so); *fetching* an article page and sending its text is the forbidden
  line. Cost is the minor reason; the real one is keeping the prompt surface tiny,
  auditable, and free of any per-article HTTP fetch. The synopsis is length-capped at
  ingest so a full-text feed can't turn "synopsis" into "body". If richer per-item
  summaries are ever wanted, fetch + summarize only the ~15 *winners* — a future decision.
- **Serendipity is code-random from the non-picked pool** (not "LLM, pick 3 off-profile
  items") — an LLM anti-pick is still profile-shaped; uniform random is not.
- **Profile doc is one markdown blob per user in `user_settings`,** rewritten weekly with
  one previous version retained. No embeddings, no vector store, no per-topic weights —
  the LLM reads prose. Comments are first-class input to the rewrite (they carry far more
  signal than the thumbs).
- **Ingestion is per-user but v1 is effectively single-user** (owner-only via the env
  gate), so the pipeline runs once in practice; no shared-pool optimization until a second
  user actually enables news.
- **The news LLM steps are a sanctioned expansion of the runtime-LLM surface** (previously
  router-only). The README spanning constraint is amended in the same commit: runtime LLMs
  are now exactly {router classifier, news curator, news profile-rewriter}, and the news
  two see no Google data, ever.
- **New view = nav rail now, settings restructure later.** The rail ships here because News
  needs somewhere to live; everything else about the shell (settings modal + its side nav,
  per-user flags UI, news settings panel) is goal 12.

## Out of scope (do not build)

- The settings modal, its internal side-nav, and any settings restructure (goal 12).
- Braindump → LLM-recreate profile flow, and the news-domain chip picker UI (goal 12).
- Fine-grained per-user feature flags in DB + owner admin UI (goal 12; env var here).
- NewsData.io (or any keyed aggregator beyond Guardian), SerpAPI, Tavily/Exa
  "dig deeper" actions.
- **Fetching article bodies** — any HTTP request to an article page for its full text
  (the feed-provided synopsis is *not* this and is in scope); summarizing fetched bodies;
  full-text search; embeddings; read-later/archive; push/email digests.
- An **in-app article reader** — cards link out to the source; the app never renders
  article bodies.
- Topic sub-headers / domain filter chips (the `domain` tag is stored but the list stays
  flat in v1).
- Any Google API involvement in the news path (no Docs export, no Gmail digest — nothing).
- Feed-list editing UI (DB/seed only for now).

## Acceptance criteria

- **Ingest:** the daily job (and `fetch-now`) pulls all three source types, dedupes across
  them (unit-tested on URL + fuzzy-title fixtures), and survives a dead feed (fixture feed
  404s → run completes, others ingested). New Alembic migration applies cleanly.
- **Curation:** the LLM payload contains exactly `{id, title, synopsis, source,
  published_at}` per candidate + the profile doc — the synopsis is present when the feed
  supplied one and is **length-capped**, and **no full article body / fetched-content field
  is ever serialized** (unit tests on the prompt builder: field-set assertion + a
  full-text-feed fixture whose serialized synopsis is truncated); ingestion makes **no HTTP
  request to any article URL** (unit test: no per-item body fetch); returned ids are
  validated against the candidate set (unit test: alien id dropped) and each carries a valid
  `domain` label; ~15 items stored per run with why-lines; ~3 flagged `is_serendipity`,
  chosen from the non-picked pool (unit test with seeded RNG).
- **Feedback loop:** votes + comments persist and re-render after reload; the weekly
  rewrite job produces a new profile doc that survives restart, retains the prior version,
  and demonstrably incorporates a fixture comment (eval-style spot check, not a gate).
- **Frontend:** nav rail shows Home | News with login/settings bottom-left; News renders a
  day-grouped card list — each card's **headline links to the source (new tab)**, with
  source, relative time, why-line, ✨ serendipity badge on the interleaved off-profile
  items, and a working 👍/👎 + collapsible comment; non-enabled users see no News entry and
  `/news` endpoints 403 them.
- **Posture:** AST test green (zero new Google API methods — the news module imports no
  Google client); router write set, scopes, and all existing eval gates untouched; `tsc`,
  frontend build, backend tests green.
- `goal-11-owner-steps.md` exists (Guardian key registration, env vars, seed feed-list
  review).

## Harness upkeep (closing checklist — friction-driven only)

- New `news.md` rule (or `router.md` section): the news-LLM contract (title + capped feed
  synopsis, **no fetched body / no article-page HTTP**, no Google data, id-validation
  dispose, serendipity-is-code).
- README ladder + spanning-constraints amendment (runtime-LLM set) — same commit as this
  brief; verify at close.
- `verifier-web`: News-view checks (rail nav, cards render, feedback persists,
  gated-user 403).
- `goal-11-owner-steps.md` kept current as steps are discovered.
- Record rule fire/no-fire (`/context`); wrap-up to the planning chat.
