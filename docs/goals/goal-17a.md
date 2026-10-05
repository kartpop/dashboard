# Goal 17a — Nightly tasks + threads snapshot to a Google Doc, and 30-day pruning

**One line:** Every night, for each user, the app **fully rewrites** one dedicated Google Doc
with a readable snapshot of their tasks and threads, and then deletes completed tasks older than
30 days. An **Export now** item in a `⋯` menu runs the same export on demand. This is a narrow,
explicit amendment to the drive ADR's no-rewrite rule. Depends on goal 17.

## Why

- **After goal 17, tasks exist only in the app DB.** If the dashboard is down, there should still
  be a human-readable copy somewhere familiar. Google Drive is where the notes already live.
- **Rewrite, don't append.** An append-only log would grow without bound and isn't readable as
  "the state of things". Overwriting a single Doc keeps it small. Docs **version history** keeps
  every earlier night's snapshot for free.
- **Completed tasks pile up.** Without pruning, the `task` table only grows. Thirty days covers
  "what did I finish recently", and the snapshot's version history covers anything older.

## This is a view, not a backup

The snapshot is for reading. It is **not** a restore source, so nothing ever parses it back into
the DB. Disaster recovery of the DB is the nightly SQLite `.backup` cron from goal 8
(`scripts/backup.py`). Keep the two separate.

## ADR amendment (write it in `architecture/drive-access-scoping.md`)

Add a dated amendment section, "**2026-10 (goal 17a): one rewritable Doc per user**":

- **Scope of the exception:**
  - The Layer-5 "no doc-rewrite pattern anywhere" rule gets **exactly one** exception: the
    per-user tasks snapshot Doc.
  - Its id lives in `user_settings.tasks_snapshot_doc_id`. It is created by the existing single
    create path (`create_doc_in_folder`) under the user's `notes_folder_id`.
  - Only **one** function may issue a delete-content request: `docs.rewrite_snapshot(creds,
    doc_id, body)`. Its only caller is `snapshot/service.py`.
- **Enforcement:**
  - The AST guardrail pins `deleteContentRange` (and any other content-removal request) to
    `rewrite_snapshot`.
  - The snapshot service reads the doc id **only** from `user_settings.tasks_snapshot_doc_id`,
    and asserts it differs from `notes_doc_id` and from every doc id in `notes_index` before
    writing. If it matches any of them, it fails closed.
  - The folder-ancestry gate (Layer 2) runs before the rewrite, as for notes.
- **Blast radius:** the worst a bug can do is wipe a Doc that is fully regenerable from the DB.
  The notes Docs stay insert-only.

## What ships

### 1. Storage (one Alembic migration)

- `user_settings.tasks_snapshot_doc_id` (nullable), and `user_settings.last_snapshot_at`
  (nullable timestamptz).

### 2. Snapshot Doc lifecycle

- **`ensure_snapshot_doc(session, creds, user_id)`** follows the goal-8a self-heal pattern:
  - If the id is unset, or it 404s (the user deleted the Doc, or the client id changed), create
    "Dashboard — Tasks snapshot" inside `notes_folder_id` and persist the id.
  - It calls `ensure_notes_target` first, so the folder exists.

### 3. Rendering (pure function, unit-tested)

**`render_snapshot(tasks, lists, groups, threads, now) -> str`** produces markdown that goes
through the existing goal-10 markdown→Docs renderer, so headings and bullets look the same as in
the notes Docs.

```
Tasks snapshot — generated Mon 5 Oct 2026, 02:00 IST
Rewritten nightly by the dashboard. Edits here are overwritten.

THREADS
• NGO visit — 3 done · open: visit NGO 1 (My task, Mon 5 Oct), email NGO 3 (Follow-up, Tue 6 Oct)
• Quarterly report — needs next step · last: sent draft (Fri 2 Oct)

MY TASKS
Overdue
• Renew domain (due Thu 1 Oct)
Today · Mon 5 Oct
• [Group: Errands] Pick up parcel
• Call teammate A
No date
• …

FOLLOW-UPS
…

OTHER LISTS
…

COMPLETED (last 7 days)
• Mon 5 Oct — Send invoice (My Tasks)
• …
```

- **Threads:** active threads only, with open steps first. Archived threads are left out.
- **Tasks:** shown per list, in the same bucket order as the panel (Overdue, then dated buckets,
  then No date), with groups as a prefix tag, and notes as one indented line truncated to
  200 characters.
- **Completed:** the last **7** days. The DB keeps 30.

### 4. Rewrite mechanics

- **`docs.rewrite_snapshot(creds, doc_id, body)`:**
  - `documents.get` reads the current `endIndex`.
  - Then **one** `batchUpdate` does `deleteContentRange(1, end-1)` (skipped if the Doc is empty)
    followed by the rendered inserts.
  - One batchUpdate means Google applies it atomically. A half-written snapshot is never
    visible.
- **Scope:** `drive.file` is enough, because the Doc is app-created. No new scope.

### 5. Nightly job

**`snapshot/scheduler.py`** reuses the news-scheduler shape: an in-process loop, a 15-minute
tick, the startup grace delay, and a per-user try/except.

**Due when:** the IST time is ≥ 02:00 and `last_snapshot_at` is before today's 02:00 IST. A
missed night (for example, the server was down) runs on the next tick after boot.

**Per user:**
1. **Export:** `ensure_snapshot_doc`, then `render_snapshot`, then `rewrite_snapshot`, then set
   `last_snapshot_at`.
2. **Prune:**
   - delete `task` rows with `status='completed'` and `completed_at < now - 30 days`;
   - delete `task_group` rows in **past** buckets that now have no tasks.

**Prune runs whether or not the export succeeded.** It's local and independent. A failed export
is logged and retried on the next tick, because `last_snapshot_at` is not advanced.

**Who is included:** users without Drive access (no token, or a missing scope) skip the export
but still get pruned.

**Thread steps survive pruning.** A done step already carries its own `label`, `occurred_on`, and
`via`. A pruned task only leaves a stale `task_id` on a *done* step, which nothing dereferences.
Open steps can't point at completed tasks.

### 6. Export now

- **`POST /tasks/snapshot`** runs the export only (no prune) for the current user. It returns
  `{doc_url, generated_at}`. It is rate-limited to once per 30s per user (a 429 with a friendly
  message).
- **UI:**
  - **Desktop:** a `⋯` button in the Tasks area header opens a menu with "Export snapshot to
    Google Doc".
  - **Mobile (goal 15):** the same item goes in the **Me** sheet.
- **Toast:** "Snapshot updated · Open doc" (opens `doc_url` in a new tab). On failure: "Export
  failed: …".

## Locked decisions

- **One Doc per user, fully rewritten.** There is no per-day Doc and no append.
- **Nightly at 02:00 IST**, then pruning. Completed tasks are kept for **30 days** in the DB and
  shown for **7 days** in the snapshot.
- **Scratch is not included.** Scratch content is already routed into the notes Docs.
- **The snapshot is never read back by the app.**

## Out of scope (do not build)

- Restoring from the snapshot, and machine-readable export formats (JSON/CSV).
- Exporting to Google Tasks, or to Sheets.
- Per-user snapshot schedules or settings. The time and window are constants in
  `snapshot/config.py`.
- Including archived threads or scratch.

## Acceptance criteria

**Unit:**
- `render_snapshot` golden tests: empty user, groups, overdue, multi-open-step threads, and the
  completed-7-day window.
- The prune boundary: 29 days 23 hours is kept, 30 days 1 minute is deleted.
- Empty past-bucket groups are deleted. A future bucket's empty group is kept.
- Done thread steps survive pruning of their task.

**Safety:**
- The AST test pins content-removal requests to `rewrite_snapshot`.
- The snapshot service refuses (fails closed) when the snapshot id equals `notes_doc_id` or any
  `notes_index` doc.
- The ancestry gate runs before the rewrite.

**Scheduler:**
- "Due" logic around 02:00 IST (before, after, missed night).
- One user's failure doesn't block the others.
- A failed export doesn't advance `last_snapshot_at`.

**Live (owner-observed, one run each):**
1. "Export now" creates the Doc in the notes folder, and the toast link opens it.
2. A second "Export now" replaces the content rather than appending it, and version history shows
   both.
3. Deleting the Doc in Drive and exporting again recreates it (self-heal).
4. The next morning, the nightly run has updated the Doc and `last_snapshot_at`.

**Regression:** all tests, `tsc`, the build, and `ruff` pass. `alembic upgrade head` applies
cleanly on SQLite.

## Harness upkeep

- **The ADR amendment** described above.
- **`docs/goals/README.md`:**
  - add the goal-17a line;
  - the Drive spanning-constraint bullet notes the single rewritable snapshot Doc.
- **`.claude/rules/writes.md`:** the snapshot rewrite is the one sanctioned overwrite, with its
  guards.
- **No owner steps.** There is no new scope or env var. The Doc is created by the app.
