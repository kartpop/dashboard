# API reference & feature details

The HTTP surface of the backend, grouped by the milestone that introduced it. For setup and run
steps see the root [README](../README.md); for milestone history see [goals/](goals/).

## Auth & multi-tenancy (goal 8)

Google Sign-In is the OAuth grant. **Every non-auth endpoint requires a session** and is scoped to
the signed-in user; an unauthenticated call returns **401** in the standard error envelope.

- `GET /auth/login` — 302 to Google consent (identity + tasks + calendar.readonly + drive.file,
  `access_type=offline`).
- `GET /auth/callback?code=&state=` — verifies the ID token, enforces the email allowlist, upserts
  the `user` row (refresh token stored **Fernet-encrypted**), sets the session cookie, 302s to the
  app. A non-allowlisted account → 302 to `?auth_error=not_invited` (no row, no token).
- `POST /auth/logout` — clears the session. `GET /auth/me` — `{id, email, name, picture,
  is_superuser}` or 401.
- Sessions are a signed `HttpOnly`/`Secure`/`SameSite=Lax` cookie (Starlette `SessionMiddleware`).

### Settings (`user_settings`, replaces env vars)

- `GET /settings` — `{calendars:[{id, summary, primary, background_color, enabled}],
  enabled_calendar_ids, notes_folder_id, notes_doc_id, notes_folder_url, notes_doc_url}`.
  `calendars` comes from `calendarList.list`; visiting also **bootstraps** the user's notes
  folder + Doc if absent.
- `PUT /settings/calendars` — `{calendar_ids:[...]}` sets the extra (non-primary) calendars merged
  into the day strip; free-text ids not in `calendarList` are accepted (add-by-id).
- `GET|POST /settings/allowed-emails`, `DELETE /settings/allowed-emails/{email}` — **superuser
  only** (403 otherwise). `POST {email}` invites; `DELETE` blocks future sign-ins (the superuser's
  own email can't be removed).

## Tasks: reads & overlay

Reads are `GET /tasks` (all lists, merged with the local overlay), plus overlay PATCH and group
CRUD. Rank and grouping are **overlay-only** — they never sync to Google.

## Calendar: reads (goal 7b)

Read-only; the calendar surface has **zero** write capability.

- `GET /calendar/day?date=YYYY-MM-DD` — one IST calendar day's events for the header strip.
  `date` is optional (default = today, IST); an invalid value → **400** in the standard error
  envelope. Returns `{date, events}` where each event is
  `{id, title, start, end, all_day, meet_link, location, organizer, my_response,
  attendees:[{name, email, response_status}]}`.
  `meet_link` is `hangoutLink`, falling back to the `video` `conferenceData` entry point, else
  `null`. `my_response` is the owner's own RSVP (`responseStatus` of the `self: true` attendee;
  `null` for events with no owner attendee entry — the strip treats that as accepted).
  `organizer` is the organizer's display name, falling back to email.
- The day window merges `primary` **plus the user's toggled-on calendars** (goal 8:
  `user_settings.enabled_calendar_ids`, set via `PUT /settings/calendars`; replaces the old
  `EXTRA_CALENDAR_IDS` env var), deduped by `iCalUID` (the primary copy wins for an
  invited-attendee duplicate) and sorted by start. Extra calendars are **best-effort**: a failing
  one logs a warning and the rest still return; a primary failure is a `502`. Calendar IDs are
  **config-only** — never from LLM output or request payloads (same hygiene rule as the Docs IDs).

## Write endpoints

Google writes cover task metadata, task content, and list rename — rank/grouping stay overlay-only:

- `POST /tasks/{list}/{task}/reschedule` `{due_date, rank?, group_id?}` — set/clear the Google due
  date (cross-bucket drag **or** the per-task date-picker); `due_date` is `YYYY-MM-DD` (IST) or
  `null` for no date.
- `POST /tasks/{list}/{task}/move` `{target_list_id, rank?, due_date?, group_id?}` — move to another
  list (insert-then-delete; the overlay row migrates to the new task id). Cross-list drag (goal 6)
  may also reschedule + regroup in the same write: an **omitted** `due_date` preserves the source
  due, an explicit value (or `null` → `NO_DATE`) sets it on the insert leg; `group_id` names a
  destination group (validated against the dest bucket, else 422).
- `POST /tasks/{list}` `{title, rank?}` — create a task (lands undated → `NO_DATE`, top).
- `PATCH /tasks/{list}/{task}` `{title?, notes?, status?}` — edit content; `status`
  `completed`/`needsAction` is complete/uncomplete. Only the fields sent are written.
- `DELETE /tasks/{list}/{task}` — delete a task (the UI defers this behind a ~5s undo toast).
- `PATCH /lists/{list}` `{title}` — rename a task list.

The tasks panel is a daily-driver MVP: create / edit / complete / delete, an arbitrary-date picker,
an Overdue rollup at the top of each list, and a per-panel refresh.

## Threads (goal 14, 14a)

A thread is an ordered list of steps: **done** steps are local history, followed by any number of
open **next** steps (goal 14a), each linked to a real Google Task in `My Tasks` (my move) or
`Follow-ups` (their move). Google is the source of truth for a next step's title / notes / due.
`steps` is served done steps first (history order), then the open steps by due ascending
(undated last). Every call is scoped to the
signed-in user; every mutation returns the updated `Thread`.

- `GET /threads` — `{threads: Thread[]}`, active and archived, **after a reconcile** against
  Google: a completed linked task flips that next step to done (dated by the completion, IST) and
  moves it to the end of the done history; a done step whose task is open again becomes next again
  (unless another step links that task); a deleted task removes that next step; an open task refreshes the cached label/note/due. A
  failed Google fetch serves the cached state.
- `POST /threads` `{title}` — create (no steps). `PATCH /threads/{id}` `{title?, archived?}` —
  rename / archive / restore; archiving never touches the linked task.
- `POST /threads/{id}/steps` `{label, note?, occurred_on?}` — log a done step, inserted **before**
  the open steps; `occurred_on` defaults to today (IST).
- `POST /threads/{id}/next` `{label, list: "mine"|"follow", due?, note?}` — create the Google task
  in that pinned list, then link it as one more open step. **422** if the pinned list is missing.
- `PATCH /threads/{id}/steps/{sid}` `{label?, note?, occurred_on?, due?, list?}` — a done step is a
  local edit; a next step writes through to Google (label/note → content, due → reschedule, list →
  move, which repoints the link). Unchanged fields are skipped.
- `POST /threads/{id}/steps/{sid}/complete` — next step only: complete its task, flip that step
  (its siblings stay open).
- `DELETE /threads/{id}/steps/{sid}` — delete a done step, or **unlink** a next step (the Google
  task stays in its list). Returns the thread plus `unlinked: bool`.

Shapes: `Thread = {id, title, archived, created_at, last_moved_on, steps}` (`last_moved_on` = the
latest done date, else the creation date); `Step = {id, kind, label, note, occurred_on, list, due,
tasklist_id, task_id, via}` — `list` is set on next steps, `via` on a done step that came from a
completed task. A dashboard move (`POST /tasks/{list}/{task}/move`) repoints any linked step to the
new task id; a move made outside the dashboard reads as a deletion (v0).

## Scratchpad + auto-router

An append-only capture box files a dumped thought to the right place. A captured entry is run
through the **only runtime LLM in the system** (`app.router.classifier`) — a small/cheap model
(`claude-haiku-4-5`) that returns a schema-validated `{destination, confidence, fields}`.
**Deterministic code does every write** (`app.router.service`); the LLM never writes. The router's
entire Google-write surface is **insert-only** — `{create_task, reschedule, append_note}` — never a
delete, status write, content edit, or Docs overwrite.

The **capture box is a bullet pad** (goal 7): `- ` starts a bullet, Enter continues it at the same
indent (Enter on an empty bullet exits the list), Tab / Shift+Tab indent/outdent, and **Shift+Enter
(or Cmd/Ctrl+Enter) captures the whole editor as one entry** and clears it. It's a plain `<textarea>`
— the value is captured verbatim (bullets as literal text).

**Deferred capture (goal 7a):** Shift+Enter clears the editor immediately but **holds the
`POST /scratch` for ~5s** behind a "Captured — Undo" toast. **Undo restores the text with zero
backend writes** (undo-by-never-sending — the append-only store is untouched, no delete endpoint
exists); letting the window lapse fires exactly one POST. This is entirely client-side — the
endpoint below is unchanged. (Known: **Route now** clicked inside the window won't include the
still-held capture.)

- `POST /scratch` `{text}` — append a capture (append-only; never edits/deletes prior entries).
- `GET /scratch` — recent entries with their routing state (`unrouted` / `routed_task` /
  `kept_note` / `in_review` / `resolved`). The scratchpad's **RECENT** list (goal 7a) filters this
  client-side: unresolved entries (`unrouted` + `in_review`) first, then only the ~5 most-recent
  routed/resolved as a dimmed tail.
- `POST /scratch/route-now` — route every unrouted entry now (same code path as the scheduled job;
  idempotent — route-once).
- `GET /review` — pending review items (low-confidence / `event` / `unknown` — calendar is
  read-only v1, so events go to review for a manual add).
- `POST /review/{id}/confirm` `{destination?, fields?}` — confirm (edit-then-confirm); a `task`
  fires exactly one `create_task`, a `note` fires exactly one `append_note`.
  `POST /review/{id}/dismiss` — writes nothing.

**Notes → Google Doc (goal 7).** A high-confidence `note` is appended **verbatim** to the top of one
configured Doc (`NOTES_DOC_ID`) under a Heading-3 timestamp (`6-July-2026, 8:41 PM IST`), newest
first — insert-only. Each note ends with a **light-gray `borderBottom` delimiter paragraph** with
spacing above/below (goal 7a — the Docs API has no horizontal-rule request), so consecutive notes
read as separated entries. Unset `NOTES_DOC_ID` → the note stays kept-local with a logged warning. Drive
access is **`drive.file`-scoped** with a folder-ancestry gate + startup scope assertion (ADR
`docs/goals/architecture/drive-access-scoping.md`); doc/folder IDs are config-only. Setup:
[goals/goal-7-owner-steps.md](goals/goal-7-owner-steps.md). Bootstrap the Doc with
`uv run python -m app.google.bootstrap`.

A periodic in-process job auto-routes unrouted entries (no Celery). Disable with
`ROUTER_SCHEDULER_ENABLED=0`; tune the cadence with `ROUTER_SCHEDULER_INTERVAL` (seconds).

> **Set `ANTHROPIC_API_KEY`** for the router to classify. Without it, classification fails closed —
> every entry routes to `unknown` → the review queue (no Google writes). Override the model with
> `ROUTER_MODEL` and the auto-write confidence gate with `ROUTER_CONFIDENCE_THRESHOLD`.

### Evaluating the router

The router's quality is *measured, not asserted* — a labelled set gates the goal:

```sh
cd backend && uv run python -m app.router.evals.runner          # scorecard + GATE: PASS/FAIL
```

Or via the `/eval` command, which fans the cases across cheap-model `eval-worker` subagents and
aggregates (see `.claude/skills/eval-runner/SKILL.md`).
