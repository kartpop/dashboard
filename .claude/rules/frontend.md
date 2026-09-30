---
paths: ["frontend/**"]
---

# Frontend conventions (React)

- Each dashboard surface (Tasks, Calendar, Drive, overlay) is a self-contained panel component
  under `frontend/src/panels/`, composed on a single dashboard page — avoid cross-panel imports.
- Local UI state stays in the component; data fetched from the backend is owned by a thin
  per-panel hook (e.g. `useTasksPanel`) so panels can be developed and tested independently.
- No global state library unless a concrete cross-panel sharing need appears — prefer lifting
  state to the dashboard page over adding one.
- "Self-contained panel" means no panel-to-panel imports — it doesn't forbid shared leaf
  utilities. Cross-cutting helpers with no panel-specific knowledge (the backend fetch
  wrapper `api.ts`, `formatDate.ts`) live at `frontend/src/` root and may be imported by any
  panel hook.
- **MVP layout (goal 6):** `DashboardPage` renders `PinnedTasksRow` (the full-width top row) then
  `OtherTasksSection` (collapsed, ephemeral state) and the calendar. `PinnedTasksRow` owns ONE
  resizable grid holding all three top-row columns — My Tasks | Follow-ups (the pinned pair, under
  one shared `DndContext` for cross-list drag) + the scratchpad. The scratchpad is passed in as a
  `scratchpad` **prop** (a `<CapturePanel>` node built by `DashboardPage`) so `TasksPanel.tsx`
  imports no sibling panel. `DndListGroup` is now **children-based** (owns sensors + `handleDragEnd`
  over its `lists`; the caller renders the columns) so a `ResizeHandle` can sit as a grid sibling
  between the two pinned columns. Column widths are `fr` fractions (default 30/30/40) in ephemeral
  state — dragging a handle shifts width between the two adjacent columns; below a breakpoint the
  grid stacks and handles hide. No width **persistence** / `ui_prefs` / visibility chooser yet
  (deferred to goal 9). Pinned columns pass `compactDates` → the per-row date collapses to just the
  calendar-picker icon (the bucket header carries the date; Today/Tomorrow headers show weekday +
  `dd/mm/yyyy` via `bucketHeading`). The pinned lists are matched **by title** against the live
  Google lists via the static `PINNED_LIST_TITLES` constant (exported from `TasksPanel.tsx`); a
  missing title renders an empty-column hint, not a crash. Tasks-surface state is one lifted
  `useTasksPanel` shared by `PinnedTasksRow` / `OtherTasksSection` / `TasksToasts`; the write toasts
  are rendered once (`position: fixed`).
- **Two-row top area (goal 14):** `PinnedTasksRow`'s grid grew a second row rather than a wrapper
  (so column resizing keeps working): **left block** = My Tasks | handle | Follow-ups on top
  and **Threads** spanning those three tracks below, split by `--r0`/`--r1` (default 45/55) with a
  draggable row handle (`.resize-handle--rows`, its own `--handle` track, clamped 20–80%,
  ephemeral) between them; **right column** = handle + Scratchpad spanning all three rows. Threads is passed in as a `threads`
  node (like `scratchpad`). Every grid item is placed explicitly by class (`.pinned-slot-0/1`,
  `.resize-handle--pair`, `.resize-handle--rows`, `.threads-panel`, `.resize-handle--scratch`,
  `.capture-panel`); the DOM
  order is the stacked order at ≤1080px (My Tasks, Follow-ups, Threads, Scratchpad), where the
  placements reset to `auto`. Tasks + threads state are both lifted to `DashboardPage` (see
  `.claude/rules/threads.md` for the coupling callbacks). The scratchpad's RECENT rests at header +
  2 rows (`--recent-rest`); dragging its split sets `data-split` and hands sizing back to
  `--editor-ratio`.
- **Optimistic drag/group convention (goal 3+):** All drag and group mutations are optimistic.
  The component computes the new rank from its current local state (midpoint of neighbours)
  and passes it to the hook. The hook applies the state update inside `setState`, then fires
  `apiPatch`/`apiPost`/`apiDelete` *outside* `setState` without awaiting. Never do a full
  reload (`load()`) after a drag op. A single drag always produces exactly one PATCH
  (rank ± group_id). For the DnD implementation details, known rough edges, and bug history
  see `.claude/rules/tasks-panel.md` (auto-loads when editing `frontend/src/panels/tasks/**`).
- **Deferred-write undo toasts (g4a delete, g7a capture) — a shared shape, not an abstraction.**
  Two surfaces now hold a write behind a ~5s "Undo" toast, and they share the same skeleton (kept
  local to each, deliberately — no forced helper): the optimistic UI change applies immediately; the
  held payload + timer live in **refs** (survive re-renders, so the deferred write isn't lost); a
  `commitPending()` fires the held write when the window lapses **and** is called first when a new
  action supersedes the pending one (one toast at a time); an unmount effect flushes any still-held
  write. The two differ only in what Undo does: **delete** (in `useTasksPanel`) restores a snapshot
  and never sends the `DELETE` (zero Google writes); **capture** (in `CapturePanel`) restores the
  text into the editor — prepending above anything typed during the window — and never sends the
  `POST /scratch` (zero backend writes; the append-only store has no delete endpoint by design). If a
  third deferred toast appears, *then* consider extracting; two is not enough to abstract.
