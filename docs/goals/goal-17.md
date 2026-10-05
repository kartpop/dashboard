# Goal 17 — The dashboard owns tasks (Google Tasks retired as the source of truth)

**One line:** Tasks and task lists move into the dashboard's own database. Every read and write
of the Tasks panel, Threads, and the router goes to that database, never to the Google Tasks API.
Each user's existing Google Tasks are imported **once, automatically**. After that, Google Tasks
is left frozen as it was at cutover. A nightly read-only snapshot to a Google Doc follows in
goal 17a, and dropping the `tasks` OAuth scope follows in goal 17b.

## Why (the friction this closes)

- **The daily quota runs out.** The Google Tasks API has a per-project limit of about 50k queries
  a day, shared by every user. One day of use reached 54,478 requests and the dashboard started
  returning 429s.
  - Each `GET /tasks` calls `tasklists.list`, then pages through **every task ever completed**
    in every list (`showCompleted` + `showHidden`, 20 items per page by default). That cost grows
    every day.
  - The Tasks panel and the Threads panel each poll every 45s. Threads reconcile runs the same
    full fetch a second time.
  - Every open tab polls, including hidden ones, and now including a phone tab (goal 15).
- **The UI flickers.** A completed task disappears and then comes back a second later. The cause
  is that a poll response that started *before* the write landed overwrites the optimistic state.
  The long, multi-page fetch makes this window wide. The state is correct eventually, but the
  intermediate state is a mess.
- **Google Tasks is no longer used directly.** Since goal 15 made the dashboard usable on a
  phone, it is the only way tasks get added or changed. Nothing uses recurrence, reminders, or
  tasks shown in the Google Calendar or Gmail apps. Keeping Google as the source of truth costs
  quota, latency, and consistency bugs, and gives nothing back.

## Core model

- **The dashboard DB is the source of truth for tasks.** There is no two-way sync and no
  background pull from Google. After the import, the app makes **zero** Google Tasks API calls.
- **New tables, scoped by user like every other table:**
  - `task_list`: `id`, `user_id`, `title`, `position`, `created_at`, `updated_at`.
  - `task`, which absorbs the overlay:
    - `id`, `user_id`, `tasklist_id` (FK);
    - `title`, `notes`, `status` (`needsAction` | `completed`), `due` (a date),
      `completed_at`;
    - `position` (list order for unranked tasks: Google's order on import, and new tasks
      go on top), `rank`, `group_id` (FK `task_group`);
    - `created_at`, `updated_at`.
- **Ids are strings, and imported rows keep their Google ids.** A task or list imported
  from Google keeps the id it had there. Tasks and lists created afterwards get app-minted
  random ids. Because the ids carry over, `task_group.tasklist_id` and the thread steps'
  `tasklist_id`/`task_id` stay valid with no repointing.
- **`task_group` stays** unchanged; its `tasklist_id` values are already the right ids.
- **`task_overlay` becomes legacy.** The import copies its rank and group values onto `task`
  rows. The table is dropped in goal 17b.
- **Thread steps keep their `tasklist_id` and `task_id` columns**, which need no change.
- **The wire shape doesn't change.** `due` is still sent as `YYYY-MM-DDT00:00:00.000Z`, and the
  frontend's `Task` and `TaskList` types and URL shapes (`/tasks/{listId}/{taskId}`) stay the same.
- **Pinned lists:** "My Tasks" and "Follow-ups" are still found by title. Every user is
  guaranteed both rows, created empty if the import didn't bring them.
- **What is dropped:**
  - **Recurrence.** Imported recurring tasks become plain tasks.
  - **Subtask parent links.** They already render flat; `parent` is not stored.
  - **Hidden/cleared Google tasks.** They are not imported.

## What ships

### 1. Storage (Alembic)

- **One migration** creates `task_list` and `task`, and adds `user.tasks_imported_at`
  (nullable).
- **`task_overlay` is not dropped in this goal.** The importer reads it, and it is the rollback
  path. Its drop migration ships with goal 17b, once every user has `tasks_imported_at` set.
  Until then the app never reads it except during an import.
- **No data is copied inside Alembic.** The import needs Google API calls and per-user
  credentials, and a schema migration must not depend on the network.

### 2. One-time import (automatic, per user)

**Module:** `app/tasks_store/importer.py`, the **only** remaining caller of the Google Tasks
client.

**For one user:**
- Fetch every list and every task in one pass (`showCompleted`, `showHidden`,
  `maxResults=100`). Keep the open tasks plus those completed in the **last 30 days**; older
  completions are skipped. (`completedMin` is not used, because whether it also drops open
  tasks isn't documented clearly enough to trust.)
- Create the `task_list` and `task` rows with their Google ids, and copy rank and group from
  that user's `task_overlay` rows, matched on `(tasklist_id, task_id)`.
- Thread steps need no change, since the ids carry over. An open step whose task wasn't
  imported is removed by the next threads reconcile, which is the same outcome as "Deleted in
  Google" in goal 14a. The import report counts these steps.
- Make sure both pinned lists exist.
- Set `tasks_imported_at`.

**Guarantees:**
- **All or nothing:** each user's import is **one DB transaction**. A failure (a 429, an expired
  token, anything else) rolls back completely and leaves `tasks_imported_at` null.
- **Idempotent:** a user with `tasks_imported_at` set is skipped. A `--force` re-run first
  deletes that user's `task` and `task_list` rows.
- **No races:** imports are serialised in the process, so the startup sweep and a first-load
  import can't both copy the same user.

**Triggers. The owner does not need to do anything:**
1. **On startup**, a background task runs through every user with `tasks_imported_at IS NULL`,
   one user at a time. It reuses the scheduler pattern from `news/scheduler.py`, including the
   startup grace delay.
2. **On first load:** when `GET /tasks` sees a user who hasn't been imported, it runs that user's
   import inline before answering. This covers a user whose startup import failed.
   - If that inline import fails, the response is `503 tasks_import_pending`, and the panel
     shows "Importing your tasks…" and retries every 5 seconds.
   - The same `tasks_ready` gate guards every task endpoint and the threads endpoints that
     write tasks. For an imported user it costs nothing: no credentials are loaded and no
     Google call is made.
   - `GET /threads` doesn't import. For a user who hasn't been imported it **skips the
     reconcile**, since against an empty store every link would read as deleted, and serves
     the cached steps.
   - A router capture that becomes a task imports first. If that fails, the entry stays
     re-routable.
3. **Retry sweep:** the startup sweep also re-runs hourly, so a user whose token had a transient
   failure gets imported without having to log in.
4. **Escape hatch:** `uv run python -m app.tasks_store.importer --user <email> [--force]
   [--dry-run]`. With `--dry-run`, it prints the counts (lists, open, completed, overlay rows
   matched, thread steps repointed, thread steps unlinked) and writes nothing.

**After goal 17b**, the importer and the Tasks client are deleted. Any user signing up later
starts with two empty pinned lists.

### 3. Backend: read and write paths move to the DB

- **New `app/tasks_store/service.py`** owns every task and list query and mutation.
  - **`GET /tasks`** returns the same response shape as today: grouped view, buckets, groups,
    the Overdue rollup, `show_completed`. It comes from a single DB query, and the existing merge
    and bucketing code in `overlay/service.py` is kept, adapted to the new source.
  - **Every task endpoint keeps its path and body shape:** create, edit, complete/uncomplete,
    delete, reschedule, move, list rename, and the overlay and group routes.
  - **Move** becomes a single `UPDATE` of `tasklist_id`. **The task keeps its id.** The
    insert-then-delete dance, the new-id handling, and `threads.repoint_link` on move all become
    dead code. Delete them.
  - **The overlay PATCH** writes `task.rank` and `task.group_id` directly.
- **Router (goal 5):** `create_task` and `reschedule` call `tasks_store`. The write set is
  unchanged in kind. Only the target is local now.
- **Threads:**
  - `reconcile` becomes a DB join. The Google fetch, the "serving cache" fallback, and
    `get_tasklist_refs` go away.
  - Reconcile is a cheap local join on every `GET /threads`, so a completed linked task flips
    its step on the very next fetch.
  - Creating a next step creates a local task.
  - `repoint_link` is deleted, since a move keeps the id. `update_step`'s list switch updates
    the step's `tasklist_id` itself.
- **Deleted:**
  - every non-importer use of `app/google/tasks.py`;
  - the Tasks parts of `writes/service.py` (the Docs `append_note` stays);
  - the Tasks write tests that went through a fake Google layer. They are rewritten against the
    DB.

### 4. Frontend

- **Polling stays at 45s**, so a phone and a laptop still see each other's changes. It is now
  cheap, because it reads the DB.
- **Pause polling while the tab is hidden** (`document.visibilityState`), and refetch once when
  the tab becomes visible again. This applies to the Tasks, Threads, and Scratch panels.
- **Discard stale poll responses.** `api.ts` tracks every write (PATCH, POST, PUT, DELETE).
  Polls use `apiPollGet`, which drops its result if a write was in flight when the poll
  started, or ran while it was out. This fixes the flicker for good, independent of the
  backend change.
- **Move:** the response keeps `new_task_id`, which now always equals the request id, so the
  frontend's move handling needs no change.
- **Copy:**
  - The archive toast in Threads ("Its N open tasks stay in Google Tasks") becomes "Its N open
    tasks stay in your lists".
  - Any "Google Tasks" wording in empty states and errors becomes "your tasks".

## Locked decisions

- **One-way and final.** After the import, Google Tasks is never read or written by the app.
  Edits made in the Google Tasks app after cutover are **not** picked up. Google Tasks stays
  frozen as it was at cutover, which also makes it a natural point-in-time rollback copy.
- **Completed tasks are imported for 30 days only**, matching the goal-17a prune window.
- **No recurrence and no reminders.** If a need comes up, it's a new goal against the local model.
- **String ids, with Google's ids kept on import.** No repointing and no frontend type churn.
- **`task_overlay` is dropped in goal 17b, not here.** That keeps a rollback path to the
  pre-17 code for as long as any user hasn't been imported.

## Out of scope (do not build)

- **The nightly Google Doc snapshot, pruning, and "Export now".** That is goal 17a.
- **Removing the `tasks` scope and deleting the importer.** That is goal 17b.
- **Any sync back to Google Tasks**, in either direction.
- **Recurrence, reminders, notifications.**
- **Changes to Calendar or Drive.** Calendar reads remain direct Google API calls.

## Acceptance criteria

**Importer unit tests** (fake Google layer, two users):
- **Content:** lists, open tasks, and completed tasks within 30 days are imported. Hidden tasks
  and tasks completed more than 30 days ago are not.
- **Overlay carry-over:** rank and group are carried over (group ids stay valid, because list ids carry over).
- **Thread steps:** links survive as they are, since ids carry over. Open steps whose task
  wasn't imported are counted in the report and removed by the next reconcile.
- **Pinned lists:** both are created if absent.
- **Failure:** a mid-import exception rolls back completely, and `tasks_imported_at` stays null.
- **Idempotency:** a second run is a no-op. `--force` produces the same result as the first run.
- **`--dry-run`** writes nothing.
- **Isolation:** user A's import never reads or writes user B's rows.

**Triggers:** the startup sweep imports every pending user. A user whose import failed gets
imported on their first `GET /tasks`. While pending, the panel shows the importing state and
retries on its own.

**No Google Tasks calls at steady state:** an AST/import test pins `app.google.tasks` to exactly
one importer (`tasks_store/importer.py`). Every other module importing it fails the test.

**API parity:** the existing tasks, overlay, group, and threads API tests pass against the DB
backend with unchanged request and response shapes. The one exception is move, where
`new_task_id` now equals the request id.

**UI (Playwright via `/verify`):**
1. Complete a task. It disappears and **stays gone** across the next poll. Repeat it 5 times in
   quick succession, and nothing reappears.
2. Undo restores the task.
3. A cross-list drag keeps the task's thread badge.
4. Completing a thread's linked task flips the thread on the next `GET /threads`.
5. A hidden tab stops polling, and showing it again refetches once.
6. **Mobile tabs (goal 15)** behave the same.

**Regression:** `tsc`, the frontend build, `ruff`, and all backend tests pass. `alembic upgrade
head` applies cleanly on SQLite (prod runs SQLite too).

## Harness upkeep

- **`.claude/rules/writes.md`:** Tasks writes are local DB writes. Remove "insert-before-delete on
  move" and the two-callers rule for `delete_task`. The Docs write rules stay.
- **`.claude/rules/tasks-panel.md`:** describe the stale-poll guard and the hidden-tab pause.
- **`.claude/rules/threads.md`:** reconcile is a join; the Google-unavailable fallback is gone.
- **`.claude/rules/router.md`:** `create_task` and `reschedule` now target the local store.
- **Skills:**
  - `verifier-writes` is retired, since no Google task writes are left to exercise safely. Delete
    it.
  - `google-api-integration` drops Tasks from its examples.
  - `verifier-web` drops Google Tasks setup assumptions.
- **`CLAUDE.md`:** the Project and Stack lines say tasks live in the app DB. The "read paths call
  the Google API client directly" constraint now names Calendar and Drive only.
- **`docs/goals/README.md`:**
  - add the goal-17 line;
  - the spanning constraint "Google writes begin at goal 4" gets a goal-17 amendment: Tasks
    writes are local from goal 17.
- **Owner steps:** `goal-17-owner-steps.md`, covering the deploy window and the backup.
