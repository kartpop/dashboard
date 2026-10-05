---
paths: ["backend/app/threads/**", "frontend/src/panels/threads/**"]
---

# Threads (goal 14, 14a, 17) — link, reconcile

A thread is an ordered list of steps. **Done** steps are local history. After them comes the
**open block**: any number (goal 14a) of **next** steps, each a link to a real task (in the app's
task store since goal 17) in a pinned list (`My Tasks` = my move, `Follow-ups` = their move). Open steps are parallel — no
sequencing between them. Briefs: `docs/goals/goal-14.md`, `docs/goals/goal-14a.md`.

## Invariants (service-enforced, DB-backed where cheap)

- **Every done step ranks before every open step.** `add_step` inserts a done step at the midpoint
  *before the first* open step; `set_next` appends at `max + 1000` (no 409 — goal 14a dropped the
  `uq_thread_step_one_next` index). A step flipping to done is **re-ranked** (`_done_slot`) to the
  end of the done history, before the remaining open steps; a reopened one goes to `max + 1000`.
- **Display order is derived, not stored:** `serialize_thread` serves done steps by rank, then the
  open block by due ascending (undated last), ties by rank. The frontend's `orderSteps` mirrors it
  for optimistic updates; read open steps with `openOf` / `soonestOpen`, never "the last step".
- **A task is linked by at most one next step per user.** Partial unique index
  `uq_thread_step_next_task` (`user_id, task_id WHERE kind='next'`) — works on SQLite and Postgres.
- **The task store is the source of truth** for a next step's title / notes / due (goal 17; it
  was Google before). The row caches them; the step note *is* the task's description (same field).
- **`via`** holds the pinned list (`mine`/`follow`) the linked task is in. On a next step the API
  exposes it as `list`; once the step is done it's frozen and exposed as `via` (the "via
  Follow-ups" caption). Reconcile refreshes it while the step is next.

## Writes (see writes.md)

- The threads service's whole task-write surface is **`writes_svc.{create_task, update_content,
  reschedule, move}`** — AST-pinned in `tests/test_threads.py` (which also asserts threads never
  imports `app.google.tasks`). Reads go through `tasks_store.service` (`get_task_lists`,
  `get_tasklist_refs` for `_resolve_list_id`). No `creds` anywhere in the threads service.
- **Never deletes a task.** Unlinking a next step (`DELETE …/steps/{sid}`) and archiving a thread
  leave the task in its list.
- **Task first, step second, same request.** A `create_task` failure writes no row. A DB failure
  linking the step after a successful (committed) create is logged and 500s
  (`thread_link_failed`): an orphan task in a list is the accepted failure mode (reconcile can't
  recover an unlinked task).
- `update_step` on a next step: label/note → `update_content`, due → `reschedule`, list → `move`
  (which carries a due change in the same write). Each successful write is committed before the
  next one, so a later failure leaves the cache true.
- The write endpoints (`set_next`, `update_step`, `complete_step`) depend on
  `tasks_store.deps.tasks_ready` (import gate, `503 tasks_import_pending`); `GET /threads` does not.

## Reconcile (`GET /threads`)

A cheap local join (goal 17): one `store.get_task_lists(session, user_id)` read (skipped when no
step is linked, and **skipped for a user not yet imported** — against an empty store every link
would read as deleted, so the cached steps are served) indexes every task by id, then:
completed → done (dated by `completed` in IST, else today; label/note snapshotted; several in one
pass flip in completion order); **any** done step whose task is `needsAction` again → next again at
the end of the open block, unless another next step links that task (undo after a reconcile);
missing → that next step is deleted (the thread dangles only if none are left; this also covers an
open step whose task wasn't imported); open → refresh the cache. It runs on every `GET /threads`, so
a completed linked task flips its step on the very next fetch. (The goal-14 "Google fetch failed →
serve cache" fallback is gone — there is no remote fetch.)

## Moves keep the link (goal 17)

A move is an in-place update, so **the task id never changes** — a link survives a menu move or a
pinned-pair drag with no repointing (`repoint_link` was deleted). `update_step`'s list switch sets
the step's own `tasklist_id` / `via` after the `move`. Reconcile also refreshes a next step's
`tasklist_id` / `via` from the task's current list.

## Frontend coupling (DashboardPage owns both hooks)

- `useThreadsPanel` (data + writes) is lifted to `DashboardPage` next to `useTasksPanel`; the
  panels never import each other. Badges come from `threads.links` (`task_id → {id, title}`,
  active threads only), passed to `PinnedTasksRow` as `threadLinks` + `onOpenThread`.
- Completing a linked task in the tasks panel: `useTasksPanel({onTaskCompleted})` asks
  `threads.markLinkedCompleted(taskId)`, which flips that step **optimistically** and returns the
  toast copy plus `remaining` (siblings still open); the tasks toast then carries **Set next step**
  (or **Show thread** with "N still open" when siblings remain) beside **Undo**, and Undo calls
  `revertLinkedCompleted`. The backend learns about the completion via reconcile — no extra write.
- Other tasks-panel writes on a linked task (`onTaskWritten`) trigger a threads refresh rather than
  duplicated optimistic logic; threads writes that touch a task call `onTasksChanged` → the tasks
  refresh.
- Outside requests (badge click, Set next step) arrive as `threads.request` (nonce-guarded) and
  the panel consumes them during render (the "adjust state on prop change" pattern), not in an
  effect. Polling (45s, via `usePoll` — paused while the tab is hidden, one catch-up tick when it
  becomes visible, `apiPollGet` drops a result that raced a write) is held while a form or popover
  is open.
- **Phone (goal 15, `ThreadsPanel mobile`):** always the compact list (view toggle + panel refresh
  hidden; refresh lives in the Home header). Rows are `MobileThreadRow` — collapsed: title · age ·
  ⋯ over the soonest open step in full + pill + "+N steps"; expanded: a vertical list (open steps,
  newest 3 done struck with the "+N earlier" fold, then + next step / + log update, or the
  "What's next?" slot inline when dangling). `StepPopover` and `ThreadMenu` render inside `Sheet`
  (same content and callbacks; their outside-click listeners are skipped).
