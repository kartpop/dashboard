# Goal 14a — Threads: several open steps per thread

**One line:** Lift goal 14's "one next step per thread" rule. A thread can hold **any number of
open steps**, each one a real Google Task in My Tasks or Follow-ups. Done steps stay a single
local history line. The open steps sit together at the end of the track as a parallel stack. A
thread is **dangling** only when it has no open steps left. Completing one open step logs it as
done, and the rest stay open.

## Why (the friction this closes)

Goal 14 lets the last step of a thread be one live task. Real threads fan out. For example, a
thread "NGO visit" whose last update was "shortlisted three NGOs" now needs **visit NGO 1**,
**visit NGO 2**, and **email NGO 3**, all open at once, some mine and some follow-ups. Today I can
only:

- link **one** of them as the next step, and
- record the others as **Log update** steps, which are static history. They don't become tasks,
  don't show up in My Tasks or Follow-ups, and can't be completed.

So the other two tasks either live outside the thread (no badge, no story) or don't exist. The
thread then reads as "one thing pending" when three are.

## Core model (deltas from goal 14)

- **Open step** (still stored as `kind = 'next'`, so there is no data rewrite): **zero or more
  per thread**. Each one is linked to a Google Task in a pinned list, with Google as the source
  of truth for its label, note, and due date, exactly as in goal 14.
- **Ordering:** all done steps come first, in history order, and then all open steps, which form
  the **open block**.
  - Within the open block, steps are ordered by **due ascending**, then by creation order. This
    is derived at read time and not stored.
  - When an open step becomes done, it **moves out of the open block** to the end of the done
    history, just before the first remaining open step. History therefore stays chronological by
    the order in which things actually got done.
- **Dangling:** an active thread with **no open steps**. The definition is the same as in goal
  14; only the count changes.
- **Unchanged:**
  - A Google task is linked by at most one open step per user, so a task row still shows at most
    one thread badge.
  - List = whose move it is.
  - Threads never deletes Google tasks.
  - Done steps are local only.

## What ships

### 1. Backend: storage (one Alembic migration)

- **Drop** the partial unique index `uq_thread_step_one_next`. **Keep** `uq_thread_step_next_task`.
- **Downgrade:** recreate the index. Document that a downgrade fails if any thread holds two or
  more open steps, and that this is accepted, since downgrade is a dev-only path.
- **Positions:** a new open step appends at `max + _GAP`, as today. Logging a done step inserts it
  at the midpoint **before the first open step**, generalising today's "before the next step".
- **Completion moves the step:** when a step flips to done (reconcile or `complete`), its
  `position` is reset to the midpoint between the last done step and the first remaining open
  step.

### 2. Backend: reconcile

These are the same rules as goal 14, applied per open step instead of to the single one:

- **Completed:** that step becomes done (completion date, snapshot, `via`) and is repositioned as
  in §1. Sibling open steps are untouched.
- **Deleted in Google:** that step is removed. The thread dangles only if it was the last open
  step.
- **Open:** refresh the step's cache.
- **Undo after a reconcile:** a **done** step whose linked task is `needsAction` again becomes
  open again and rejoins the open block at the end.
  - This replaces goal 14's "last step and no other next step" condition, which no longer makes
    sense with siblings.
  - The one-link-per-task invariant still wins: skip the flip if another open step already links
    that task.
- **Repoint on move:** unchanged. `repoint_link` already targets the step by task id.

### 3. Backend: API contract changes

| Method & path | Change |
|---|---|
| `POST /threads/{id}/next` | **No more 409** for an existing open step. It creates one more open step and returns the thread. The only 409 left is the existing task-already-linked case. |
| `POST /threads/{id}/steps` | Inserts the done step before the **first** open step. |
| `POST /threads/{id}/steps/{sid}/complete` | Flips **that** step and repositions it (§1). |
| `DELETE /threads/{id}/steps/{sid}` | Unlinks **that** open step only. The thread stays, as do its siblings. |
| `GET /threads` | Same shape. `steps` is returned in display order: done steps by position, then open steps by due ascending. |

- **No new endpoints.** The path stays `/next` so the frontend wiring and the AST-pinned write set
  are unchanged.
- **Write surface unchanged:** `{create_task, update_content, reschedule, move}`, and still never
  `delete_task`.

### 4. Frontend: data helpers (`useThreadsPanel`)

- **Replace `nextOf(t): Step | null`** with `openOf(t): Step[]`, which returns the open steps in
  display order, and `soonestOpen(t)`, which returns the first of them or null. Every caller of
  `nextOf` gets audited:
  - sort,
  - filters,
  - compact row,
  - archive toast,
  - badge links,
  - `onTaskCompleted`,
  - `isLinked`.
- **Optimistic completion** (from the threads popover or the tasks panel): flip that step to done
  and move it to the end of the done history. The thread sorts to the top as dangling **only if
  it has no open steps left**.
- **Set next step (optimistic):** append to the open block and re-sort the block by due.

### 5. Frontend: Threads panel

**Filters** (client-side, counts are of threads):

- **Needs next step:** the thread has 0 open steps.
- **My task:** the thread has at least 1 open step in My Tasks.
- **Follow-up:** the thread has at least 1 open step in Follow-ups.
- A thread with both kinds counts under both chips.

**Sort (active threads):** dangling threads first (least recently moved first), then by the
**soonest open due** ascending.

**Compact row:**
- Show the soonest open step (its ring, label, and due pill), followed by a **`+N` chip** when
  there are more open steps. The chip's tooltip lists the other labels with their due dates.
- The rest of the row is unchanged.

**Detailed track:**

```
 ●───────●───────●- - - -┬─○  visit NGO 1      My task · Mon 5 Oct
 kickoff  call    short- ├─○  email NGO 3      Follow-up · Tue 6 Oct
                  listed └─○  visit NGO 2      My task · Thu 8 Oct
                             + add open step
```

- **Done steps:** unchanged. This is the last 3, plus the "+N earlier" fold.
- **The open block:**
  - A dashed connector forks into a **vertical stack** of open steps. Each step has a ring in its
    list colour, a label clamped to one line, and a due pill. Clicking a step opens the existing
    step popover.
  - Show **up to 4** open steps. Anything beyond that folds into a "+N more" line that expands in
    place.
  - Below the stack, a faint **"+ add open step"** opens the next-step inline form.
- **Dangling:** the "What's next?" slot (+ My task / + Follow-up / Log update) is unchanged.
- **The end-of-track `+`:** still logs an update, inserted before the open block.
- **Row height:** grows with the stack. The panel already scrolls.

**Next-step inline form: rapid entry.** After Enter creates a step, the form **stays open** with
the label cleared and the list and due date kept, so three tasks take three Enters. Esc closes it.
Each create gets its own toast ("Added to My Tasks, due Mon 5 Oct"); collapse them to the latest
if they stack.

**Step popover:** unchanged. "Mark done" and "Delete (unlink)" act on that one step.

**`···` menu / archive toast:** "Its open task stays…" becomes "**Its N open tasks stay** in
Google Tasks", with the singular form when N is 1.

### 6. Frontend: task ↔ thread coupling

- **Badges:** unchanged. One task maps to at most one thread. `threads.links` now includes
  **every** open step's `task_id`.
- **Completing a linked task in the tasks panel:**
  - **It was the last open step:** the same toast as goal 14, "Logged "<label>" in <thread>.
    What's next?" with **Set next step**.
  - **Siblings remain:** "Logged "<label>" in <thread>. N still open." with a **Show thread**
    action that focuses the thread, instead of the Set next step action.
  - **Undo:** reverts it, as today.

## Locked decisions

- **Open steps are parallel and unordered.** They are not a sequence or a dependency chain. The
  display order is by due date only. There is no "do B after A".
- **History is linear.** A completed open step joins the single done line in the order it was
  completed. There are no per-branch histories.
- **Keep the `kind = 'next'` value.** Only the cardinality changes, so no rewrite of the data or
  the enum is needed. In the UI copy, "next step" can stay where it reads naturally ("Set next
  step", "Needs next step").
- **No hard cap** on open steps. The UI folds after 4.

## Out of scope (do not build)

- Dependencies or sequencing between open steps, and sub-threads.
- Done steps branching per open step (a tree-shaped history).
- Adopting existing tasks into a thread. This is still v1.1, though it gets more useful now that
  a thread can hold several.
- Bulk-creating open steps from a pasted list, or from LLM / scratchpad capture.
- Reordering open steps by drag.

## Acceptance criteria

**Reconcile unit tests** (fake Google layer), on a thread with 3 open steps:
- completing one gives 1 done step, which lands before the 2 remaining open steps, and the thread
  is not dangling;
- deleting one leaves 2 open steps;
- completing all three makes the thread dangling, with history in completion order;
- reopening a completed task returns it to the open block;
- one task id linked from two threads is still rejected.

**API:**
- `POST /threads/{id}/next` succeeds a second and third time, and 409s only on a task that is
  already linked.
- Logging a step inserts it before the first open step.
- `DELETE` on one open step leaves its siblings and its Google task in place. Verify against
  `zz-verifier-test` per `verifier-writes`, with cleanup.
- **Isolation:** the two-user tests still pass.

**Write surface:** the AST pin is unchanged and passes.

**The loop, end to end in the UI:**
1. On a thread that has done history, add 3 open steps with rapid entry: 2 in My Tasks and 1 in
   Follow-ups. All 3 appear in their lists with the thread badge.
2. Compact shows the soonest step plus a "+2" chip. Detailed shows the forked stack.
3. Filters: the thread counts under both My task and Follow-up.
4. Complete one from My Tasks. It joins the done line, the toast says "2 still open", and the
   thread does not sort as dangling.
5. Undo restores it.
6. Complete the other two. The thread dangles, and the toast offers **Set next step**.
7. Archive a thread with 2 open steps. The toast says "Its 2 open tasks stay in Google Tasks",
   and both tasks remain.

**Visual** (eyeball): the forked stack in light and dark themes, the "+N more" fold at 5 or more
open steps, and the stacked layout at ≤1080px.

**Regression:** `tsc`, the frontend build, `ruff`, and all backend tests pass. Goal 14's
single-open-step flows behave as before.

**Deploy:** `uv run alembic upgrade head` applies cleanly on SQLite and Postgres.

## Harness upkeep

- Update `.claude/rules/threads.md`:
  - the invariants section (drop "one next step per thread", add the open-block ordering and the
    reposition-on-done rule);
  - the reconcile undo rule.
- Add the goal-14a line to `docs/goals/README.md`.
- No new scopes and no owner steps.
