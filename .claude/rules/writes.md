---
paths: ["backend/app/writes/**", "backend/app/tasks_store/**", "backend/app/google/tasks.py", "backend/app/google/docs.py", "backend/app/google/bootstrap.py", "backend/app/threads/**"]
---

# Write safety (goal 4+; task writes local since goal 17)

Google writes began in goal 4. **Since goal 17 task writes are local DB writes** — tasks and lists
live in the app's own store (`app/tasks_store/`), and no Google Tasks call is reachable from the
write layer. The remaining Google writes are the Docs/Drive notes surface (below), which are the
only mutations that leave the local DB — treat them with more care. Read this before editing the
write layer.

## Layering

- `app/tasks_store/service.py` is the **only module that queries or mutates task rows** (`Task` /
  `TaskList`, string ids, every function scoped by `user_id`). Imported rows keep their Google ids;
  rows created afterwards get an app-minted `secrets.token_urlsafe` id (`new_id()`). It hands back
  the same dict shape the old Google reshape produced, so the overlay merge and frontend are
  unchanged.
- `app/google/tasks.py` is **import-only** now: just `fetch_for_import`, called only by
  `app/tasks_store/importer.py` (the one-time per-user Google import). An AST test
  (`tests/test_tasks_import.py::test_only_the_importer_imports_google_tasks`) pins the importer as
  its only importer.
- `app/writes/service.py` owns **orchestration**: it validates inputs, applies the bucket rules,
  and decides what (if anything) to write: `reschedule`, `move` (g4); `create_task`,
  `update_content`, `delete`, `rename_list` (g4a); `append_note` (g7). The task functions take
  **no `creds`** — they write the local store via `tasks_store.service`. Routers stay thin and call
  the writes service; task endpoints depend on `tasks_store.deps.tasks_ready` (imports a
  not-yet-imported user inline, else `503 tasks_import_pending`).
- `app/google/docs.py` (g7) is the **thin Docs/Drive client** — one Google call each:
  `insert_note` (Docs `documents.batchUpdate`, insert-only — H3 heading + verbatim body + a trailing
  empty paragraph styled as a light-gray `borderBottom` delimiter (g7a); still ONE `batchUpdate`, no
  new method surface, so the AST insert-only test is unchanged), `get_parents` (Drive `files.get`,
  the ancestry gate's read), and `create_doc_in_folder` (Drive `files.create`, the **only**
  sanctioned file-create, used by the `app.google.bootstrap` command). It **never** calls
  `files.delete` or a content-overwriting `files.update` — the AST test pins that surface.

## The task fields that may be written (goal 4a; local store since goal 17)

- **Task metadata** (g4): due date, list membership (`tasklist_id`).
- **Task content** (g4a): title, notes, and `status` (complete/uncomplete rides `status`;
  `store.set_status` keeps `completed_at` in step).
- **Tasklist** (g4a): list title (`rename_list`).
- **Rank and group** are columns on `Task` (the overlay folded in); the overlay PATCH writes them
  via `overlay_svc.upsert_overlay`.
- **Never to Google:** nothing about tasks is written to Google any more.

Each content/status edit is optimistic with a pre-op snapshot; on failure → rollback + error
toast (never swallowed). Same-value title/notes/status is a no-op skipped client-side.
`app.writes.service._UNSET` is the "field not sent" sentinel: only fields explicitly provided are
written.

## Invariants

- **Idempotent.** A reschedule just sets the task's `due` / `rank` / `group_id` columns, so a
  same-bucket reschedule is naturally a no-op on the due. A move to the current list is rejected
  (400) — the client blocks it too.
- **Move is an in-place update (goal 17).** A cross-list move is one `UPDATE` of the task row's
  `tasklist_id` (plus `position` = top of the target list, `due`, `rank`, `group_id`). **The task
  keeps its id** — the response still carries `new_task_id`, always equal to the request's task id,
  so clients need no change. No insert-then-delete, no id repointing (thread links and groups stay
  valid). An unknown target list → 404 `list_not_found`.
- **`move` may reschedule in the same write (goal 6).** `move` takes an optional `due_date` and
  `group_id` so a cross-list drag that also changes the date bucket / lands in a group is **one
  write**, not two chained calls that can half-fail. Semantics:
  - `due_date is _UNSET` (omitted by the router when the request has no `due_date` key) → the task
    **keeps its due**; an explicit value sets it (`None` → `NO_DATE`).
  - `group_id` (when not None) must reference a group in the **destination** `(target_list, bucket)`
    where `bucket = due_date or NO_DATE` if provided, else the task's current bucket — else
    **422**, raised *before* any write. It is set on the task row (None = ungrouped). Menu-move
    callers pass neither.
- **Task delete is user-only.** `writes.service.delete` is called only by the user `DELETE
  /tasks/{list}/{task}` endpoint. **The goal-5 auto-router never deletes** — its write path is
  create-only (next bullet). **Neither does threads (goal 14)** — unlinking a next step or archiving
  a thread leaves the task in its list.
- **`create_task` has TWO sanctioned callers (goal 5):** (1) the **user create endpoint**
  (`POST /tasks/{list}`); (2) the **auto-router** (`app.router.service`), which creates a task from a
  routed capture. The router's *entire* task-write surface is **`create_task` + `reschedule`** (the
  g4a date path, to set the new task's due date) — both create/metadata, nothing destructive; local
  store writes since goal 17. Routing may **never** call `delete`, the complete/uncomplete `status`
  write, or `update_content`. This create-only contract lives in `.claude/rules/router.md` and is
  asserted by a router write-path test (the router's write dependency set is exactly
  `{create_task, reschedule, append_note}` from g7).
- **Threads is the third `create_task` caller (goal 14)** — so the callers are: user create
  endpoint, auto-router, threads (`app.threads.service.set_next`, which creates a thread's next step
  in a pinned list). Threads also calls `update_content` (next-step label/note, and completion via
  `status`), `reschedule` (next-step due) and `move` (switching My Tasks ↔ Follow-ups). Its write
  dependency set is exactly **`{create_task, update_content, reschedule, move}`**, AST-pinned in
  `tests/test_threads.py`; it never references `writes_svc.delete` or `app.google.tasks`. Task
  write first (committed), step write second in the same request — an orphan task is the accepted
  failure mode. Rules: `.claude/rules/threads.md`.
- **`append_note` is a router-only caller (goal 7).** `writes.service.append_note(doc_id, folder_id,
  body_text, summary=None)` is the notes writer: it appends a captured note **insert-only** to the top
  of the configured Doc under an H3 timestamp (`format_note_heading`). Its **only** caller is
  `app.router.service` (the high-confidence `note` path + confirm-as-note in review). It is
  **insert-only forever** — never a Docs delete, never a content overwrite, never a status/content
  task write. The task-write caller rules above stand unchanged; `append_note` adds a *new*
  surface, it doesn't widen the task-write callers.
  **Goal 7c:** the Doc entry gains **one LLM-authored line** — the classifier's `summary` one-liner,
  rendered **bold** between the timestamp and the **still-verbatim** raw text (`insert_note`'s
  `summary_text` arg). The raw body stays verbatim; the summary is the *only* generated line; an
  empty/missing summary degrades to the goal-7 shape. Write set + insert-only unchanged (still one
  `documents.batchUpdate`, no new method surface — the AST test is unaffected). Confirm-as-note in
  review may pass an edited body + summary (review edits win); the auto-route path passes
  `fields.summary`.
- **Folder-ancestry gate + fail-closed (goal 7).** Before any `batchUpdate`, `append_note` verifies
  the target doc's `parents` chain reaches `NOTES_FOLDER_ID` (`_assert_in_notes_folder`, cached per
  doc id). **Fail-closed:** a missing folder id, an unreachable doc, or any error verifying ancestry
  → raise `ApiError`, do **not** write (the router leaves the entry re-routable; route-once marks it
  routed only after a successful append). Doc/folder IDs are **config-only** (`NOTES_DOC_ID` /
  `NOTES_FOLDER_ID` env), never from LLM output or request payloads.
- **Completion writes immediately; delete defers (g4a).** Completion (`status` patch) is
  non-destructive — the store keeps completed tasks and uncomplete is cheap — so the write fires
  now; the undo-toast is mis-click recovery. **Delete is the only genuinely irreversible op**, so
  the deferral lives entirely in the **frontend**: the optimistic remove + ~5s undo-toast hold the
  `DELETE` until the window closes; Undo cancels it with **zero writes** (the backend `delete`
  endpoint is simply never called). The backend `delete` is immediate when invoked.
- **Rollback, not retry.** On any failure, raise a clean `ApiError`; do not retry in a loop. The
  frontend owns rollback (snapshot restore + toast). Never swallow a task-write or Docs-write
  error — unlike overlay (rank/group) PATCHes, these are not fire-and-forget.
- **Group scope on reschedule.** A `group_id` passed to reschedule must reference a group in the
  **destination** bucket `(tasklist_id, target_bucket_key)`; otherwise 422. A cross-bucket move
  touches only the dragged task's row (`group_id` → destination group or NULL); source group
  siblings are never modified.
- **Rank/group ride the task row on move.** The moved task's `rank` = the request's `rank`,
  `group_id` = the request's `group_id` (`NULL` unless a goal-6 cross-list drag dropped into a
  destination group). There is no overlay row to migrate.

## Scope / auth

Task writes need no Google scope since goal 17; the `https://www.googleapis.com/auth/tasks` scope
stays in `SCOPES` only for the one-time importer (dropped in goal 17b). Notes writing (g7) needs **`drive.file` — and only `drive.file`**, never `documents`/`drive` (ADR:
`docs/goals/architecture/drive-access-scoping.md`). Scopes live in `app.google.auth.SCOPES`;
changing them requires re-running `uv run python -m app.google.auth` to re-mint a token. Never run
the consent flow from request-handling code.

A **startup scope assertion** (`assert_scopes_within_allowlist`, called in `main.lifespan`) refuses
to boot if the token carries any scope outside `ALLOWED_SCOPES` — a token *missing* `drive.file` is
fine (notes degrade to kept-local), a token *broader* than the allowlist is not. `load_credentials`
reads scopes from the token file itself (not forced to `SCOPES`) so an old narrow token still
refreshes cleanly after `SCOPES` grows.

## Goal 8: per-user credentials + per-user notes target

- **`creds` is passed explicitly, first arg.** Every `app/google/*` call (and `append_note`) takes
  a live `creds: Credentials` (the current user's). Routers get it from
  `Depends(get_current_credentials)`; the writes service forwards it into the thin client wrappers.
  **Goal 17:** the task functions of the writes service take no `creds` (local store).
  There is no global `load_credentials()` — it is now `app.google.auth.load_credentials(session, user)`
  and the scope assertion is **per-token** (a broader-than-allowlist grant → `ScopeError` → 403),
  no longer a startup boot check.
- **Overlay writes are user-scoped.** `upsert_overlay` / `get_group` / etc. take `user_id`. Since
  goal 17 `upsert_overlay` sets `rank` / `group_id` on the `Task` row (looked up via
  `tasks_store.service.get_task`, filtered by `user_id`; a missing task → `None` → 404). The
  `task_overlay` table is legacy, read only by the importer (dropped in goal 17b).
- **`append_note(creds, doc_id, folder_id, body_text, summary=None)`** — the doc/folder ids come from
  the **user's `user_settings`** (resolved by `app.settings.service.ensure_notes_target`, which
  app-creates the folder + Doc on first need), never env vars. Still insert-only, still router-only,
  still fail-closed on the folder-ancestry gate. `NOTES_DOC_ID`/`NOTES_FOLDER_ID` and
  `app.google.bootstrap` are gone. The one sanctioned file-create surface grew by `create_folder`
  (Drive root) alongside `create_doc_in_folder` — both `files().create`, so the AST insert-only test
  is unchanged (no `files().delete` / content-overwriting `files().update`).
- **`ensure_notes_target` self-heals stale ids (goal 8a).** Under `drive.file` per-file access is
  keyed to the OAuth **client id** that created the file, so a changed client id (across a deploy)
  or a user-deleted file makes the stored notes ids 404 — and the idempotency guard would otherwise
  reuse a dead id forever. Before the reuse guard, `ensure_notes_target` probes reachability via
  `docs.file_accessible` (a `files().get` **read** — no new mutation surface, AST test unaffected);
  a definite **404** drops the id and re-bootstraps, any other error fails closed (never discards a
  good id), results cached per process (a deploy re-probes each user once). Brief: `goal-8a.md`.

## Goal 9: notes hierarchy (folder/Doc tree + hierarchical routing)

- **`rename_file` — the ONE new sanctioned Drive mutation.** `docs.rename_file(creds, file_id, name)`
  is a **metadata-only** `files.update` whose body is **exactly `{"name": ...}`** — never content,
  never `parents` (no add/remove), never `trashed`. It is **settings-path-only** (called from
  `settings.service._materialize` when a node's name changed) and **never reachable from the router**
  (AST-asserted). The Docs/Drive AST surface now allows `files().update` but pins it to `_rename_file`
  alone; a unit test pins the rename body to `{"name": ...}`. Still no `files().delete`, no
  content-overwriting update, no `addParents`/`removeParents`.
- **Delete = orphan, always.** Removing a node drops it from the index; the Drive file/folder is
  **never** deleted or trashed — it stays in the user's Drive, just never written again. Re-adding the
  same name later creates a **fresh** Doc (no re-attach). Doc→folder conversion = orphan + create.
- **Eager materialization on save** (`PUT /settings/notes-index`): diff the incoming tree against the
  stored one **by `node_id`**, apply Drive ops **parent-before-child**, persisting each created/renamed
  `drive_id` as it succeeds (mirrors `ensure_notes_target`'s folder-before-doc commit). A partial
  failure persists what succeeded; a retry of the same PUT is idempotent by `node_id`. `create_folder`
  gained an optional `parent_id` (root notes folder when top-level) — still `files().create`.
- **`insert_note`'s entry shape is H3 → H4 → H5 → body → delimiter** (goal 9): the LLM one-liner is
  the **H3** headline, the timestamp the **H4** beneath it (was a bold line), then an optional **H5**
  keyword line, the verbatim body, and the `borderBottom` delimiter. A missing summary promotes the
  timestamp back to H3 (no empty headline); empty keywords skip H5. Still ONE insert-only
  `documents.batchUpdate`, no new Docs method surface. `append_note` grew a `keywords` arg.
- **Self-heal extends to hierarchy Docs.** `settings.service.resolve_note_target` probes a routed
  hierarchy Doc with `file_accessible` (cached per `(user_id, drive_id)`); a definite **404**
  re-creates the Doc **at the same path** (re-creating any missing ancestor folders) and updates the
  index; any non-404 error fails closed. The default-Doc/root-folder self-heal is unchanged.

## Goal 10: structured-body rendering in `insert_note` (the formatter)

- **The body is rendered as light markdown, deterministically — NOT an LLM step.** `docs._render_body`
  parses the (still-verbatim) body and emits Docs styling instead of one flat text run: markdown
  **headings (`#`…`######`) → bold NORMAL_TEXT lines** (marker stripped, words kept, depth as paragraph
  indent), **bullets / numbered lists (`- `/`* `/`1. `, nested by 2-space/tab indent) → real Docs
  bullets** (`createParagraphBullets`, nesting via leading tabs), **inline `**bold**` → bold runs**
  (markers consumed). Anything unrecognized passes through verbatim. No new LLM call, no third verbatim
  relaxation — only markdown *markers* are consumed as styling.
- **The no-body-headings invariant (the point of the goal).** Body input can produce bold / indent /
  bullets but **NEVER a `HEADING_*` paragraph** — the entry chrome stays the only heading structure
  (H3 one-liner → H4 timestamp → H5 keywords), so goal-9's "extract all H4s" search can never be
  polluted by a pasted `## Agenda`. A unit test pins "zero `HEADING_*` from body content, ever."
- **Still ONE insert-only `documents.batchUpdate`, no new method surface.** `createParagraphBullets` /
  `updateTextStyle` are *request types* inside the single batchUpdate, not new service methods — the
  AST insert-only test is unchanged. Bullet requests apply **last, top-down** (they consume leading
  tabs and shift later indices, so every earlier request runs against valid indices). A body with no
  markdown renders **byte-identically** to the pre-goal-10 shape (one NORMAL_TEXT paragraph).
