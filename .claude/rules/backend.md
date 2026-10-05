---
paths: ["backend/**"]
---

# Backend conventions (FastAPI)

- Handlers live under `backend/app/routers/`, one router module per resource; wire them up in
  `backend/app/main.py`.
- Error responses are JSON: `{"error": {"code": "<machine_code>", "message": "<human message>"}}`.
  Raise `app.errors.ApiError(status_code, code, message)`; the registered `StarletteHTTPException`
  handler in `app/main.py` shapes it into that envelope — never hand-roll an error `JSONResponse`
  or return bare strings/stack traces.
- Google API clients (Calendar, Drive/Docs, and the import-only Tasks client) live in
  `backend/app/google/`, one module per service, and are the only place that calls the Google APIs
  directly (see CLAUDE.md hard constraint: no MCP/LLM in read paths). See
  `.claude/skills/google-api-integration/SKILL.md` for the credentials/pagination/async conventions
  those modules follow. These modules do fetch+reshape only — no sorting, grouping, or merging.
  **Goal 17:** tasks are not a Google read path any more — `app/google/tasks.py` has only
  `fetch_for_import`, imported solely by `app/tasks_store/importer.py` (AST-pinned).
- **Tasks live in the app DB (goal 17)** — `app/tasks_store/`: `models.py` (`TaskList` / `Task`,
  string ids; `rank` / `group_id` are columns on `Task`), `service.py` (the **only** module that
  queries or mutates task rows), `importer.py` (one-time per-user Google import), `deps.py`
  (`tasks_ready` dependency / `ensure_ready` — imports a not-yet-imported user inline, else
  `503 tasks_import_pending`), `scheduler.py` (startup + hourly import sweep).
- Sort, bucket, and group logic lives exclusively in `backend/app/overlay/service.py`. Routers
  call `overlay_svc.get_merged_task_lists(session, user.id, store.get_task_lists(...))` and stay
  thin. Do not put sort/group logic in routers, in `app/tasks_store/*`, or in `app/google/*`.
- **API shape (goal 3+):** `GET /tasks?view=grouped` returns `task_lists[].buckets[].items[]`.
  Key is `buckets` (not `groups`). Each item has `type: "task"` or `type: "group"` (groups embed
  their `items`). No `priority` field. Bucket key is `YYYY-MM-DD` (IST) or the sentinel `NO_DATE`.
- **Overlay service owns group CRUD** (`create_group`, `update_group`, `delete_group`). Group
  scope = `(tasklist_id, bucket_key)`. Task `group_id` is nullable; pass `group_id=None` to
  `upsert_overlay` to explicitly ungroup (it updates the `Task` row; a missing task → `None` →
  the router answers 404).
- The persistence layer (`backend/app/overlay/`, `backend/app/tasks_store/`, ...) uses SQLModel +
  Alembic. Run `alembic upgrade head` from `backend/` before starting the server. The `overlay.db`
  SQLite file lives in `backend/` and is gitignored.
- Use `async def` for all route handlers and I/O (DB, HTTP); wrap blocking calls (e.g. the
  Google API client's `.execute()`) in `asyncio.to_thread(...)` — see the private sync
  `_fetch_*` / public async `get_*` split in `app/google/calendar.py`.

## Auth + multi-tenancy (goal 8) — HARD RULES

- **Every user-owned query filters by `current_user.id`.** Not optional. Handlers take
  `user: User = Depends(get_current_user)` (from `app/auth/deps.py`) and pass `user.id` into the
  service; services filter every `select(...)` / `session.get(...)` by `user_id`. Never trust a
  `user_id` from a request path/body. `Task` / `TaskList` have string-id PKs plus a `user_id`
  column — a lookup by id must check ownership (`tasks_store.service.get_task` does
  `session.get(Task, task_id)` then returns `None` unless `task.user_id == user_id` and the
  `tasklist_id` matches); list queries filter `Task.user_id == user_id`. New user-owned tables get a
  `user_id` FK to `user.id`. The headline test is two-user isolation: a second user must never
  read or mutate the first's rows by id.
- **Google credentials are per-user and passed explicitly.** Every `app/google/*` function takes a
  live `creds: Credentials` as its FIRST arg; handlers get it from
  `creds: Credentials = Depends(get_current_credentials)` and thread it through the service into the
  client. There is **no** global credential load anymore. `app/google/auth.py`
  `load_credentials(session, user)` builds it from the user's Fernet-encrypted refresh token and
  runs the **per-token** scope assertion (a broader-than-allowlist grant → `ScopeError` → 403).
- **Sessions:** Starlette `SessionMiddleware` (signed `HttpOnly`/`Secure`/`SameSite=Lax` cookie).
  `get_current_user` reads `request.session["user_id"]`; unauthenticated → 401. `require_superuser`
  gates the allowed-email admin (403 otherwise).
- **Per-user config replaced env vars.** `app/settings/service.py` owns `user_settings`: calendar
  toggle ids (`enabled_calendar_ids`, JSON) and the app-created notes folder/Doc ids
  (`ensure_notes_target` bootstraps them on first need). `NOTES_*` / `EXTRA_CALENDAR_IDS` and
  `app.google.bootstrap` are gone — never reintroduce them.
