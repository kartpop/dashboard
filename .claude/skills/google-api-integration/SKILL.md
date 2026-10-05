---
name: google-api-integration
description: Conventions for adding a new read-only Google API client module under backend/app/google/ (Calendar, Drive, Docs, ...). Use when wiring up a new Google service or extending an existing one.
---

# Google API integration conventions

Patterns that repeated across the `app/google/` client modules (`calendar.py`, `docs.py`).
Follow them when adding a new service module (e.g. `drive.py`).

**Tasks is not a live read path (goal 17).** Tasks live in the app DB (`app/tasks_store/`);
`app/google/tasks.py` is **import-only** — just `fetch_for_import`, imported solely by
`app/tasks_store/importer.py` (AST-pinned; deleted in goal 17b). Never add a new Google Tasks call
or use it as a template for a read path.

## Credentials (per-user, goal 8)

Credentials are **per-user and passed explicitly** — there is no global token file anymore. Every
`app/google/*` function takes a live `creds: Credentials` as its **first argument**
(`_service(creds)` → `build(..., credentials=creds)`). Handlers obtain it from
`creds: Credentials = Depends(get_current_credentials)` (`app/auth/deps.py`) and thread it through
the service into the client; the scheduler backstop loads each user's creds per tick. So a new
service module's functions look like `async def get_thing(creds, ...)`, and its callers already hold
`creds`.

`app.google.auth.load_credentials(session, user)` builds the live credentials from the user's
Fernet-encrypted refresh token (from the `user` row), refreshes to mint an access token, re-persists
a rotated refresh token, and runs the **per-token** scope assertion (a grant broader than
`auth.ALLOWED_SCOPES` raises `ScopeError`). Sign-in is a **web OAuth flow** (`build_flow` /
`authorization_url` / `exchange_code` / `verify_id_token`) — never `InstalledAppFlow`, never a
consent flow from request-handling code.

If a new service needs a scope not in `auth.SCOPES`, add it there (and to the consent screen) — but
**never** `documents`/`drive` (ADR: drive-access-scoping; `drive.file` only). Existing users re-grant
by signing out and back in.

For a calendar-style "list what the account can see" fetch, see `calendar.get_calendar_list`
(`calendarList.list`, within `calendar.readonly`) — the settings toggle's source.

## Sync client, async boundary

`googleapiclient.discovery.build(...)` and `.execute()` are blocking. Keep a private sync
`_fetch_*` function that does the Google call, and expose an `async def get_*` wrapper that runs
it via `asyncio.to_thread(...)`. Routers only ever call the async wrapper.

## Pagination

Use `app.google._paging.list_all(resource, **list_kwargs)` to walk `nextPageToken` and collect
every `items` page into one list (today only the Tasks importer uses it; it will be needed again
for `files().list()` (Drive)).

## Error normalization

Routers wrap any exception from a `get_*` call in `app.errors.ApiError(status_code, code, message)`,
which shapes the response as `{"error": {"code": ..., "message": ...}}` per the backend
convention. Pick a `code` that names the failing service, e.g. `google_calendar_unavailable`.

## Shaping responses

Don't forward raw Google API payloads. Map to a small, stable dict (id/title/status/etc.) in the
`_fetch_*` function — this keeps the frontend decoupled from upstream field churn and keeps
endpoints "dumb" (fetch + reshape, no ranking or merging).

## Docs / Drive writes (goal 7 — `app/google/docs.py`)

The notes writer added a Docs/Drive client. Conventions specific to it:

- **Scope is `drive.file` only** — never `documents`/`drive` (ADR
  `docs/goals/architecture/drive-access-scoping.md`). Google enforces the token can touch only files
  the app *created*, so there is exactly one create path (`create_doc_in_folder`, hard-coding
  `parents=[folder_id]`), used only by the `app.google.bootstrap` command.
- **Docs edits go through `documents.batchUpdate`** with a list of request dicts applied in order
  (later requests see earlier edits' index shifts). Inserting at the **top of the body** = index `1`;
  build the block as one `insertText`, then `updateParagraphStyle` ranges over it (heading vs body).
  Indices are UTF-16 offsets — plain ASCII/newlines count as 1.
- **Insert-only body writes.** This client never calls `files.delete` or a content-overwriting
  `files.update`; the AST guardrail test (`test_docs_module_write_surface_is_insert_only`) pins that
  surface. Read a file's parents for the folder-ancestry gate via `files.get(fields="parents")`.
- **Goal 9 additions (still within `drive.file`):** `create_folder` takes an optional `parent_id`
  (`parents=[parent_id]` when nesting a hierarchy folder, else Drive root) — still `files.create`.
  `rename_file` is the one **metadata-only** `files.update`, body **exactly `{"name": ...}`** (never
  content/parents/trashed), **settings-path-only** — the AST test now allows `files().update` but
  pins it to `_rename_file`, and a unit test pins the body. The `insert_note` entry block grew to
  H3 → H4 → H5 → body → delimiter but is still a single top-insert `batchUpdate`.
- Same sync `_fn` / `async def` + `asyncio.to_thread` split as the other client modules.

## Calendar day window + multi-calendar merge (goal 7b — `app/google/calendar.py`)

The header-strip day fetch is the first client that queries **more than one calendar** and does an
in-module merge (a deliberate, narrow exception to "no merging in `app/google/*`" — it's a
same-shape union, not overlay logic):

- **IST day window:** `_ist_day_bounds(day)` returns `timeMin`/`timeMax` as that IST day's midnight
  → next midnight (`+05:30` offsets); fetch with `singleEvents=True, orderBy="startTime"`. Keep the
  bounds a pure helper so a unit test can pin it for an arbitrary date.
- **Meet link:** `_extract_meet_link` = `hangoutLink`, else the `video` entry in
  `conferenceData.entryPoints`, else `None` (all-day/no-conference events → `None`).
- **Multi-calendar:** query `primary` **plus** every id in `EXTRA_CALENDAR_IDS` (comma-separated
  env, config-only — never LLM/request input). `_merge_events` dedupes by `iCalUID` (first list
  wins → pass primary first so an invited-attendee duplicate keeps the primary copy) and sorts by
  start. Extras are **best-effort** (per-calendar try/except → logged warning); a `primary` failure
  propagates as the `502`.
