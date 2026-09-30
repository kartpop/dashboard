# Goal 14 — Threads: the story behind each task

**One line:** A new **Threads** panel sits in the bottom-left of the dashboard, under My Tasks and
Follow-ups. A thread is an ordered list of steps: done steps are local history, and the last step
can be a **live next step**, which is a real Google Task in My Tasks or Follow-ups. When that task
is completed (from the dashboard, the phone, or Google Tasks), the step becomes done and the thread
asks **"What's next?"** Also: the scratchpad's RECENT list shrinks to 2 visible rows so the editor
gets the height.

## Why (the friction this closes)

My Tasks and Follow-ups only show the **latest snapshot** of each piece of work. As an engineering
manager I run many parallel threads, some I'm driving and some I'm chasing. Example: *intro call
with a partner NGO → built an LLM wiki on their content → shared it → no reply, reminder #1
mid-Sept → reminder #2 end of Sept*. Only that last item exists as a task today. Seeing the whole
chain is what makes me plan the next move. It also shows me the threads I've dropped: ones where
nothing is scheduled next.

## Reference prototype

The UX was settled in a clickable prototype. It's a copy in the repo,
[`goal-14-prototype.html`](goal-14-prototype.html) (open it in a browser), and the same page is
published as a private artifact at <https://claude.ai/artifact/P9gnyafzRriqYo6LBP5pvB>.

- **Treat it as the UX spec:** interactions, states, copy, and layout.
- **It is not the implementation:** its data model, sample data, and vanilla JS are throwaway.
- **Styling:** match the real app's existing tokens and panel styling (`index.css`), not the
  prototype's CSS.

## Core model

- **Thread:** a title, an ordered list of steps, and an `archived` flag.
- **Step, one of two kinds:**
  - **Done step:** a label, a date it happened (`occurred_on`), and an optional note. Stored
    locally only.
  - **Next step:** at most **one per thread**, always last. It is **linked to a Google Task** in
    the My Tasks or Follow-ups pinned list:
    - **Google is the source of truth** for its label (task title), note (task notes), and due
      date. The step row holds the link plus a cached copy of those fields.
    - **Which list it's in says whose move it is:** **My Tasks** means I owe the next move;
      **Follow-ups** means they owe it and my task is to nudge them.
- **No "waiting" step without a task.** Waiting on someone always means a Follow-up with a nudge
  date.
- **Dangling thread:** an active thread with no next step. It shows a "No next step" chip and a
  "What's next?" slot, and it sorts first.

## What ships

### 1. Backend: storage (new `app/threads/` package + one Alembic migration)

- **`thread` table:**
  - `id` PK
  - `user_id` FK → `user.id` (indexed)
  - `title`
  - `archived_at` (nullable)
  - `created_at`, `updated_at`
- **`thread_step` table:**
  - `id` PK
  - `thread_id` FK
  - `user_id` FK (indexed)
  - `position` (float rank, midpoint insertion like the overlay)
  - `kind` (`done` | `next`)
  - `label`
  - `note` (text, default `""`)
  - `occurred_on` (date, nullable; set for `done`)
  - `tasklist_id`, `task_id` (nullable: set for `next`, and retained on a `done` step that
    came from a completed task)
  - `due` (cached, nullable)
  - `via` (`mine` | `follow` | null: which list a completed next step was in; drives the
    "via My Tasks / via Follow-ups" caption)
  - `created_at`, `updated_at`
- **Invariants, enforced in the service and backed by DB constraints where cheap:**
  - At most one `next` step per thread.
  - A Google task id is linked by at most one active `next` step per user. Use a partial unique
    index on `(user_id, task_id)` where `kind = 'next'` if SQLite and Postgres both allow it;
    otherwise enforce it in the service.
  - The `next` step always has the highest `position`.
- **Registration:** add the models module to `alembic/env.py`'s explicit imports.
- **Dates:** use IST (`today_ist()`), the same as the task buckets.

### 2. Backend: reconcile (Google → threads)

`GET /threads` fetches the two pinned lists (reuse `google/tasks.get_task_lists`, which already
returns completed tasks) and reconciles every linked step before responding:

- **Linked task is completed:** the `next` step becomes `done`.
  - Set `occurred_on` to the task's completion date in IST. `_reshape_task` gains a `completed`
    field for this; fall back to today if it's missing.
  - Snapshot the final title and notes into `label` and `note`.
  - Set `via`.
- **A `done` step's linked task is back to `needsAction`:** the step becomes `next` again, but
  only if the thread has no other `next` step and this is the thread's last step. This covers
  undoing a completion in the tasks panel after a reconcile already ran. Otherwise leave it done.
- **Linked task is gone (deleted in Google):** remove the `next` step. The thread becomes
  dangling.
- **Linked task is still open:** refresh the cached `label`, `note`, and `due` from Google.
- **Moves between lists:** when a linked task is moved between lists through the dashboard, the
  task id changes, because a move is insert-then-delete. The `move` endpoint in
  `routers/tasks.py` must call `threads_svc.repoint_link(user, old_list, old_id, new_list,
  new_id)` after a successful move. Without it, reconcile sees the old id as deleted. This covers
  both the tasks panel's move and drag-between-pinned-lists.
  - A move done **outside** the dashboard (e.g. in the Google Tasks app) looks like a deletion.
    That's acceptable in v0; note it in the rule file.

### 3. Backend: API (`app/threads/router.py`, thin, `ApiError` for errors)

Everything is scoped to `current_user.id`. Freeze this contract before building the frontend.

| Method & path | Body | Effect |
|---|---|---|
| `GET /threads` | none | `{threads: Thread[]}`: all of the user's threads, active and archived, after reconcile. The client filters and sorts. |
| `POST /threads` | `{title}` | Create a thread with no steps. |
| `PATCH /threads/{id}` | `{title?, archived?}` | Rename, archive, or restore. Archive **does not touch** the linked task, which stays in Google Tasks. |
| `POST /threads/{id}/steps` | `{label, note?, occurred_on?}` | Log a `done` step. It is inserted **before** the `next` step if there is one; `occurred_on` defaults to today (IST). |
| `POST /threads/{id}/next` | `{label, list: "mine"\|"follow", due, note?}` | Create the Google task via `writes_svc.create_task` in the resolved pinned list, then insert the `next` step linked to it. Return **409** if a `next` step already exists. |
| `PATCH /threads/{id}/steps/{sid}` | `{label?, note?, occurred_on?, due?, list?}` | **Done step:** local update. **Next step:** `label`/`note` go through `writes_svc.update_content`, `due` through `writes_svc.reschedule`, and `list` through `writes_svc.move`, which repoints the link. Unchanged fields are skipped. |
| `POST /threads/{id}/steps/{sid}/complete` | none | **Next step only.** Complete the Google task (`update_content(status="completed")`), then flip the step as in reconcile. |
| `DELETE /threads/{id}/steps/{sid}` | none | **Done step:** delete it. **Next step:** **unlink only**. Remove the step and leave the Google task in its list; the toast says so. |

**Shapes:**

- `Thread`: `{id, title, archived, created_at, last_moved_on, steps: Step[]}`.
  - `last_moved_on` is the latest `done.occurred_on`, or `created_at` if there are no done steps.
  - Steps are ordered by `position`.
- `Step`: `{id, kind, label, note, occurred_on, list, due, tasklist_id, task_id, via}`.
  - `list` is `mine` / `follow` for `next`, null for `done`.
- The frontend derives the `task_id → thread` map for badges from this payload. There is no
  separate endpoint.

### 4. Backend: write rules

- **`writes.md` changes:**
  - Add the threads service as a **new sanctioned caller** of `create_task` (the list becomes:
    user endpoint, auto-router, threads).
  - Add it as a caller of `update_content`, `reschedule`, and `move`.
  - **Threads never calls `delete_task`.** Unlinking and archiving leave the task alone.
- **AST test:** add one (mirroring `test_router_write_dependency_set_is_insert_only`) that pins
  the threads service's `writes_svc` dependency set to exactly `{create_task, update_content,
  reschedule, move}`.
- **Failure handling:** follow the existing contract of roll back and don't retry.
  - If `create_task` fails, no step row is written.
  - If the Google write succeeds but the DB write fails, log it. The next reconcile can't
    recover an unlinked task, so do the Google write first and the DB write in the same request.
    An orphan task in a list is the acceptable failure mode.
- **No LLM** anywhere in this goal. The runtime-LLM set is unchanged.

### 5. Frontend: layout

- **New grid:** the dashboard's top area becomes:
  - **Left block** (the current My Tasks and Follow-ups widths, i.e. `--w0 + handle + --w1`):
    **two rows**.
    - **Top row:** My Tasks | Follow-ups, exactly as today. Same resize handle, same shared
      `DndContext`, each column scrolls internally.
    - **Bottom row:** **Threads**, spanning both columns.
  - **Right column:** Scratchpad, spanning **both rows**.
- **Row split:** default about **45% tasks / 55% threads**, with a draggable horizontal handle
  between the rows (clamped 20–80%, ephemeral like the column widths, hidden when stacked).
  *(Pulled into scope by the owner during implementation, 2026-09-29.)*
- **Narrow screens:** at the existing `max-width: 1080px` stack, the order is My Tasks,
  Follow-ups, Threads, Scratchpad.
- **Wiring:** extend `PinnedTasksRow`'s grid rather than adding a wrapper that breaks column
  resizing (`frontend.md`: adding a column means extending the row). Threads is passed in as a
  node from `DashboardPage`, the same way the scratchpad is.

### 6. Frontend: Threads panel (`frontend/src/panels/threads/`, `useThreadsPanel` hook)

Follow the prototype.

**Header:** one row, containing:
- The title "Threads".
- **Filter chips with counts:** All · Needs next step · My task · Follow-up · Archived.
  - Needs next step shows its count in the warning colour when it is above 0.
  - Filtering is client-side.
- **View toggle:** two icon buttons with tooltips, Compact (three lines) and Detailed (mini step
  track). Remember the choice in `localStorage`, wrapped in try/catch. **Default: Compact.**
- A **"+ thread"** button.
- **Not included:** a search box and a Stale filter.

**Sort (active threads):** dangling threads first (least recently moved first), then by the next
step's due date ascending.

**Compact row** (one line):
- A caret and the title.
- **If there is a next step:** a ring in the list colour, the next step's label, and a due pill
  (`My task · Tomorrow` / `Follow-up · Wed Oct 14`; overdue shows in the warning colour).
- **If dangling:** a "No next step" chip and "Last: <label>".
- The days since the last move, right-aligned, in the **warning colour at 10+ days**.
- The `···` menu.
- **Clicking the row expands it** into the detailed track, in place. Starting an edit in a row
  keeps it expanded.

**Detailed row:**
- **Left column:** title, "N steps · moved Xd ago" (warning colour at 10+ days), and a "No next
  step" chip when dangling.
- **Right column:** a horizontal step track.
  - Only the **last 3 done steps** show; older ones fold into a "+N earlier" chip that expands
    and collapses them. The track scrolls horizontally within the row when expanded.
  - **Done step:** a solid dot, a 2-line-clamped label, the date, a note icon if it has a note,
    and "via My Tasks / via Follow-ups" if it came from a task.
  - **Next step:** a ring in the list colour and a due pill. The connector into it is dashed.
  - **Dangling:** a dashed warning slot "What's next?" with **+ My task**, **+ Follow-up**, and
    **Log update**. An empty thread gets the hint "Log what's happened so far, or set a next
    step."
  - A faint **`+`** at the end of the track (full strength on row hover) logs an update, which is
    inserted before the next step.

**Inline forms:** Enter submits, Esc cancels.
- **Log update:** "What happened?", dated today.
- **Next step:** a label, a My task / Follow-up segment, and a due date defaulting to tomorrow.
  On create, the toast says "Added to Follow-ups, due tomorrow."

**Step popover** (click any step):
- **Every step:** an editable label, a note textarea, and Delete.
- **Done step:** a "Happened on" date.
- **Next step:**
  - A My task / Follow-up segment (switching it is a **move**).
  - A due date.
  - A note textarea captioned "Same text as the Google Task's description".
  - **Mark done**.
  - Delete, which **unlinks** the step as in §3.
- Edits save on blur or close, not per keystroke. Next-step edits go through the §3 PATCH.

**`···` menu:**
- Active thread: Show all steps / Show recent steps · Log update · Archive thread.
- Archived thread: Restore thread.
- Archiving gives an **Undo** toast. If an open task exists, the toast adds "Its open task stays
  in Google Tasks."

**New thread:** "+ thread" shows a name input at the top of the list (and clears any filter).
Enter creates the thread and opens the Log update form on it.

**Mutations:** optimistic, with snapshot-rollback and an error toast on failure (`frontend.md`).
There are no confirm dialogs.

**Polling:** the same 45s cadence and manual-refresh idea as the tasks panel. Skip a tick while a
form or popover is open.

### 7. Frontend: task ↔ thread coupling (state lifted to `DashboardPage`)

`DashboardPage` owns both `useTasksPanel` and `useThreadsPanel`. Panels never import each other.

- **Thread badge on task rows:** a task in My Tasks or Follow-ups that is a thread's next step
  shows a small thread badge (icon plus a truncated thread title, with the full title in a
  tooltip) in `SortableTask`.
  - The badge must **not** break the drag listeners (`tasks-panel.md`).
  - Clicking it scrolls the Threads panel to that thread, expands it in Compact view, flashes it,
    and resets filters if the thread is hidden.
- **Completing a linked task:**
  - **From the tasks panel:** the threads state updates **optimistically**, the same moment,
    through a callback from the lifted state. The step flips to done and the thread moves to the
    top as dangling.
  - A toast says: "Logged "<label>" in <thread>. What's next?" with a **Set next step** action
    that opens the next-step form on that thread.
  - **Undo** (uncomplete) in the tasks panel reverts the thread state.
  - The backend learns about the completion via reconcile. No extra write is needed.
- **Tasks panel edits of a linked task** (title, notes, due, move) are reflected in the thread on
  the next threads refresh. Trigger a threads refresh after those writes resolve, rather than
  duplicating the optimistic logic.
- **Notes on a linked task** show the tasks panel's existing notes affordance. It's the same text
  as the step note.

### 8. Scratchpad: RECENT shows 2 rows

- **Default size:** the RECENT section's default height fits **its header plus 2 entries**.
  Everything else scrolls inside it (`overflow-y: auto`, already in place). The editor takes the
  rest of the now full-height column.
- **Split drag:** the existing editor/recent drag (`--editor-ratio`) keeps working. Only the
  default and minimum change so that 2 rows are visible at rest.
- **Unchanged:** RECENT's contents and ordering (unresolved first, then the dimmed routed tail).

## Locked decisions

- **The next step is a Google Task, not a local reminder.** Google is the source of truth for its
  title, notes, and due date. The step note and the task description are the **same field**.
- **List = whose move it is.** My Tasks means mine; Follow-ups means theirs. There are no extra
  step types (no "waiting" state, no owner field).
- **One next step per thread**, always last. Done steps are local-only history.
- **Threads never deletes Google tasks.** Deleting a next step or archiving a thread unlinks it
  and leaves the task alone.
- **Completion is detected by reconcile on `GET /threads`**, plus an optimistic flip when
  completed from the dashboard. There are no webhooks and no background job.
- **A dashboard move repoints the link.** A move outside the dashboard reads as a deletion (v0
  limitation).
- **Compact is the default view.** The last 3 done steps are shown in Detailed. The stale
  threshold is 10 days (a visual cue only, not a filter).
- **45/55 default row split**, draggable (clamped 20–80%, not persisted).

## Out of scope (do not build)

- **Adopting existing tasks into a thread** (e.g. turning an existing "teammate: partner leads" follow-up
  into one). This is v1.1.
- Scratchpad capture into a thread, LLM summaries, and "suggest next step".
- Attachments or links on steps, drag-reordering steps, and moving steps between threads.
- Search, a Stale filter, and persisting the tasks/threads row split.
- Detecting moves made outside the dashboard.
- Sharing threads between users.

## Acceptance criteria

**Isolation:** two-user isolation tests on every threads endpoint. User B can never read or
mutate user A's threads or steps.

**Reconcile unit tests** with a fake Google layer covering:
- completed → done, with the completion date and a snapshot of title and notes;
- uncompleted → next again, when it is the last step with no other next;
- deleted → next step removed;
- open → cache refreshed;
- a dashboard move → link repointed and still `next`.

**Write surface:**
- The AST test pins the threads service to `{create_task, update_content, reschedule, move}`.
- `delete_task` is never reachable from threads.
- `writes.md` lists the new callers.

**API contract:**
- `POST /threads/{id}/next` returns 409 when a next step exists.
- Logging a step inserts it before the next step.
- Deleting a next step leaves the Google task in place (verify against the `zz-verifier-test`
  list per the `verifier-writes` skill, with cleanup).

**The loop, end to end in the UI:**
1. Create a thread, log 2 steps, and set a Follow-up next step. The task appears in Follow-ups
   with the thread badge.
2. Complete it from the Follow-ups panel. The thread immediately shows it as done ("via
   Follow-ups"), sorts to the top with "No next step", and the toast offers **Set next step**.
3. Undo restores the next step.
4. Completing the same kind of task in the Google Tasks app shows up as done after a refresh.

**Notes round-trip:** a note edited in the step popover shows as the task's notes in the tasks
panel (and in Google Tasks), and the reverse after a refresh.

**Switching list** in the popover moves the task between My Tasks and Follow-ups, and the thread
stays linked.

**Layout and visual checks** (eyeball, per the g4 lesson):
- Threads fills the bottom-left under both task columns.
- Scratchpad spans the full height.
- Column resizing still works.
- The stacked order at 1080px or narrower is correct.
- Compact and Detailed both match the prototype.
- Both light and dark themes are legible.
- RECENT shows 2 rows at rest and scrolls for the rest.

**Regression:** `tsc`, the frontend build, `ruff`, and all backend tests pass. Existing task
panel behaviour (drag between lists, reschedule, undo toasts), scratchpad capture and routing,
and the calendar strip are intact.

**Deploy:** `uv run alembic upgrade head` applies cleanly on SQLite and Postgres.

## Harness upkeep (closing checklist, friction-driven only)

- Add a `threads` entry to `.claude/rules/` if the reconcile, link, and repoint rules want a home
  (probably a short `threads.md` scoped to `backend/app/threads/**` and
  `frontend/src/panels/threads/**`). Update `writes.md` (new callers) and `frontend.md` (grid
  shape).
- Update `docs/goals/README.md` (the goal-14 line) and the root `README.md` if endpoints are
  listed there.
- Record rule fire/no-fire (`/context`) on the threads and writes edits.
- No new OAuth scopes and no owner Google-side setup, so no owner-steps file (just the migration).
