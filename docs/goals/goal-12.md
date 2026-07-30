# Goal 12 — Dev view: meeting notes → GitHub issue drafts → one-click filing

**One line:** A third rail view (**Dev**) where GitHub issue drafts appear automatically —
a cron (+ manual **Create now**) reads **only the not-yet-processed entries** across a
whole subtree of user-configured app-created notes Docs (per-doc **DB cursor**, no in-doc
marker), a **synthesising LLM** (opus) reads *all* of the day's new entries at once and
proposes a **de-duplicated** set of issues `{title, body, repo, sources[]}` — collapsing
the same action item mentioned in several conversations into one draft — and the
owner reviews/edits/approves cards; on approve, **deterministic code** files the issue via
the GitHub REST API and adds it to the repo's configured **ProjectsV2 project** via GraphQL
(fine-grained PAT, Fernet-encrypted in `user_settings`) — the app's **first non-Google
write surface** and its **first Docs read path**.

## Intent / acceptance bar

The failure this kills: I attend back-to-back meetings, transcribe, get an MOM from a
model, paste it into the scratchpad with a routing header ("notes internal kaapi"), the
router files it into the right Doc — **and then I forget to action it**. Desired: without
me doing anything, the backend periodically (or on demand, right after a meeting) picks up
the MOM entries that landed anywhere in my configured notes subtree since the last scan,
sends *all of that new text at once* to a capable model, and the Dev tab fills with draft
issue cards — each with a title, a written-out body, and a sensible default repo picked
from my configured list. Crucially, a given topic surfaces once even when I chewed on it in
three separate meetings: the model sees the whole day together and **synthesises** the
scattered mentions — same doc at different timestamps, or different docs — into a single
draft that cites all of them, instead of handing me three near-duplicate cards to
reconcile by hand.
Once a day (or whenever) I skim the cards, tweak a title or body, maybe switch the repo,
and hit approve — the issue is created in the org repo (e.g. `kaapi-backend`) **and
attached to that repo's project**, so it shows up in my GitHub project backlog view
immediately, with zero search-tag-shuffle ceremony. Dismissing a card is free and touches
nothing. The LLM never files anything; only my approval does, through deterministic code.
Reprocessing never happens: a scanned entry is consumed exactly once, tracked by a DB
cursor keyed to the timestamps the app already writes into every Doc entry — there is no
marker in the Doc, so there is nothing for a human (me, editing the Doc by hand) to
accidentally delete. Nothing about the Google posture moves: `drive.file` already covers
reading Docs the app created, doc ids come from my config (never LLM output), and the
Google write set is byte-identical to goal 10's.

## What ships

- **1. Docs read path + cursor (deterministic code).** A `dev` backend module:
  - **Source Docs are user-configured**: a picker over the goal-9 notes hierarchy marks
    which nodes are meeting-notes sources — leaf Docs (e.g. `internal/kaapi`) **or
    folders** (e.g. `internal`), where a selected folder means *every* Doc under it,
    recursively. Folder selections resolve to concrete doc ids **at scan time** from the
    hierarchy index in the DB, so a Doc created later under a selected folder is picked up
    automatically. Stored per user; ids always resolved from the hierarchy index — never
    from LLM output.
  - **First sanctioned Docs read**: `documents.get` on those app-created Docs. No scope
    change — `drive.file` grants read/write on files the app created. The AST
    write-dependency test grows a pinned entry for the one new read method; the Docs
    *write* surface is untouched (still insert-only, no new mutation types).
  - **Entry parser**: splits a Doc into entries by the goal-10 uniform shape (**H3
    one-liner → H4 timestamp → optional H5 keywords → body → delimiter** — note goal-10
    flipped the goal-9 order; the **timestamp is the H4 line**, not the H3). Timestamps
    parse back from the H4 text the app itself wrote (`6-July-2026, 8:41 PM IST`), and the
    H3 one-liner is the human-readable entry title. Docs are
    **newest-first** (captures prepend at the top), so new-entry selection is by parsed
    timestamp, never by document position; the descending order may be used as an
    early-exit optimization but correctness must not depend on it. Multiple entries can have the same timestamp, so drill down to find last entry with same timestamp, such that no entry is missed.
  - **Per-doc DB cursor** (`dev_doc_cursor`: `user_id, doc_id, last_processed_entry_ts,
    boundary_entry_keys`, Alembic migration): a scan reads the Doc and keeps entries with
    timestamp strictly newer than the cursor, **plus** entries *equal* to the cursor
    timestamp whose key is not in `boundary_entry_keys` — timestamps are minute-granular
    and several entries can legitimately share one (batch pastes do this in practice), so
    a strictly-newer rule alone would silently drop a same-minute entry captured after a
    scan. `boundary_entry_keys` holds stable keys (hash of entry timestamp + H3 one-liner)
    of the entries already processed at exactly `last_processed_entry_ts`. The cursor
    advances after drafts are stored. Forward-only, process-once, idempotent — a rerun
    sends **nothing** to the LLM. **No in-doc marker**:
    the originally-sketched DO-NOT-DELETE text marker is rejected (a moving marker =
    delete+insert, violating the insert-only invariant, and is human-deletable anyway);
    the DB cursor buys the same token savings with zero Doc mutations.
  - **Scheduler**: the in-process asyncio pattern (goal-5 router / goal-11 news — no new
    dependency), default **once daily (end of day)** so the whole day's mentions synthesise
    together rather than being split across cron windows, plus a manual **Create now**
    endpoint + button in the Dev view header (same spirit as Route now / Fetch now) for the
    right-after-a-meeting case. **All new entries across all resolved source Docs are
    gathered into one batch before the single LLM call** — synthesis needs the whole day in
    one context.
  - **The cron is gated on the `dev` flag — cost, not just access.** The scheduled scan
    iterates **only** users whose `allowed_email.features.dev` is on (superuser always
    counts) **and** who have finished config (≥1 source Doc + a valid PAT). Everyone else is
    skipped before any Doc is read or any LLM token is spent — the flag is the switch that
    keeps the opus call from firing for users who don't use the feature. `Create now` is
    likewise flag-gated (the endpoint 403s without it). This **mirrors the existing news
    scheduler** verbatim — `news/scheduler.py::_tick_all_users` already `continue`s past any
    user failing `gating.is_news_enabled(session, user)` before any profile/LLM work, so the
    precedent (and cost discipline) is set; the dev scan copies that shape with a
    `gating.is_dev_enabled` delegating to `auth.service.is_feature_enabled(..., "dev")`.
- **2. Synthesising LLM (the fourth runtime LLM).** One batched call per scan per user
  (**opus** — this step is not classification; it must recognise that a login bug raised in
  standup and "auth failing" written up after a 1:1 are the *same* issue, and merge them.
  Cross-conversation synthesis is genuine reasoning and the volume is ~once a day, so the
  stronger model is worth it; env-configurable model id; structured output). **Input**: the
  full set of new entries across the whole configured subtree since the cursor — *every*
  new entry from *every* source Doc in one prompt — each tagged with its source doc path +
  entry timestamp, plus the user's configured **repo catalog** (repo full names + a short
  user-written description each). **Output**: a **de-duplicated** list of proposed issues,
  each `{title, body_markdown, repo, sources}` where `sources` is a **list** of
  `{doc_path, entry_ts}` — one draft may cite several entries it synthesised, and one entry
  may contribute to several drafts (an MOM with no action items contributes to none). The
  prompt instructs the model explicitly to collapse repeated/overlapping mentions of the
  same underlying work into a single issue and to write the body from the union of what was
  said. **LLM-proposes / code-disposes**: a returned repo not in the catalog falls back to
  the default repo; bodies/titles are stored verbatim as *drafts* in a `dev_issue_draft`
  table (`id, user_id, title, body, repo, status draft|filed|dismissed, sources` — a JSON
  array of `{doc_id, entry_ts}` provenance rows — `issue_url, created_at`, Alembic
  migration). To keep runs from re-proposing something already handled, the call is also
  given the titles of the user's still-open drafts and recently-filed issues as
  *do-not-redraft* context (dedup across scans, not just within one). **The LLM has no
  GitHub access and files nothing.** It sees entry text + repo catalog + open-draft titles
  only — never doc/folder ids, never tokens.
- **3. The Dev view (third rail entry).** Rail becomes Home | News | Dev:
  - A card list of drafts, pending first, then filed/dismissed (collapsed or dimmed tail).
  - **Card anatomy**: editable title; editable body (auto-growing markdown textarea);
    **repo dropdown** over the configured catalog with the LLM's pick preselected; a
    **project dropdown** over the projects the PAT can see for the selected repo (the repo's
    configured default preselected, changing the repo repopulates it) — editable, not
    read-only, since the PAT already fetches the list; a muted **sources line** listing
    every entry the draft was
    synthesised from (`internal/kaapi · 28-Jul 3:12 PM` + `internal/standup · 28-Jul 10:04
    AM`), so a merged issue visibly shows its provenance.
  - **Actions**: **Approve & file** (the only path to a GitHub write), **Dismiss** (local
    status flip, zero GitHub calls), inline edits persisted on blur. A filed card shows
    the issue link (`repo#123`, opens GitHub in a new tab).
  - Header: last-scan time + **Create now**.
- **4. GitHub filing layer (deterministic code, the first non-Google write surface).** On
  approve: `POST /repos/{owner}/{repo}/issues` (REST) with the card's current
  title/body, then `addProjectV2ItemById` (GraphQL) to attach the new issue to the repo's
  configured project. **Idempotent with partial-state recording**: the issue number/url is
  stored the moment creation succeeds; if the project-add fails, the card shows
  "filed — project attach pending" and retry re-runs **only** the GraphQL step — an issue
  is never double-created. Failures surface as an error state on the card (rollback-not-
  blind-retry ethos). Auth: a user-supplied **fine-grained PAT** (org-scoped; Issues
  read/write + org Projects read/write + Metadata read), stored **Fernet-encrypted** in
  `user_settings` exactly like the Google refresh tokens — never logged, never echoed back
  (the config API returns only a masked hint + validity).
- **5. Config section (inside the Dev view — not the settings page).** Compact and
  self-contained so this goal stays independent of the g13 settings-modal work. **The PAT
  is the key that unlocks the rest** — once it is saved, repos and projects are *fetched
  from GitHub*, not hand-typed:
  - **PAT (entered first)**: paste-once write-only field; a validation ping (viewer + repo
    metadata) on save; masked thereafter. Everything below is disabled until a valid PAT
    exists.
  - **Repos**: the app **enumerates the repos the PAT can see** (a fine-grained PAT is
    already scoped to a chosen repo set at mint time, so this list is exactly the granted
    repos — no manual `org/repo` typing). The user ticks which of them are issue targets,
    marks one **default**, and writes the **one-line description** each (the only hand-typed
    field — it feeds the LLM's repo-pick, so it stays human-authored).
  - **Project per repo**: for each selected repo the **ProjectsV2 list is fetched via the
    API**; the user picks the repo's **default** project (auto-selected when exactly one
    exists — the `kaapi-backend` case), stored as project node id + title. This is only the
    *default* — the per-card project dropdown (§3) can override it at file time.
  - **Refresh**: a manual "re-sync from GitHub" action re-pulls repos + projects so newly
    granted repos or newly created projects appear without re-entering the PAT.
  - *This same PAT-fed metadata fetch is the foundation a later granularity goal builds on:
    labels, milestones, and assignable users are all one more metadata call per repo — out
    of scope here (repo + project only), but the plumbing lands now, deliberately.*
- **6. Access gating (UI *and* cron).** A `dev` feature flag on the `allowed_email.features`
  column — the goal-11 mechanism verbatim (`auth.service.FEATURES` registry gains `dev`;
  **superuser always on**; for everyone else the superuser ticks the box in Settings →
  Allowed emails, exactly like `news`). The flag gates **three** surfaces, not two: (a) no
  flag → no Dev rail entry; (b) every `/dev/*` endpoint 403s; (c) **the scheduled scan skips
  unflagged users entirely** — the flag is read inside the cron's per-user loop, so an
  unflagged user is never read from Docs and never sent to the model — mirroring the news
  scheduler, which already gates its per-user loop the same way (`is_news_enabled`).
- **7. Guardrail artifacts in lockstep.** A new **`dev.md` rule** pins the contract: the
  drafting-LLM input surface (new-entry text + repo catalog + open-draft titles, no
  ids/tokens), LLM never
  calls GitHub, code files only human-approved drafts, repo/project ids validated against
  config, PAT encrypted + never logged, Docs read is `documents.get`-only on configured
  app-created Docs, cursor is DB-only (no Doc markers). AST test: + one pinned Docs read
  method; Google write surface unchanged. README spanning-constraint amendments (runtime-
  LLM set + first Docs read) ship with this brief (done 2026-07-29).

## Draft decisions (2026-07-29)

*Drafted from the brainstorm session; overturn in planning if wrong.*

- **Read the Docs, not the local `scratch_entry` rows** (owner picked): the Doc is the
  durable, human-curated record — reading it catches text pasted or edited directly in
  Docs and stays correct if a transcript tool ever writes there via the app later. The DB
  rows remain a non-source (they'd miss out-of-band edits).
- **DB cursor beats the in-doc marker** (owner picked over their own initial sketch): the
  marker would have to move every run (delete+insert — breaks the insert-only invariant)
  and is deletable by the human it warns. The cursor is invisible, undeletable, and equally
  token-frugal. Consequence documented: text manually inserted *behind* the cursor (into an
  already-processed region) is never re-scanned — the workaround is a fresh capture.
- **Fine-grained PAT in `user_settings`** (owner picked): admin on the org is sufficient to
  mint one; per-user, rotatable without redeploy, Fernet-encrypted like Google tokens. A
  GitHub App is the clean long-term answer but overkill for a personal dashboard v1.
  *Caveat for owner-steps: the org must allow fine-grained PATs (an org policy toggle).*
- **Config lives in the Dev view** (owner picked): keeps g12 fully independent of the g13
  settings restructure; g13 may later relocate it, mechanically.
- **Opus, not sonnet/haiku, for the synthesising step** (owner asked sonnet-vs-opus): the
  v1 requirement grew a hard part — *de-duplicating the same latent issue across separate
  conversations and timestamps*. That is semantic reasoning over the whole day's text, not
  per-entry drafting, and it is exactly where a stronger model separates from a cheaper one
  (a weaker model tends to either miss the match and emit duplicate cards, or over-merge
  two genuinely-distinct items). Volume is ~once a day over a bounded batch, so the cost
  delta is negligible. Model id stays env-configurable — drop to sonnet if it proves good
  enough in practice.
- **Scan cadence: end-of-day daily + Create now (revised).** Whole-day synthesis wants the
  day's mentions *together*; a 6-hour cron fragments the day, so the morning batch can file
  an issue the afternoon batch then re-drafts. Default to one end-of-day run (the natural
  "review the day" moment) plus **Create now** for the after-a-meeting case. Cross-scan
  duplication is further contained by feeding open-draft/recently-filed titles into the
  prompt as do-not-redraft context (§2). A planning-time knob; the mechanism (asyncio
  scheduler) is unchanged.
- **Only repo + project in v1** (owner-stated): priority, labels, assignees, milestones
  are explicitly later goals.
- **Drafts are cheap, filing is sacred**: every GitHub mutation requires a human approve on
  a specific card; there is no bulk-approve and no auto-file mode in v1.

## Out of scope (do not build)

- Issue **priority/labels/assignees/milestones** (owner-deferred — repo + project only).
- Bulk approve, auto-file without human review, or filing from the cron path.
- Editing or syncing an issue **after** filing (no status sync back from GitHub, no
  two-way anything).
- Granola / transcript ingestion (still parked — this goal starts at the notes Doc).
- Reading Docs the app did not create (the `drive.file` line holds; no scope change).
- Any new Docs/Drive **write** (no markers, no annotations in the Doc — read-only path).
- Attaching one issue to **more than one project at once**; org project matrices; GitHub
  App auth; webhooks. (Choosing *which single* project on a card — from the list the PAT
  fetches — is in scope; attaching to several simultaneously is not.)
- Settings-modal placement of the config (g13 territory); mobile shell work.
- Any router / news-pipeline / Google-write-set change.

## Acceptance criteria

- **Source resolution:** a selected folder resolves to all Docs beneath it (recursively)
  at scan time — a Doc added to the hierarchy under that folder *after* selection is
  included in the next scan without touching the config (unit test on the resolver
  against the hierarchy index).
- **Cursor & parser:** a fixture Doc in the goal-10 entry shape (**H3 one-liner → H4
  timestamp → H5 keywords**) — **newest entry first**, matching real capture order —
  parses into entries with the timestamp read from the **H4** line (unit test);
  scanning twice in a row sends **zero** entries to the LLM on the second pass (unit test
  on the scan job with a stubbed LLM); two entries sharing the same minute-granularity
  timestamp split across two scans → the later-captured entry is processed exactly once,
  not skipped and not doubled (unit test on the boundary-key logic); the cursor only
  advances after drafts persist (crash between LLM and store → rescan reprocesses, no
  entry lost — unit test).
- **Batching & synthesis:** a scan gathers new entries from *several* resolved source Docs
  into a **single** LLM call (unit test on the batch assembler — one call, all docs'
  deltas present); a fixture where the same action item appears in two entries (different
  docs and/or different timestamps) yields **one** draft whose `sources` array cites
  **both** entries, not two drafts (unit test with a stubbed LLM returning the merged
  shape — asserts the store persists the multi-source provenance); the prompt carries
  open-draft/recently-filed titles as do-not-redraft context (prompt-builder test).
- **Drafter dispose step:** prompt-builder unit test — exactly {entry text, doc path,
  entry timestamp, repo catalog, open-draft titles} serialized, no doc/folder ids, no
  tokens; an out-of-catalog repo in the LLM output lands as the default repo (unit test);
  an entry yielding no action items contributes to no drafts (fixture).
- **Filing:** approve on a card creates the issue in the configured repo **and** it
  appears in the configured ProjectsV2 project (verified against a scratch test
  repo/project — see owner-steps); the card flips to filed with a working issue link;
  dismiss performs no GitHub call (unit test with a mocked client); a mocked project-add
  failure leaves the recorded issue number intact and retry re-runs only the GraphQL step
  (unit test — no second issue).
- **Secrets:** the PAT round-trips write-only — config GET returns a mask, the PAT string
  appears in no log line and no API response (test).
- **Gating & shell:** rail shows Dev only for `dev`-flagged users; `/dev/*` endpoints 403
  otherwise (endpoint test); Create now triggers a scan on demand. **Cron scoping:** a
  scheduled run over a user table where only some users are `dev`-flagged reads Docs and
  calls the (stubbed) LLM for the flagged users **only** — unflagged users get zero Doc
  reads and zero LLM calls (unit test on the cron's per-user loop with a spy); an unflagged
  user's `Create now` 403s.
- **Posture:** AST test green with exactly one new pinned Google method (the Docs read);
  Google scopes and write set byte-identical; router evals untouched; Alembic migration
  clean; `tsc`, frontend build, backend tests green.
- `goal-12-owner-steps.md` exists (org fine-grained-PAT policy check, PAT creation with
  exact permissions, scratch test repo + project setup, source-doc + repo config
  walkthrough).

## Harness upkeep (closing checklist — friction-driven only)

- New `dev.md` rule: the drafting-LLM contract, GitHub write contract, Docs-read contract,
  DB-cursor convention (as sketched in §7).
- README ladder + spanning-constraint amendments — shipped with this brief (2026-07-29);
  verify at close they still match as-built.
- `verifier-web`: Dev-view checks (rail gating, card render/edit, Create now). GitHub
  write verification needs a scratch-repo recipe analogous to the `zz-verifier-test`
  task lists — extend `verifier-writes` (or add a sibling note) with the test-repo
  convention + cleanup (close created test issues).
- `goal-12-owner-steps.md` kept current as steps are discovered.
- Record rule fire/no-fire (`/context`); wrap-up to the planning chat.
