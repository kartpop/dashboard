# Goal 12 — Frontend shell: settings modal, per-user feature flags, news settings

**One line:** The app shell grows up around the goal-11 nav rail: **settings moves out of
its overlong page into a modal with its own internal side-nav** (Calendars · Notes &
hierarchy · Allowed emails · News); the **allowed-emails panel becomes richer DB-backed
per-user management with fine-grained feature flags** (chip-style email input, per-user
feature toggles — building on goal 11's `allowed_email.features` column + News checkbox,
which already replaced the `NEWS_ENABLED_EMAILS` env var); and a **News settings
panel** gives the profile doc a real home — view/edit, a **braindump box + "recreate
profile from braindump" LLM action**, and a **feed/domain chip picker** that snaps to a
curated catalog of known-good sources as you type.

## Intent / acceptance bar

The dashboard stops feeling like one page with a settings page bolted on. The rail (from
goal 11) is the app's spine; clicking settings opens a modal I can navigate — calendars,
notes hierarchy, allowed emails, news — each its own panel instead of one endless scroll.
As the owner I manage users the way the reference screenshot does: type emails into a chip
box, add them, and per user flip what they get — specifically whether the News feature is
on for them; flipping it off makes News vanish from their rail and 403s the endpoints, no
redeploy. In the News panel I see my profile doc as the LLM sees it, can edit it directly,
or paste a messy braindump and have the LLM regenerate the profile from it (showing me the
result before it saves — LLM-proposes, I dispose). Below that, my source list is chips:
typing "ars" snaps to Ars Technica's feed from the catalog; unknown-but-valid feed URLs can
still be added raw. Nothing in the backend news pipeline, router, write set, or Google
scope surface changes — this goal is shell, settings plumbing, and two small news-adjacent
endpoints.

## What ships

- **1. Settings modal with internal side-nav.** The existing settings page's content is
  decomposed into panels inside a modal (opened from the rail's bottom-left anchor):
  **Calendars** (the g8 toggle list), **Notes & hierarchy** (the g9 tree widget),
  **Allowed emails** (owner-only), **News** (visible if news is enabled for the user).
  Panels move — they are not rewritten; calendar/notes behavior is unchanged and their
  existing verifier checks still pass. The old full-page settings route dies.
- **2. Allowed emails → DB-backed users with feature flags (owner-only panel).**
  - A `user_flags` (or extension of the existing user/settings) table: per-email row with
    flags, starting with exactly one flag: `news_enabled`. `ALLOWED_EMAILS` env remains
    the sign-in allowlist source of truth **or** migrates into this table — decide in
    planning (draft decision below picks a direction).
  - UI per the reference screenshot: chip-style multi-email input (paste-friendly,
    validated), an add action, a user list with per-user flag toggles. Owner-only:
    non-owner users never see this panel and the endpoints 403 them.
  - **Feature flags already DB-backed (goal 11)**: `allowed_email.features` + the News
    checkbox + `auth.service.FEATURES` registry already replaced `NEWS_ENABLED_EMAILS`.
    Goal 12 only enriches the management UI (chip input, more features) over that column —
    no env var to remove.
- **3. News settings panel.**
  - **Profile doc**: rendered + editable (the goal-11 in-view drawer moves here and the
    drawer dies). Shows the retained previous version with a one-click revert.
  - **Braindump → recreate**: a free-text box; "Recreate profile" sends braindump + current
    profile to the LLM (same headlines-era contract: no Google data in the prompt) and
    shows the proposed new profile **for explicit accept/discard before saving** — the one
    settings write that involves an LLM, and it never writes directly.
  - **Feed/domain chips**: the per-user feed list (goal 11 seeded it in `user_settings`)
    gets a chip editor with typeahead against a **code-shipped catalog** of known-good
    sources (name → feed URL: the goal-11 default set plus a reasonable long tail).
    Typing snaps to catalog entries; a raw `https://…` feed URL can be added as a custom
    chip (validated by a fetch-and-parse probe before accept). Removing a chip stops
    future ingestion; already-ingested items stay.
- **4. Guardrail artifacts in lockstep.** `news.md` (from g11) gains the
  braindump-recreate contract (propose-then-accept, no direct save, no Google data);
  Alembic migration for flags; AST test unchanged (still zero Google methods in news;
  settings panels move, their Google call surface doesn't grow).

## Draft decisions (2026-07-28)

*Drafted from the brainstorm session; overturn in planning if wrong.*

- **Modal, not a routed settings view** — the owner asked for exactly this ("settings can
  open in a separate modal window for itself, which itself can have a side panel").
  Deep-linking to a settings tab is a non-goal.
- **Move, don't rewrite, the calendar + notes panels.** This goal's risk is regression in
  settled surfaces; the decomposition is mechanical extraction. Any behavior change found
  wanting goes to a future goal.
- **Allowlist direction:** migrate `ALLOWED_EMAILS` into the DB table and make the panel
  the source of truth (env var becomes bootstrap-seed-only for the owner email) — one
  system instead of env-plus-DB split-brain. If planning finds this bites deploy/auth
  (g8's sign-in path), fall back to env-for-signin + DB-for-flags and record why.
- **Flags start as exactly `{news_enabled}`.** No roles/RBAC, no per-feature matrix UI —
  the screenshot's "Role" dropdown is out; a flat flag list per user is in. More flags
  arrive only when a real feature needs one.
- **The feed catalog is code-shipped, not fetched** — a static name→URL map in the repo,
  curated by hand. Keeping it honest is a review-time concern, not a runtime dependency.
- **Braindump recreate is propose-then-accept** — the LLM output is staged in the UI and
  written only on explicit accept, mirroring LLM-proposes/code-disposes in settings form.

## Out of scope (do not build)

- Any change to the news pipeline itself (ingest, curation, serendipity, weekly rewrite —
  all goal-11 behavior frozen here).
- Roles/RBAC, invite links, CSV upload (screenshot affordances beyond the chip input),
  email notifications to added users.
- Per-user feed catalogs or catalog fetching/auto-discovery.
- Deep links / routing into settings tabs; mobile-specific shell work.
- Any Google API surface change, scope change, or router/write-set change.

## Acceptance criteria

- **Shell:** settings opens as a modal from the rail with side-nav entries Calendars ·
  Notes & hierarchy · Allowed emails (owner only) · News (news-enabled users only); the
  calendar toggle list and notes tree behave exactly as before inside it (existing
  verifier checks re-run green); the old settings page route is gone.
- **Users & flags:** owner adds two emails via the chip input in one action; each appears
  with a `news_enabled` toggle; flipping it on/off takes effect without redeploy (rail
  entry appears/disappears on next load; endpoints 403 when off — endpoint test);
  non-owner gets no panel and 403s on the management endpoints. (`NEWS_ENABLED_EMAILS`
  was already removed in goal 11 — the flag lives in `allowed_email.features`.)
- **News settings:** profile doc edits persist; revert restores the retained previous
  version; braindump → recreate shows a proposal that is saved **only** on accept (test:
  discard leaves the stored profile byte-identical); prompt builder still serializes no
  Google data (unit test); feed chips: typeahead snaps to a catalog entry, a valid raw
  feed URL is accepted after probe, an invalid one is rejected with a message, and a
  removed chip's feed is absent from the next ingest run's fetch set (unit test on the
  fetch-set builder).
- **Posture:** AST test green (no new Google methods anywhere); Alembic migration clean;
  router write set, scopes, eval gates untouched; `tsc`, frontend build, backend
  tests green.

## Harness upkeep (closing checklist — friction-driven only)

- `news.md`: braindump-recreate contract + feed-catalog convention.
- `frontend.md` rule: shell conventions if any emerged (modal, rail, panel extraction
  pattern).
- `verifier-web`: settings-modal navigation, flag-toggle effect, news-settings checks;
  re-point the old settings-page checks.
- Deploy/owner docs: allowlist/feature-flag management note (`goal-12-owner-steps.md` if
  owner actions surface). (`NEWS_ENABLED_EMAILS` was already removed in goal 11.)
- Record rule fire/no-fire (`/context`); wrap-up to the planning chat.
