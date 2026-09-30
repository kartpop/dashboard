---
paths: ["frontend/src/panels/tasks/**"]
---

# Tasks panel — DnD architecture and learnings

This file captures the design decisions and bugs encountered while building the drag-and-drop
layer in `TasksPanel.tsx` / `useTasksPanel.ts`. Read it before touching either file.

## Architecture snapshot

The tasks panel uses **@dnd-kit** with a **flat SortableContext per bucket**. All sortable
IDs — standalone task IDs, group header IDs (`group-{id}`), and task IDs nested inside groups
— live in a single `SortableContext items` array. `GroupContainer` and `SortableTask` both
call `useSortable`; they're all children of the same context.

The alternative (nested `DndContext` per group) was rejected because dnd-kit does not support
dragging an item between two nested contexts cleanly.

### ID encoding
| Entity | Sortable ID |
|---|---|
| Standalone task | `task.id` (Google Tasks string) |
| Group header | `group-{group.id}` (prefixed number) |
| Task inside group | `task.id` (same string — no prefix) |

`activeId.startsWith("group-")` is the only way `handleDragEnd` distinguishes a group drag
from a task drag. Do not rename this prefix without updating every reference.

### Container resolution
`findContainer(id, items)` maps a sortable ID to `{type:"bucket"}` (top-level standalone) or
`{type:"group", groupId:N}` (inside a group). It is NOT called for group-prefixed IDs — those
are handled before `findContainer` in `handleDragEnd`.

### State ownership
All rank arithmetic happens in `TasksPanel.tsx` before calling the hook. `useTasksPanel.ts`
receives the pre-computed `newRank` and simply applies it. Never compute ranks inside the hook.

---

## Goal 4 — cross-bucket reschedule & move-to-list (first Google writes)

### One DndContext per LIST (was: per bucket)
To drag a task *between* date-buckets, the `<DndContext>` moved up from `BucketSection` to
`TaskListSection` — it now wraps every bucket of one list. Each bucket keeps its own
`<SortableContext>`; sortable ids (`task.id`, `group-{id}`) are globally unique so they don't
collide across buckets in the shared context. Each bucket's `<ul>` is also a
`useDroppable({ id: "bucket:"+bucket.key })` (class `.bucket-droppable`, min-height) so a drop in
a bucket's open area resolves to that bucket even when it has no item under the pointer.
Cross-**list** drag is still unsupported (each list is its own DndContext) — moving between lists
is the menu's job, not drag.

### `handleDragEnd` now lives at list level
1. `findBucketForId(buckets, activeId)` → source bucket; the dest bucket comes from `overId`
   (`bucket:{key}` → by key, else `findBucketForId(overId)`).
2. **Group drag:** groups never span buckets — if dest bucket ≠ source bucket, `return`; else the
   g3 group-reorder runs (with `bucket:` open-area resolving to the bucket end).
3. **Task drag, dest bucket ≠ source bucket → RESCHEDULE branch:**
   - `dueDate = destBucket.key === "NO_DATE" ? null : destBucket.key`.
   - **Destination group resolution:** drop on a `group-{id}` header or on a task nested in a dest
     group → `destGroupId = that group`; drop on the bucket open area or a standalone task →
     `destGroupId = null`. (Same rule as a within-bucket drop, just evaluated in the dest bucket.)
   - `destIndex` + `newRank` are computed against the **destination** container with the task
     hypothetically inserted (`computeGroupTaskRank` for a dest group, `computeMidpointRank` for the
     dest bucket) — identical math to g3, different container.
   - Calls `rescheduleTask(...)`.
4. **Task drag, same bucket → unchanged g3 paths** (reorder / move-into-group / ungroup), overlay
   PATCH only, no Google write. All five bug-fixes below still apply.

### Snapshot-rollback pattern (Google writes are NOT fire-and-forget)
Unlike overlay PATCHes (optimistic, errors swallowed), `rescheduleTask` and `moveTaskToList`:
- capture `snapshotRef.current = prev.taskLists` **inside** the `setState` updater (so it's the
  latest committed state) *before* applying the optimistic transform;
- fire exactly one `apiPost`;
- on `.catch`, restore `taskLists` from the snapshot **and** set `writeError` (the `.toast`). Never
  swallow a Google-write error.
`rescheduleTask` moves the row across two buckets in one transform (`moveTaskAcrossBuckets`):
remove from source (with the same group auto-remove as `moveTask` — an emptied source group is
dropped from local state), set `due`/`group_id`/`rank`, insert into the dest bucket at `destIndex`.
`moveTaskToList` optimistically removes the task from the source list, POSTs, then on success
**silently refetches** (`refetchSilently`, no `isLoading` flip) so the task reappears in its
destination correctly bucketed with the server's new task id; a refetch failure is swallowed (the
move already happened server-side) — only a *move* failure rolls back.

---

## Goal 4a — full task CRUD (tasks MVP)

### Per-task controls
`SortableTask` now renders, inside a `.task-row` (the `.task-item` is a flex **column** so notes
sit below): a complete **checkbox**, an inline-editable title (click → `.task-title-input`,
Enter/blur commits), a notes **expand triangle** (`.notes-toggle`) that reveals a
`.task-notes` textarea (collapsed by default, commits on blur), an `<input type="date">`
(`.task-date`) for arbitrary due dates, and the `⋯` menu. **Same-value edits fire no write** —
the *component* guards (compares trimmed input to the current value) exactly like the g3 group
rename; the hook always writes when called. Empty title is rejected client-side. Every control
except the drag handle calls `e.stopPropagation()` on `onPointerDown` so interacting with it
never starts a drag.

### New-task insertion (insert-from-response)
Per-list "+ add task" → `createTask` inserts an optimistic temp row (id `temp-…`) at the top of
the `NO_DATE` bucket (creating the bucket if absent) with a top rank `(min existing rank) − 1000`,
POSTs `/tasks/{list}`, then **replaces the temp row** with the server task (real id) — the g3
`createGroup` insert-from-response pattern, no reload.

### Date-picker → reschedule
`setDueDate` reuses the g4 `POST …/reschedule` endpoint (no new endpoint): optimistic-remove the
row, POST, then **silently refetch** so it re-buckets correctly — including dates with **no
existing bucket** and the **Overdue** rollup, which drag cannot reach. Clearing the date sends
`due_date: null` (→ `NO_DATE`).

### Overdue rollup (render-only)
The backend rolls all past-due items into one synthetic bucket `{key:"OVERDUE"}` at the top.
It is **not a drag/reschedule target** — `handleDragEnd` returns early if `destBucket.key ===
"OVERDUE"` (its key is not a real date). Past dates are set via the picker, not by dragging in.

### Two undo-toast state machines (`pushActionToast` / `undoActionToast`)
One action-toast is shown at a time; the hook owns the ~5s timer (refs, not component state, so a
deferred write survives re-renders). `pushActionToast(message, onUndo, onExpire)` **commits any
in-flight toast first** (`commitPending`) so a deferred delete can never be orphaned; the unmount
cleanup also flushes. The two machines differ only in their callbacks:
- **Completion** — write fires **immediately** (`PATCH status:"completed"`); `onExpire` is a no-op;
  `onUndo` restores the pre-op snapshot **and** `PATCH status:"needsAction"`. Optimistic remove
  uses `removeTaskFromList` (drops an emptied source group; restored from snapshot on undo).
- **Delete** — `onExpire` fires the held `apiDelete`; `onUndo` just restores the snapshot (**zero
  Google writes**). Delete failure after the window → rollback + error toast.

### Move-menu fixes (the two g4 bugs)
1. **Clip fix:** the `⋯` menu (`.task-menu-popover`) is rendered via `createPortal` to
   `document.body` and positioned `fixed` from the trigger's `getBoundingClientRect()` — it escapes
   the group container's `overflow:hidden`, so it is never truncated for a task low in a tall group
   or a one-item group. The outside-click handler checks **both** the trigger and the portaled
   popover (refs) before closing. The menu also now carries a **Delete** action.
2. **Optimistic destination:** `moveTaskToList` no longer refetches. It captures the moved task,
   removes it from the source, POSTs, then inserts it into the destination **from the move
   response's `new_task_id`** (`insertMovedTask`, placing it by its due date via the client
   `bucketKeyForDue`/`bucketLabelForKey` helpers). Both sides are optimistic; a later refresh
   settles exact ordering.

### List rename + manual refresh
Inline-editable list header (`.list-title` → `.list-title-input`) → `renameList` → `PATCH
/lists/{id}` (component guards same-value). A per-panel `.panel-refresh` button calls `refresh`
(generalised `refetchSilently`) to surface phone-app changes and a recurring task's next instance.

### Subtasks (MVP)
Tasks carry a `parent` field (passed through from Google) but render **flat as standalone**. No
nesting UI; a `parent` task is never dropped or duplicated (it's just another flat row).

---

## Goal 6 — shared DndContext across the pinned pair (cross-list drag)

### One DndContext can now span TWO lists (was: one per list)
g4 lifted the `DndContext` to one-per-list. g6 introduces **`DndListGroup`**, which wraps
**one _or_ two** lists under a single `DndContext` + one shared `handleDragEnd`:
- **`PinnedTasksRow`** renders the two pinned lists (matched by title, `PINNED_LIST_TITLES`)
  inside **one** `DndListGroup` → a task can be dragged *between* them.
- **`OtherTasksSection`** renders each non-pinned list in its **own** single-list `DndListGroup`
  → the cross-list branch never fires, so their behavior is byte-for-byte the g4 per-list one.

`TaskListColumn` is the presentational half (header rename + add-task + `BucketSection`s); it no
longer owns a context. `DndListGroup` is **children-based** (goal 6a): it owns the sensors +
`handleDragEnd` over its `lists` prop, but the CALLER renders the columns as `children`. `DndContext`
emits no DOM, so those children are direct grid items of the parent grid — which lets a
`ResizeHandle` sit as a sibling *between* the two pinned columns. `PinnedTasksRow` builds that grid
(`.top-row-resizable`: 3 columns + 2 handles, default 30/30/40 `fr`, ephemeral widths) and also
holds the scratchpad column (passed in as a `scratchpad` prop). `OtherTasksSection` renders each
list as `<DndListGroup lists={[list]}><TaskListColumn/></DndListGroup>` — single-list, cross-list
branch never fires (g4 behavior). Pinned columns get `compactDates` (per-row date → picker icon
only; date lives in the bucket header via `bucketHeading`).

### Sortable/droppable IDs must be globally unique across the shared context
Task ids (Google strings) and `group-{id}` (global DB PK) are already unique. **Bucket droppable
ids were NOT** — two lists both have a `Today` bucket. Fixed: the droppable id is now
`bucket:{listId}:{bucketKey}` and carries `data:{listId, bucketKey}`; the handler reads the
resolved list+bucket from `over.data.current` (not by parsing the id).

### `handleDragEnd` resolves list first, then dispatches
`findListAndBucket(lists, id)` finds the source list+bucket for `activeId`; the dest list+bucket
come from `over.data.current` (bucket droppable) or `findListAndBucket(overId)`. Then:
- **Group drag** — returns if `crossList` OR dest bucket ≠ src bucket (groups never span lists or
  buckets); else the g3 reorder.
- **`crossList` (dest list ≠ src list) → cross-list move branch:** compute `destGroupId` +
  `dueDate` (`undefined` = preserve when dest bucket key == src bucket key; `null` for `NO_DATE`;
  else the dest date key) + `destPlacement()` rank, then `tasks.moveTaskCrossList(...)`. Dropping
  INTO `OVERDUE` is blocked (past dates go through the picker), same as reschedule.
- **Same list, dest bucket ≠ src bucket** → the g4 reschedule branch (unchanged logic).
- **Same list, same bucket** → the g3 reorder / move-into-group paths (unchanged).

`destPlacement(movedTask)` is the shared "insert hypothetically, then midpoint" rank math used by
both the cross-list and reschedule branches (`computeMidpointRank` for a bucket, `computeGroupTaskRank`
for a group).

### Cross-list move on the frontend (`moveTaskCrossList` in the hook)
Optimistic on both sides: remove from the source immediately; POST `/move` with
`{target_list_id, rank, group_id, due_date?}` (`due_date` omitted when the bucket is unchanged →
backend preserves the source due); on success insert the returned `new_task_id` into the
**destination bucket at the exact drop group+index** via `insertTaskIntoListBucket` (NOT
`insertMovedTask`, which is the menu path that just tops the due-date bucket). Rollback + toast on
failure. The `move` backend write is the g4 layer extended with `due_date`/`group_id` — see
`.claude/rules/writes.md`.

---

## Goal 7a — eager destination inserts + calmer list styling

### Destination is now optimistic too (was: insert-on-response)
g4a/g6 removed the row from the source immediately but only placed it in the **destination** once
the Google write returned — a visible few-seconds gap where the task existed nowhere. g7a closes
that gap for the three "task changes bucket/list" paths by inserting into the destination **inside
the same `setState`** as the source removal:
- **`setDueDate`** (picker) — re-buckets the row client-side via `insertMovedTask(removed, listId,
  updated)` (`bucketKeyForDue` handles NO_DATE / a date / the Overdue rollup, creating the bucket if
  absent), then still `refetchSilently()` to settle exact order.
- **`moveTaskToList`** (menu) and **`moveTaskCrossList`** (pinned-pair drag) — the backend mints a
  **new** task id on the insert leg, so the optimistic row goes in with a `temp-move-{ts}` id and is
  reconciled to `res.new_task_id` on success via **`updateTaskFields`** (patches the id in place,
  whether the row landed standalone or inside a group). Rollback restores the pre-op snapshot.

Keep this shape for any future bucket/list mutation: never let a task blink out while a Google write
is in flight. Groups still never span buckets or lists (unchanged — a deliberate constraint, not a
bug).

### Styling: fewer lines, no group box
Per-row bottom dividers were dropped from tasks and group wrappers (`.panel li.task-item`,
`.panel li.group-item-wrapper { border-bottom: none }` out-specify the base `.panel li`). Group
containers lost their border/radius/fill in favour of a soft-blue **left rail** (`.group-container`
`border-left`), matching the date-urgency left-edge language; the header divider is gone too. The
`⋯` menu still portals to `<body>` (the group's `overflow:hidden` is now `visible`, but the portal
stays — it also dodges the column's `overflow-y:auto` scroll clip).

---

## Goal 14 — thread badge + completion coupling

- **Badge:** `SortableTask` renders `.task-thread-badge` (icon + truncated thread title, full title
  in the tooltip) when `actions.threadFor(task.id)` returns a thread. Like every non-handle control
  it stops `pointerdown`, so it never starts a drag. Click → `actions.onOpenThread(id)`.
- **`useTasksPanel(options)`:** `onTaskCompleted(taskId)` may return a `CompletionLink` — its
  message replaces "Task completed", its `actionLabel`/`onAction` become a second toast button
  (`ActionToast.actionLabel` → `runToastAction`, which closes the window as if it lapsed, then runs
  the action), and its `onUndo` runs inside the existing Undo. `onTaskWritten(taskId)` fires after
  an edit / due / reschedule / move / complete / undo resolves (the pre-move id for moves). Both
  live in a ref; the hook's state machines are otherwise unchanged.

## Bug log — what broke, why, and what fixed it

### 1. Groups can only be dragged once

**Symptom:** First group reorder works; every subsequent attempt leaves the group in place.

**Root cause:** After the first drag the flat SortableContext contains task IDs that are
*inside* groups. When dragging a group on the second attempt, `closestCenter` frequently
reports `over.id` as one of those nested task IDs instead of the adjacent group's header ID.
`findBucketItemIndex(overId, items)` searches only top-level bucket items — it returns `-1`
for nested task IDs, causing an early `return`.

**Fix:** In the group-reorder branch of `handleDragEnd`, resolve `overId` before calling
`findBucketItemIndex`. If `overId` is not a group-prefixed ID, call `findContainer` on it; if
that returns `{type:"group", groupId:N}`, replace `overId` with `"group-N"`.

```ts
let resolvedOverId = overId;
if (!overId.startsWith("group-")) {
  const overContainer = findContainer(overId, items);
  if (overContainer?.type === "group")
    resolvedOverId = `group-${overContainer.groupId}`;
}
const toIndex = findBucketItemIndex(resolvedOverId, items);
```

---

### 2. Dragging an item down requires n+1 steps

**Symptom:** To move item A one position below item B, the user must drag all the way past B
to hover over item C (the item after B) and then drop.

**Root cause:** The original code used `adj = toIdx > fromIdx ? toIdx - 1 : toIdx` before
splicing. Tracing through: drag A (idx 0) over B (idx 1) → `adj = 0` → splice A back at 0 →
no change. The adjustment was an incorrect attempt to compensate for the splice shifting items.

**Fix:** Use `toIdx` directly (arrayMove semantics). After `splice(fromIdx, 1)`, insert at
`toIdx` without adjustment. This matches dnd-kit's own `arrayMove` helper.

```ts
const [moved] = reordered.splice(fromIdx, 1);
reordered.splice(toIdx, 0, moved);                 // no adj
const newRank = computeMidpointRank(reordered, toIdx);
```

Applied in both the bucket-level task reorder path and the group-internal task reorder path.

---

### 3. Cannot drag tasks into groups

**Symptom:** Dragging a standalone task over a group and releasing does nothing (the task
stays standalone). The `handleDragEnd` logic for cross-container moves is correct; the problem
is earlier — collision detection never reports a group item as `over`.

**Root cause:** `closestCenter` computes distance from the drag *center* to the *center* of
every registered rect. When a standalone task is dragged near the top edge of a group, the
standalone task sitting immediately above the group has a center that is geometrically closer
to the cursor than any item inside the group. `over.id` resolves to that standalone task;
`handleDragEnd` sees a same-container reorder; nothing changes.

**Fix:** Replace `closestCenter` with a custom collision strategy:

```ts
const collisionDetection: CollisionDetection = (args) => {
  const hits = pointerWithin(args);          // pointer physically inside a rect?
  return hits.length > 0 ? hits : closestCenter(args);
};
```

`pointerWithin` checks whether the *pointer position* is inside a registered DOM rect. When
the pointer enters the group container div or any task `li` inside it, the group/task is
returned immediately — no center-distance race. `closestCenter` is used only as a fallback
for inter-item gaps where the pointer is outside every rect.

---

### 4. Empty groups have no drop target

**Symptom:** An empty group cannot receive a dragged task because `pointerWithin` (and
`closestCenter`) find no sortable rect inside the group body.

**Root cause:** `.group-tasks` (the `ul` inside the group container) has zero height when
empty, so the group-container div's only meaningful rect area is the header bar — a small
target.

**Fix:** `min-height: 32px` on `.group-tasks` ensures empty groups always present a drag
surface.

---

### 5. Group creation triggers a full data reload

**Symptom:** Creating a group shows a 2-3s loading spinner while the task list refetches from
the Google API.

**Root cause:** `createGroup` called `load()` after the POST, intending to pick up the
server-assigned `id`. The POST response already contains the full group object.

**Fix:** Construct a typed `Group` from the POST response and insert it directly via
`setState`. No network round-trip needed.

```ts
const data = await apiPost<{ id: number; name: string; rank: number | null }>(...);
const grp: Group = { type: "group", id: data.id, name: data.name, rank: data.rank, items: [] };
setState((prev) => updateBucket(prev, tasklistId, bucketKey, (items) => [...items, grp]));
```

---

## Known rough edges (as of goal 3)

The following are not fixed yet. Track them before starting DnD-related work:

- **Drag visual feedback is poor.** There is no `DragOverlay` component. The dragged item is
  dimmed in-place (`opacity: 0.4`) while a ghost follows the cursor. dnd-kit recommends a
  `DragOverlay` for smoother UX; the current approach can cause confusing visual during
  cross-container moves.
- **`verticalListSortingStrategy` shifts all items** during a drag, applying CSS transforms.
  The translated visual positions don't match the DOM rects used for collision. This can make
  it hard to precisely target a drop position, especially for groups with many items.
- **Dropping a standalone task "on" a group** always appends to the end (via the
  `overId.startsWith("group-")` → `destIndexInContainer = grp.items.length` path). There is
  no way to insert at a specific position except by hovering over an existing task inside the
  group. When dropping on the group header or body below the last task, the item goes to the
  end — this is usually fine but can surprise users.
- **Drag to ungroup** (dragging a task from inside a group to standalone) works but the
  insertion index at the bucket level is approximate; it snaps to the nearest standalone task's
  position rather than a precise between-items slot.
- **No touch / mobile support.** `PointerSensor` handles mouse; adding `TouchSensor` from
  `@dnd-kit/core` is the path to mobile drag.
- **Rank precision degrades** over many reorders. Ranks are midpoints; after many operations
  they can converge. A periodic re-normalisation pass (e.g. reset ranks to 1000-spaced
  integers on every full load) would prevent this.

---

## Rank computation rules

Both `computeMidpointRank` (bucket-level items) and `computeGroupTaskRank` (tasks inside a
group) work on the *already-reordered* array: splice the item out, splice it in at `toIdx`,
then compute midpoint between `reordered[toIdx-1]` and `reordered[toIdx+1]`. Items with
`rank === null` are treated as `(index + 1) * 1000` for comparison purposes.

`toIdx` is always the final position in the reordered array, equivalent to dnd-kit's
`arrayMove(items, fromIdx, toIdx)`. **Never apply a `toIdx - 1` adjustment for downward
drags** — that was the source of bug #2 above.
