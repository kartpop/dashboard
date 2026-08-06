---
name: verifier-web
description: Launch recipe and observation protocol for verifying changes to this dashboard (FastAPI backend on :8010, React/Vite frontend on :5173). Use whenever the verify skill requires a handle on this app's surfaces.
---

# Web verifier — dashboard

Covers the two runtime surfaces: the FastAPI backend (`:8010`) and the React frontend (`:5173`).
Both must be running before you drive either surface.

## Prerequisites

- Google OAuth token already minted at `backend/.google-tokens/token.json`. If absent, run
  `cd backend && uv run python -m app.google.auth` once interactively (opens a browser).
- Overlay DB migrated: `cd backend && uv run alembic upgrade head` (safe to re-run; no-ops if
  already at head).
- Node deps installed: `cd frontend && npm install` (skip if `node_modules/` exists and
  `package.json` hasn't changed).
- Playwright ready (for the GUI surface): the `playwright` package is a backend **dev
  dependency** (in `pyproject.toml` / `uv.lock`), so `uv sync` installs it. The **browser binary
  is NOT in the lockfile** — run `cd backend && uv run playwright install chromium` once per
  machine (and again after a `playwright` version bump, since the required Chromium build is
  pinned to the package version). Verify with `uv run python -c "from playwright.sync_api import
  sync_playwright"`. NOTE: this fixes local runs; a restricted agent sandbox may still block a
  browser launch — fall back to a main-session / manual pass there.

## Launch

Start each in a background tmux pane (or `run_in_background`) so both are up before you drive
them.

```bash
# Backend
cd backend && uv run uvicorn app.main:app --reload --port 8010

# Frontend
cd frontend && npm run dev
```

Wait for the backend to print `Application startup complete` and the frontend to print
`Local: http://localhost:5173` before sending requests.

## Backend surface (API)

Drive with `curl` or `httpx`. The base URL is `http://localhost:8010`.

```bash
# Tasks (grouped view) — check for buckets/items shape, no priority field
curl -s "http://localhost:8010/tasks?view=grouped" | jq '.task_lists[0].buckets[0]'
# Expected: {"label": "...", "key": "...", "items": [...]}
# Each item has "type": "task" or "type": "group"; no "priority" field anywhere

# Verify no "groups" key at task_list level (renamed to "buckets")
curl -s "http://localhost:8010/tasks?view=grouped" | jq 'keys'
# Should show: ["task_lists"] with each list having "buckets", not "groups"

# Create a group
curl -s -X POST http://localhost:8010/tasks/<LISTID>/groups \
  -H 'Content-Type: application/json' \
  -d '{"name": "Test Group", "bucket_key": "2026-06-09", "rank": 1000}' | jq .

# PATCH overlay (rank + group_id)
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/<TASKID>/overlay \
  -H 'Content-Type: application/json' \
  -d '{"rank": 500, "group_id": 1}' | jq .

# Ungroup (group_id: null)
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/<TASKID>/overlay \
  -H 'Content-Type: application/json' \
  -d '{"rank": 500, "group_id": null}' | jq .

# PATCH group rank
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/groups/<GROUPID> \
  -H 'Content-Type: application/json' \
  -d '{"rank": 750}' | jq .

# DELETE group (members become standalone)
curl -s -X DELETE http://localhost:8010/tasks/<LISTID>/groups/<GROUPID> | jq .

# Error shape check (should 400)
curl -s -X PATCH http://localhost:8010/tasks/foo/bar/overlay \
  -H 'Content-Type: application/json' \
  -d '{}' | jq .
```

### Calendar day read (goal 7b — read-only)

```bash
# Today's IST events for the header strip (default = today).
curl -s http://localhost:8010/calendar/day | jq '.date, (.events[0])'
# A specific day; each event: {id,title,start,end,all_day,meet_link,location,attendees[]}.
curl -s 'http://localhost:8010/calendar/day?date=2026-07-08' | jq '.events | length'
# Invalid date → 400 invalid_date envelope.
curl -s 'http://localhost:8010/calendar/day?date=nope' | jq .
```

### Write endpoints (goal 4 — Google writes)

These mutate Google Tasks (due date + list membership). **Never fire them against a real list** —
they go through the `verifier-writes` skill, which seeds and tears down `zz-verifier-test` lists.
Load `verifier-writes` for the full recipe; the shapes:

```bash
# Reschedule = due-date change (POST, not PATCH). due_date is "YYYY-MM-DD" (IST) or null (NO_DATE).
curl -s -X POST http://localhost:8010/tasks/<LISTID>/<TASKID>/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"due_date": "2026-06-15", "rank": 1000, "group_id": null}' | jq .
# group_id must reference a group in the DESTINATION bucket, else 422 group_wrong_bucket.

# Move to another list (insert-then-delete; overlay row migrates to the new task id).
curl -s -X POST http://localhost:8010/tasks/<LISTID>/<TASKID>/move \
  -H 'Content-Type: application/json' \
  -d '{"target_list_id": "<OTHERLISTID>"}' | jq .
# Move to the current list → 400 same_list.
```

### Content CRUD endpoints (goal 4a — Google content writes)

Also exercised only against `zz-verifier-test` lists (`verifier-writes`). Shapes:

```bash
# Create a task (lands undated → NO_DATE, top of bucket). 201, returns the new task.
curl -s -X POST http://localhost:8010/tasks/<LISTID> \
  -H 'Content-Type: application/json' -d '{"title": "vt-new", "rank": 1000}' | jq .
# Empty title → 400 empty_title.

# Edit content — PATCH the bare task. Only the fields sent are written; status rides this path.
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/<TASKID> \
  -H 'Content-Type: application/json' -d '{"title": "renamed"}' | jq .
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/<TASKID> \
  -H 'Content-Type: application/json' -d '{"notes": "a note"}' | jq .
curl -s -X PATCH http://localhost:8010/tasks/<LISTID>/<TASKID> \
  -H 'Content-Type: application/json' -d '{"status": "completed"}' | jq .   # uncomplete: needsAction
# Empty body → 400 no_fields; empty title → 400 empty_title; missing task → 404.

# Delete a task (immediate on the backend; the ~5s defer/undo is frontend-only).
curl -s -X DELETE http://localhost:8010/tasks/<LISTID>/<TASKID> | jq .

# Rename a list (tasklists resource).
curl -s -X PATCH http://localhost:8010/lists/<LISTID> \
  -H 'Content-Type: application/json' -d '{"title": "zz-verifier-test-renamed"}' | jq .
```

## Frontend surface (GUI)

Drive with Playwright. The origin is `http://localhost:5173`.

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto("http://localhost:5173")
    page.wait_for_selector(".panel")          # panels rendered
    page.screenshot(path="/tmp/dashboard.png")
    browser.close()
```

Key selectors:
- `.panel` — each surface panel (My Tasks, Follow-ups, Scratchpad). **Goal 7b:** the page is exactly
  those three; the below-fold Calendar panel and the "Other tasks" section were **removed**.
- `.task-list-section` — one per Google task list
- `.date-group` / `.date-group-label` — bucketed date groups (key = bucket key)
- `.task-item` — individual task row (standalone or within group)
- `.drag-handle` — drag affordance (⠿ braille block)
- `.group-container` — a named group container (bordered box)
- `.group-header` — group name + drag handle + delete button
- `.group-name` — clickable group name (click to rename)
- `.add-group-btn` — "+ group" affordance per bucket
- `.panel-error` — error message (present only on fetch failure)
- `.task-menu` — the ⋯ per-task menu trigger (move-to-list + delete)
- `.task-menu-popover` — the open menu popover. **Goal 4a: it is portaled to `<body>`** (escapes
  the group clip), so query it at the document root, NOT inside `.group-container`.
- `.move-to-list-option` — each target-list option inside the popover
- `.task-menu-delete` — the Delete action inside the ⋯ popover (goal 4a)
- `.toast` — write-failure toast (`role="alert"`; appears only when a Google write fails and local
  state has rolled back)

### Goal-4a selectors (full CRUD / MVP)
- `.panel-head` / `.panel-refresh` — panel header and the manual refresh (⟳) button
- `.list-title` — clickable list header (click → `.list-title-input` to rename the list)
- `.add-task-btn` — per-list "+ add task" affordance → `.add-task-input` + `.add-task-confirm`
- `.task-row` — the horizontal row inside each `.task-item` (the `.task-item` is now a column)
- `.task-check` — the per-task complete checkbox (on standalone AND grouped tasks)
- `.task-title-input` — inline title editor (click `.task-title` to open)
- `.notes-toggle` — the notes expand triangle (`--open` when expanded, `--has` when notes exist)
- `.task-notes` — the notes textarea (placeholder "Add notes" when empty)
- `.task-date` — the per-task `<input type="date">` due-date picker
- `.toast.toast--action` (`role="status"`) + `.toast-undo` — the Undo toast (completion + delete)
- Overdue bucket: a `.date-group` whose `.date-group-label` reads **"Overdue"** (bucket key
  `OVERDUE`); it is render-only — never a drag target.

**No `.priority-badge` selector** — priority was removed in goal 3.

### Goal-7b selectors (calendar header strip)
- `.dashboard-header` — the flex header row (`<h1>` + the strip), vertically centered.
- `.calendar-strip` — the whole strip (hidden below the 1080px breakpoint). Default window 8a–7p.
- `.strip-block` (`role="button"`) — a meeting block, single shared lane; overlaps stack
  back-to-front by duration (shortest in front), staggered down a few px with aligned bottoms so
  every event keeps a visible sliver. `.sb-accepted` = owner-accepted (light orange);
  `.sb-pending` = not accepted (light gray). `.strip-block-title` is the truncated label;
  `.strip-block-more` is the "+N" badge on a cluster's front block. Single click: solo block
  copies its Meet link; a block in a multi-event cluster opens the picker. Alt+click opens that
  block's link in a new tab.
- `.strip-picker` — the overlap chooser (rows `.strip-picker-row` with `.spr-dot`/`.spr-time`/
  `.spr-title`; click copies that event's link); `.strip-picker-backdrop` closes it.
- `.strip-tooltip` (`role="tooltip"`) — hover detail (title / time / location / organizer `.tt-org`
  / per-attendee RSVP rows `.tt-att-row`) with a `.tt-open` open-in-new-tab link; `.tt-right`
  anchors it right for late-day blocks.
- `.strip-now` — the red now-marker (present only when viewing **today** and now is in-window;
  10s tick).
- `.strip-chevron` — the ±1h window shifters (disabled at 00:00 / 24:00); `.strip-hint-badge` is
  the orange out-of-window meeting count. `.strip-refresh` — manual refresh (spins while loading;
  auto-refresh every 3 min).
- `.strip-daypill` — prev/next-day navigation; `.strip-viewed` — the centered viewed date;
  `.strip-today` — the jump-back button (present only when viewing a non-today day).
- `.strip-toast` — the transient "Meet link copied" / "No Meet link for this event" confirmation.

### Goal-7c selectors / checks (instant routing, editable review, create-row date)
- `.add-task-top-row` — the create row: `.add-task-input` (title) + `.task-date` (due picker) +
  `.add-task-confirm` / `.add-task-cancel`; `.add-task-notes-input` is the notes textarea below.
  Picking a date + Add creates the task **in that date bucket** (one optimistic result).
- `.review-fields` — the editable review item. `select` type choice offers **exactly `task | note`**
  (`event` dropped from the UI). Switching type swaps the fields: **task** → `.review-title` +
  `.review-notes` + `.task-date` + `.review-hint`; **note** → `.review-summary` (bold one-liner) +
  `.review-note-text`. Confirmed edits are what land.
- **Instant-routing check (the goal-7c headline):** capture a high-confidence task
  (e.g. "buy milk tomorrow") via `.capture-input` + Shift+Enter, wait past the ~5s undo hold, and
  assert the RECENT entry badge (`.scratch-badge.state-routed_task`) shows **→ Task** and the task
  appears in the panel — **without** clicking `.route-now-btn`. `POST /scratch`'s own response
  carries the routed state; no `route-now` call is needed. (A failure leaves `.state-unrouted`; the
  scheduler backstop — now ~15 min — retries.)

**Goal-4 DnD note:** there is now ONE `<DndContext>` per task list (it spans the list's buckets), so a
task can be dragged *between* date buckets = reschedule (one `reschedule` POST + optimistic re-bucket).
A within-bucket drag still fires only an overlay PATCH (no Google write). Write-path verification
(reschedule/move, failure rollback, the `.toast`) is covered by the `verifier-writes` skill against
`zz-verifier-test` lists only.

To observe PATCH/POST/DELETE requests fired by the frontend, attach a route listener:

```python
mutations = []
page.on("request", lambda r: mutations.append(r) if r.method in ("PATCH","POST","DELETE") else None)
page.goto("http://localhost:5173")
```

### Goal-4a UI-flow checks (behaviours endpoint checks can't see)

Drive these with Playwright + a request listener (`mutations` above), against `zz-verifier-test`
lists only. They assert *state-machine* properties, not just final state:

- **Complete + Undo fires ZERO Google writes on undo.** Check `.task-check` on a task → it leaves
  the active view, exactly one `PATCH …/{task}` with `{status:"completed"}` fires, and a
  `.toast--action` appears. Click `.toast-undo` *within ~5s* → the task returns and exactly one
  more `PATCH` with `{status:"needsAction"}` fires (the undo). Completing a group's last member
  removes the group (and undo restores it).
- **Deferred DELETE fires ONLY after the window.** `⋯` → `.task-menu-delete` → row vanishes +
  `.toast--action`. Clicking `.toast-undo` quickly → **no `DELETE` request at all**. Letting the
  toast expire (>5s) → exactly one `DELETE …/{task}` fires.
- **Date-picker reaches a no-bucket date.** Set `.task-date` to a date weeks out (no existing
  bucket) → one `reschedule` POST; after the silent refetch the task appears in a new date bucket.
  Setting a past date → it lands in the **Overdue** bucket; clearing → `NO_DATE`.
- **Overdue rollup** renders as a single top `.date-group` labelled "Overdue" (not scattered
  past-date buckets).
- **Refresh** (`.panel-refresh`) re-runs `GET /tasks` (observe the request) without a full reload.
- **Move-menu optimistic destination** — after `.move-to-list-option`, the task appears in the
  target list **immediately** (no ~2-3s gap), and the popover is **not clipped** for a task low in
  a tall group. Menu clipping is a *visual* property → confirm by screenshot / manual review, not
  only by request count.

### Dev view (goal 12; tabs + pagination in 12a)

Every `/dev/*` endpoint is behind the per-user **`dev` feature flag** (403 without it) and a
session cookie, so drive these as a flagged user. **Never file** against a real repo during
verification — `POST /dev/{id}/file` is a live GitHub write. Save/unsave/dismiss are local
flips and safe to exercise.

```bash
# View metadata ONLY (goal 12a): no draft array, plus the four tab counts.
curl -s http://localhost:8010/dev | jq .
# → {"last_scan_at": ..., "config_complete": true, "counts": {"review":N,"saved":N,"filed":N,"dismissed":N}}

# One lane at a time, newest activity first. status ∈ review|saved|filed|dismissed.
curl -s 'http://localhost:8010/dev/drafts?status=review&limit=5' | jq '.items | length, .next_cursor'
# Follow the opaque keyset cursor for the next page — no overlap, no gap, no drift.
curl -s 'http://localhost:8010/dev/drafts?status=filed&limit=5&cursor=<NEXT_CURSOR>' | jq '.items[].id'
# Bad inputs → 400 envelopes: status=nonsense (bad_status), cursor=garbage (bad_cursor).

# Local status flips — zero GitHub calls, idempotent, return the updated draft.
curl -s -X POST http://localhost:8010/dev/<ID>/save   | jq .status   # → "saved"
curl -s -X POST http://localhost:8010/dev/<ID>/unsave | jq .status   # → "draft"
curl -s -X POST http://localhost:8010/dev/<ID>/dismiss | jq .status  # → "dismissed"
```

Frontend selectors:
- `.dev-tabs` / `.dev-tab` (`role="tab"`, `.dev-tab--active` on the current lane) — the four
  lanes **In review · Saved for later · Filed · Dismissed**; `.dev-tab-count` is the badge.
- `.dev-list` / `.dev-card` — the active lane's cards only (one lane is mounted at a time).
  `.dev-card--saved` (tinted) / `.dev-card--filed` / `.dev-card--dismissed`.
- `.dev-save-btn` ("Save for later", review lane) / `.dev-unsave-btn` ("Move to review", saved
  and dismissed lanes) / `.dev-dismiss-btn` / `.dev-file-btn` / `.dev-retry-btn`.
- `.dev-scroll-sentinel` — the review lane's infinite-scroll trip wire (present only while its
  cursor is non-null); `.dev-load-older` — the settled lanes' explicit pager.

UI-flow checks (Playwright + the `mutations` request listener above):
- **Save for later moves the card and shifts two badges.** Click `.dev-save-btn` on a review
  card → exactly one `POST /dev/{id}/save`, **no GitHub-bound request**, the card leaves
  `.dev-list` immediately (no reload / no `GET /dev/drafts`), In-review's `.dev-tab-count`
  drops by one and Saved-for-later's rises by one.
- **The saved lane is actionable.** On the Saved tab, a card shows `.dev-file-btn`,
  `.dev-unsave-btn` and `.dev-dismiss-btn`, and its title input is editable (blur → one
  `PATCH /dev/{id}`).
- **In review drains by scroll.** With a backlog > 20, the first page paints immediately, then
  scrolling to the bottom appends pages (`GET /dev/drafts?status=review&cursor=…`, one per
  page) until `next_cursor` is null and `.dev-scroll-sentinel` disappears — ending with every
  pending draft in the DOM.
- **Settled lanes stay bounded.** Activating Filed/Dismissed fires exactly **one**
  `GET /dev/drafts` and renders ≤ 20 cards; scrolling to the bottom fires **none** — older rows
  appear only after clicking `.dev-load-older`.
- **Switching tabs reconciles the badges** — each `.dev-tab` click re-reads `GET /dev`.

## What to capture

- API responses: paste the JSON body inline in the report.
- Frontend: screenshot saved to `/tmp/` — include the path and describe what's visible.
- Network: method + URL + request body when verifying drag or group ops.
- Console errors: `page.on("console", ...)` — any `console.error` line is a finding.

## Teardown

Kill background processes after verification. The overlay DB (`backend/overlay.db`) is
gitignored and safe to leave in place — it accumulates rank/group data from test interactions.

## Goal 8: auth-gated endpoints + isolation

Every non-`/auth` endpoint now requires a **session cookie** — a bare `curl` gets 401. To exercise
an authenticated endpoint, either drive the real Google Sign-In flow in a browser (blocked: no
browser in the verifier sandbox) or, for endpoint tests, use the pytest suite's dependency-override
pattern (`app.dependency_overrides[get_current_user]`) — see `backend/tests/conftest.py`. The
**two-user isolation** headline check (a second user can't read/mutate the first's rows by id) is
covered by pytest, not the web verifier. Verification against a **deployed** instance is a distinct
mode: it can reach only public/authed surfaces with a real session and can't inspect the DB —
prefer the local pytest gate for isolation/tenancy assertions.
