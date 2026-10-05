---
paths: ["backend/app/router/**"]
---

# Auto-router safety (goal 5)

The router is the **only runtime LLM** in the system. It reads a captured thought and
*proposes* where it goes; deterministic code *disposes*. Read this before editing anything
under `backend/app/router/`.

## The contract (LLM-proposes / code-disposes)

- **No write lives in the LLM path.** `classifier.py` returns a `RouterClassification` and
  nothing else — it imports the Anthropic SDK, never `app.writes` or `app.google`. Every write
  (task-store or Google Docs) is in `service.py`. If you find yourself importing a writer into `classifier.py`, stop.
- **Insert-only blast radius (goal 7).** The router's *entire* write dependency set is
  exactly **`{create_task, reschedule, append_note}`** — the AST test pins it.
  - **`writes_svc.create_task`** (task content) + **`writes_svc.reschedule`** (the g4a date path to
    set the new task's due date — non-destructive metadata). **Goal 17:** both now target the
    **local task store** (no `creds`, no Google Tasks call); the router must not import
    `app.google.tasks` (`test_router_does_not_import_the_google_tasks_client`).
    `_resolve_list_id(session, user_id, target_list)` reads the pinned lists from the store
    (`store.get_tasklist_refs`), and `_create_task_from_fields` calls
    `tasks_store.deps.ensure_ready` first — a user not yet imported is imported, and a failed import
    (`503 tasks_import_pending`) leaves the entry re-routable.
  - **`writes_svc.append_note`** (goal 7): a high-confidence `note` is appended **insert-only** to
    the top of the configured notes Doc under an H3 timestamp. **Never a Docs delete or
    overwrite** — `append_note` only inserts. **Goal 7c amendment:** the Doc entry now carries
    **one LLM-authored line** — the classifier's `summary` one-liner (bold, between the timestamp
    and the body). The *raw text stays verbatim*; the summary is the only generated line. The write
    set and insert-only contract are **unchanged** (still `append_note`, no new writer); a
    missing/empty summary degrades to the goal-7 shape.
  Routing must **NEVER** reach a task delete (`writes_svc.delete`), the complete/uncomplete status
  write, `update_content`, or any `files.delete` / `files.update` content rewrite. Task delete is
  user-endpoint-only (writes.md).
- **Confidence gate.** A `task` or `note` is auto-acted only when `confidence >= CONFIDENCE_THRESHOLD`
  (`config.py`). Below threshold → review queue, never an auto-write.
- **Schema gate.** The classifier output must validate against `RouterClassification`. A model error,
  refusal, or unparseable result collapses to `unknown` (`classifier.py` returns
  `unknown_classification()` and never raises) → review.
- **Allowed-destination gate.** `task` and (from goal 7) `note` have live writers. A `note` writes to
  the Doc when `NOTES_DOC_ID` is set, else it degrades to **kept-local + a logged warning** (never a
  crash, never a blocked pipeline). `event` and `unknown` are never auto-acted — they go to review
  (calendar is read-only v1). **Doc/folder IDs come from config only, never from LLM output** — the
  router proposes a destination *class*; `service.py` picks the doc (`_notes_doc_config`).
- **Route-once / idempotency.** `route_entry` no-ops unless `routing_state == UNROUTED`; routing flips
  the state, so the scheduled job and `route-now` never re-route or double-create. A created task is
  not re-captured (no capture↔route loop).

## Layering

- `classifier.py` — the runtime LLM (structured output). No writes, no DB.
- `service.py` — deterministic dispose: reads the classification, performs/withholds writes,
  persists `routing_state`, builds review items. A write failure leaves the entry `UNROUTED`
  (re-routable) and raises `ApiError` — never swallowed, never half-written.
  - **Classify/dispose split (goal 9).** `classify_text(session, user_id, text)` is the pure LLM
    step (no DB write, no Google write); `route_entry(..., classification=None)` disposes it and
    will **reuse an injected classification** instead of calling the LLM again. This lets the
    capture UI pre-classify **during the ~5s undo toast** (`POST /scratch/classify` — side-effect
    free, discarded on undo) and hand the result back on commit, so the classifier latency hides
    behind the toast instead of stacking after it. Dispose stays deterministic either way — the
    confidence/schema/destination gates still run and a note's Doc still comes from path→id
    resolution, **never from the client-relayed payload** (same trust boundary as review-confirm).
- `scheduler.py` — in-process periodic `route_unrouted` (no Celery). `route-now` calls the same fn.
  **Goal 7c:** capture routes **inline** in `POST /scratch` (`route_entry` runs in the request; the
  response carries the routed state). The scheduler is demoted to a **retry backstop** for entries
  inline routing left `UNROUTED` — default interval stretched to ~15 min. Capture is persisted before
  routing, so a routing failure never loses it (2xx, entry `UNROUTED`).
- `evals/` — the labelled set + the scored runner; `score()` is a pure function (unit-tested without
  the API). The gate: clear-case destination accuracy ≥ 0.90 **and** zero task-class false positives
  **and** zero ambiguous cases auto-written. Notes are graded on **destination only** — the body is
  verbatim (deterministic) and the timestamp/top-insertion are unit-tested, not eval-graded. The
  goal-7c `summary` one-liner is **not eval-graded** (cosmetic, spot-checked — not a write decision).

## Model tiering

The router runs on a **small/cheap model** (`config.ROUTER_MODEL`, default `claude-haiku-4-5`) — this
is classification + light extraction, not reasoning. That is a product decision, not a compromise.

## Goal 9: hierarchy-aware note routing

- **Hierarchy in the prompt, path-not-id contract.** The classifier's system prompt gains a
  **dynamic per-user section** (`classifier._filing_section`): the user's notes hierarchy rendered
  as **paths only** (e.g. `conversations/john/growth`) plus the default-Doc fallback rule. **Drive
  ids NEVER enter the prompt and the LLM NEVER emits an id** (ADR layer 3 stands) — it proposes a
  `target_doc_path`; deterministic code (`settings.service.resolve_note_target`) maps path → stored
  id (case-insensitive exact leaf match; unknown/null → the default Doc). `route_entry` passes the
  leaf paths via `classify(text, doc_paths)`.
- **Doc choice never gates review.** The confidence gate is still about note-vs-task-vs-unknown; a
  wrong-doc guess is low-stakes (the note is still filed, its path shows in RECENT). Confirm-as-note
  re-validates a non-null `target_doc_path` against the index (**422** `unknown_doc_path` on a stale
  path — the dropdown only offers real leaves).
- **The 2nd verbatim relaxation.** The body written to the Doc is `note_text` — the raw capture with
  **only the routing prefix stripped**, under an emphatic preserve-verbatim prompt instruction and a
  deterministic **truncation guard** (`service._guarded_note_body`: a missing/empty/`< 50%`-length
  `note_text` falls back to the **raw text verbatim** — a mangled extraction never silently loses
  words). The one-liner (`summary`, the H3 headline) and the optional `keywords` (H5) are the only
  other LLM-authored lines. The **heading levels are stable per note**: the one-liner is always
  H3 (a placeholder when absent, so the timestamp is always H4), and only the leaf-level H5
  keywords skip when absent — a later "extract all H4s" search reliably yields every timestamp.
- **`routed_doc_path`** (nullable, on `ScratchEntry`) records where a `kept_note` landed (null =
  default Doc), set on both auto-route and confirm-as-note; `GET /scratch` returns it.
- Write set unchanged: still exactly `{create_task, reschedule, append_note}` (AST-pinned). The
  metadata rename (`docs.rename_file`) is **settings-path-only** and never reachable from the router
  (AST-asserted). Eval gate adds **doc-path accuracy ≥ 0.9** on the clear hierarchy subset; keywords
  + the one-liner stay un-graded.

## Goal 10: the routing-header contract (LLM-interpreted, code-enforced)

The first few words of a capture — the segment **before the first `-` or newline** — are
**order-insensitive routing words** (`task tomorrow …` ≡ `tomorrow task …`): a destination keyword,
date words, and/or doc-path words. A determinate header **takes precedence over the body**. Neither
prompt-only nor code-only works alone (prompt-only was exactly the observed failure — an explicit
`notes daily syncup` header bounced to review with the hierarchy already in the prompt), so:

- **The prompt carries the full contract** (`classifier._SYSTEM`, ROUTING HEADER block): segment
  definition, order-insensitivity, header-beats-body, worked examples. The LLM owns the
  **open-vocabulary** parts — free word order, multi-word doc paths (`daily syncup`), named weekdays.
- **Dispose adds thin deterministic guards for the CLOSED-vocabulary tokens** (`service._parse_header`,
  applied in both `route_entry` and `confirm_review`):
  - **Header detection is code:** the segment before the first `-`/newline, capped at ~8 words; longer
    or absent → no header (goal-9 body inference unchanged).
  - **Destination keywords force the destination.** `task`/`todo` → forced task; `note`/`notes` →
    forced note; the confidence gate is bypassed **for destination only** on a forced capture — an
    explicit keyword is user intent, never a review bounce. Neither/both keywords → the LLM's
    destination + the gate, as before.
  - **Unambiguous date words backstop a null date.** `today`/`tomorrow`/`day after (tomorrow)` with a
    null LLM `due_date` → code resolves it (IST, same `_today_ist` base). Named weekdays stay
    LLM-resolved, eval-graded. `_create_task_from_fields` takes the parsed header for this.
  - **Doc-path matching stays the LLM's** (multi-word, fuzzy), validated path→id exactly as today. A
    **forced note whose fields the LLM didn't produce** (it proposed a task) degrades safe: body = raw
    minus the header (`_dispose_note(..., degraded=True)`), no summary/keywords, default Doc unless the
    proposed path validates.
- **Guarded review-item fields (the un-mangled editor).** `_new_review_item` applies the goal-9
  truncation guard to `note_text` **at creation** (server-side single source of truth), so the editor
  prefill and confirm fallback both see the raw capture when the LLM's extraction was missing/short —
  not the low-confidence extraction it declined to auto-file. The frontend keeps its `?? entry_text`.
- Write set / scope / schema unchanged. Eval gate grows a **header-contract subset**: keyword headers
  parse + never bounce to review, and clear relative-date headers resolve a due date. Destination
  accuracy is graded on the **effective** (post-forcing) destination.

## Goal 10a: two daily-driver bugs the first MOM pastes exposed

**1. The `note_text` echo made the response scale with the input — and ate the other fields.**
`note_text` is the body echoed back VERBATIM, so a ~10k-char meeting-notes paste costs ~2.5k
output tokens to answer. Two distinct failures, both fixed by bounding the INPUT:
- Past `ROUTER_MAX_TOKENS` the JSON truncates, `parsed_output` is None, and the schema gate
  collapses the **whole** classification to `unknown`/0.0. Observed in production: every capture
  over ~8k chars failed, every short one was fine.
- Given more headroom it parses but the model spends the budget retyping the body and returns
  `summary`, `target_doc_path` **and** `keywords` as null — and the echo it produces is silently
  **abridged** (measured: 4.1k chars back from 9.9k in), so the body can't be trusted either.

It surfaced as three unrelated-looking bugs — note filed to the default Doc, no H3 one-liner, no
H5 keywords — plus a silent hit to the routing header's credibility (the header forced the note
correctly; there was nothing left to file it *with*). **The hierarchy was never stale**; don't go
looking for a cache. Verify a length hypothesis against `scratch_entry.route_result` before
anything else — it stores the exact classification and `unknown`/0.0 means the call *failed*.
- **Above `config.CLASSIFY_MAX_CHARS` the classifier is shown only the head** (`_excerpt`), and
  `classify` **discards any `note_text` it echoes back**. Prompt-only does NOT hold — the model
  echoes even when told not to (measured), so the code-side drop is the guard and
  `_echo_section` is only belt-and-braces. Routing reads off the head anyway: the header is the
  first line.
- **Code supplies the body instead** (`_guarded_note_body`), so this is **not** a third verbatim
  relaxation — it is *less* LLM involvement, not more: code copies the user's words rather than
  the model retyping them, which is strictly safer than the echo it replaces.
- **A capture's first line is not a header unless a closed-vocabulary token proves it**
  (`_Header.determinate` — a `task`/`note` keyword or a date word). `_parse_header` always
  populates `body`, but stripping it on an unproven segment silently eats the user's first line
  (`"line 0\nline 1…"` → `"line 1…"`). Undeterminate → fall back to the raw capture. Anything
  reading `header.body` must gate on `determinate`.
- Keep `ROUTER_MAX_TOKENS` generously above the echo cap: overflow is **catastrophic, not
  degraded** (unknown, not a shorter note), and headroom is free — `max_tokens` is a cap, not a cost.

**2. Route-once was a check-then-act, and inline routing made the window ~25s.**
`route_entry` read `routing_state == UNROUTED`, then wrote the final state only after the
classifier call + Docs append. Goal-7c (inline routing) and goal-10 (long pastes) stretched that
gap wide enough for a second router — the scheduler backstop, a "Route now" click, a retried POST
— to read the same UNROUTED row and **append the same note to the Doc twice** (observed: one
capture, two Doc entries). The route-once *contract* was never wrong; its *implementation* was
not atomic.
- **`_claim_for_routing` is a conditional UPDATE** (CAS `UNROUTED` → `ROUTING`): the WHERE clause
  re-checks the state inside the database's own atomic write, so exactly one caller wins and the
  losers no-op. Any new routing entry point **must** claim before doing slow work — the plain
  `!= UNROUTED` check is only a cheap early-out, never the guard.
- **A claim is transient, never a grave.** `_release_claim` hands it back to `UNROUTED` on
  failure (the goal-5 re-routable contract); `route_unrouted` reclaims claims older than
  `_STALE_CLAIM_SECONDS` (`routed_at` doubles as the claim timestamp) so a process that dies
  mid-route still recovers. `_STALE_CLAIM_SECONDS` must stay well above a real inline route.
- `confirm_review` has the same check-then-act shape on `status == PENDING`; it has not bitten
  (no LLM call in that path, and confirm is a deliberate click) but it is the same class of bug.

## Goal 8: per-user routing

Routing is per-user. `route_entry(session, user, creds, entry)` /
`route_unrouted(session, user, creds)` / `confirm_review(session, user, creds, item_id, ...)` take
the current `User` + their live `creds`; every Google call (notes; since goal 17 tasks are local)
uses those creds and every
`scratch_entry` / `review_item` is written/read with `user_id`. The notes Doc is the **user's own**,
resolved via `app.settings.service.ensure_notes_target(session, creds, user_id)` (app-created folder +
Doc on first need) — the `NOTES_DOC_ID`/`NOTES_FOLDER_ID` env vars and the "kept-local when unset"
degrade are gone; a note always has a home. The scheduler backstop iterates users and loads each
user's creds (`app.google.auth.load_credentials`) per tick. The insert-only write set
`{create_task, reschedule, append_note}` and the AST test are unchanged.
