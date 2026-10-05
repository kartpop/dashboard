import { type MouseEvent, useState } from "react";

import { Sheet } from "../../Sheet";
import { ThreadBadgeIcon } from "./TasksPanel";
import {
  type ListRef,
  type TaskActions,
  type TasksHook,
  type ThreadRef,
  allListRefs,
  bucketHeading,
  bucketUrgencyClass,
  buildTaskActions,
  dueToDateInput,
  istDayKey,
} from "./taskRows";
import type { Task } from "./useTasksPanel";

/** IST weekday (0 = Sun) of today + `offset` days. */
function istWeekday(offset: number): number {
  return new Date(`${istDayKey(offset)}T00:00:00Z`).getUTCDay();
}

function chipDay(key: string): string {
  return new Date(`${key}T00:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// A native date input laid over a chip/icon: a tap opens the phone's own picker
// (`showPicker` where supported; the overlaid input catches the tap elsewhere).
function openPicker(e: MouseEvent<HTMLInputElement>) {
  try {
    e.currentTarget.showPicker?.();
  } catch {
    // Not allowed here (e.g. not a user gesture) — the native tap still works.
  }
}

const CALENDAR = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.6"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <rect x="4" y="5.5" width="16" height="14" rx="2" />
    <path d="M4 10h16M8.5 3.5v4M15.5 3.5v4" />
  </svg>
);

const DOTS = (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <circle cx="5" cy="12" r="1.7" />
    <circle cx="12" cy="12" r="1.7" />
    <circle cx="19" cy="12" r="1.7" />
  </svg>
);

/**
 * One pinned list at phone width (goal 15): the Home tab body for My Tasks or
 * Follow-ups. Same bucket data and the same row writes as the desktop column, but
 * NO drag at all — no DndContext, no sortables, no handle — and groups render
 * read-only (no "+ group", no rename/delete). A row is checkbox · title (≤2 lines)
 * · ⋯; tapping the title or ⋯ opens the task sheet holding the desktop row's
 * actions (reschedule, move, edit, open thread, delete) — nothing new.
 */
export function MobileTaskList({
  title,
  tasks,
  threadLinks,
  onOpenThread,
}: {
  title: string;
  tasks: TasksHook;
  threadLinks?: Map<string, ThreadRef>;
  onOpenThread?: (threadId: number) => void;
}) {
  const [sheetTaskId, setSheetTaskId] = useState<string | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [newDue, setNewDue] = useState("");

  if (tasks.isLoading || tasks.error)
    return (
      <p className={`panel-status${tasks.error ? " panel-error" : ""}`}>
        {tasks.error ?? "Loading…"}
      </p>
    );
  const list = tasks.taskLists.find((l) => l.title === title);
  if (!list)
    return (
      <p className="panel-status panel-error">
        list &lsquo;{title}&rsquo; not found — rename a list to match, or edit
        PINNED_LIST_TITLES.
      </p>
    );

  const allLists = allListRefs(tasks.taskLists);
  const actions = buildTaskActions(
    list,
    allLists,
    tasks,
    threadLinks,
    onOpenThread,
  );

  function submitNew() {
    const t = newTitle.trim();
    if (!t || !list) return;
    void tasks.createTask(list.id, t, { notes: null, dueDate: newDue || null });
    setNewTitle("");
    setNewDue("");
  }

  let sheetTask: Task | null = null;
  if (sheetTaskId)
    for (const b of list.buckets)
      for (const it of b.items) {
        if (it.type === "task" && it.id === sheetTaskId) sheetTask = it;
        if (it.type === "group")
          sheetTask = it.items.find((t) => t.id === sheetTaskId) ?? sheetTask;
      }

  const row = (task: Task) => (
    <MobileTaskRow
      key={task.id}
      task={task}
      actions={actions}
      onOpen={() => setSheetTaskId(task.id)}
    />
  );

  return (
    <div className="m-tasks">
      <form
        className="m-addrow"
        onSubmit={(e) => {
          e.preventDefault();
          submitNew();
        }}
      >
        <span className="m-addrow-plus" aria-hidden="true">
          +
        </span>
        <input
          className="add-task-input"
          placeholder="Add a task"
          aria-label={`Add a task to ${list.title}`}
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          enterKeyHint="done"
        />
        <label className="m-addrow-date">
          {newDue ? chipDay(newDue) : CALENDAR}
          <input
            type="date"
            aria-label="due date"
            value={newDue}
            onClick={openPicker}
            onChange={(e) => setNewDue(e.target.value)}
          />
        </label>
        {newTitle.trim() && (
          <button type="submit" className="add-task-confirm">
            Add
          </button>
        )}
      </form>

      {list.buckets.length === 0 && (
        <p className="panel-status">Nothing here.</p>
      )}
      {list.buckets.map((bucket) => {
        const urgency = bucketUrgencyClass(bucket);
        return (
          <div
            key={bucket.key}
            className={`date-group${urgency ? ` ${urgency}` : ""}`}
          >
            <span className="date-group-label">{bucketHeading(bucket)}</span>
            <ul className="m-task-list">
              {bucket.items.map((item) =>
                item.type === "task" ? (
                  row(item)
                ) : (
                  <li key={`group-${item.id}`} className="m-group">
                    <span className="m-group-name">{item.name}</span>
                    <ul className="m-task-list">{item.items.map(row)}</ul>
                  </li>
                ),
              )}
            </ul>
          </div>
        );
      })}

      {sheetTask && (
        <TaskSheet
          key={sheetTask.id}
          task={sheetTask}
          listTitle={list.title}
          actions={actions}
          onClose={() => setSheetTaskId(null)}
        />
      )}
    </div>
  );
}

function MobileTaskRow({
  task,
  actions,
  onOpen,
}: {
  task: Task;
  actions: TaskActions;
  onOpen: () => void;
}) {
  const thread = actions.threadFor?.(task.id);
  return (
    <li className="m-task">
      <label className="m-task-check">
        <input
          type="checkbox"
          className="task-check"
          checked={task.status === "completed"}
          aria-label={`complete ${task.title}`}
          onChange={() => actions.onComplete(task.id)}
        />
      </label>
      <button type="button" className="m-task-title" onClick={onOpen}>
        <span>{task.title}</span>
      </button>
      {thread && (
        <div className="m-task-meta">
          <button
            type="button"
            className="task-thread-badge"
            aria-label={`Open thread ${thread.title}`}
            onClick={() => actions.onOpenThread?.(thread.id)}
          >
            <ThreadBadgeIcon />
            <span>{thread.title}</span>
          </button>
        </div>
      )}
      <button
        type="button"
        className="m-task-more"
        aria-label={`Actions for ${task.title}`}
        onClick={onOpen}
      >
        {DOTS}
      </button>
    </li>
  );
}

/** The task sheet: the desktop row's actions, stacked for a thumb. */
function TaskSheet({
  task,
  listTitle,
  actions,
  onClose,
}: {
  task: Task;
  listTitle: string;
  actions: TaskActions;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<"menu" | "move" | "edit">("menu");
  const [title, setTitle] = useState(task.title);
  const [notes, setNotes] = useState(task.notes ?? "");
  const thread = actions.threadFor?.(task.id);
  const current = dueToDateInput(task.due);

  const today = istDayKey(0);
  const tomorrow = istDayKey(1);
  // Next week = the coming Monday — or the one after when that is tomorrow
  // (a Sunday's "next week" isn't the same day as its Tomorrow chip).
  const toMonday = (8 - istWeekday(0)) % 7 || 7;
  const nextWeek = istDayKey(toMonday === 1 ? 8 : toMonday);

  function reschedule(date: string | null) {
    onClose();
    if (date !== (current || null)) actions.onSetDueDate(task.id, date);
  }

  function saveEdit() {
    const t = title.trim();
    if (t && t !== task.title) actions.onEditTitle(task.id, t);
    if (notes !== (task.notes ?? "")) actions.onEditNotes(task.id, notes);
    onClose();
  }

  const quick: [string, string, string][] = [
    ["Today", today, chipDay(today)],
    ["Tomorrow", tomorrow, chipDay(tomorrow)],
    ["Next week", nextWeek, chipDay(nextWeek)],
  ];

  return (
    <Sheet label="Task actions" onClose={onClose}>
      <h3 className="sheet-title sheet-title--wrap">{task.title}</h3>
      <div className="sheet-sub">
        {listTitle}
        {thread && ` · thread: ${thread.title}`}
      </div>

      {mode === "edit" ? (
        <form
          className="sheet-form"
          onSubmit={(e) => {
            e.preventDefault();
            saveEdit();
          }}
        >
          <input
            className="task-title-input"
            aria-label="Title"
            value={title}
            autoFocus
            onChange={(e) => setTitle(e.target.value)}
          />
          <textarea
            className="task-notes"
            placeholder="Add notes"
            aria-label="Notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
          <div className="sheet-form-actions">
            <button type="button" onClick={() => setMode("menu")}>
              Cancel
            </button>
            <button type="submit" className="sheet-primary">
              Save
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="sheet-chips" role="group" aria-label="Reschedule">
            {quick.map(([label, key, sub]) => (
              <button
                key={label}
                type="button"
                className={`sheet-chip${current === key ? " sheet-chip--cur" : ""}`}
                onClick={() => reschedule(key)}
              >
                {label}
                <small>{sub}</small>
              </button>
            ))}
            <label className="sheet-chip sheet-chip--pick">
              Pick…
              <small>{current ? chipDay(current) : "date"}</small>
              <input
                type="date"
                aria-label="Pick a due date"
                value={current}
                onClick={openPicker}
                onChange={(e) => reschedule(e.target.value || null)}
              />
            </label>
          </div>

          <div className="sheet-list">
            <button
              type="button"
              aria-expanded={mode === "move"}
              onClick={() => setMode(mode === "move" ? "menu" : "move")}
            >
              Move to list…
            </button>
            {mode === "move" &&
              (actions.otherLists.length === 0 ? (
                <span className="sheet-list-note">No other lists</span>
              ) : (
                actions.otherLists.map((l: ListRef) => (
                  <button
                    key={l.id}
                    type="button"
                    className="sheet-list-sub move-to-list-option"
                    onClick={() => {
                      onClose();
                      actions.onMoveToList(task.id, l.id);
                    }}
                  >
                    {l.title}
                  </button>
                ))
              ))}
            <button type="button" onClick={() => setMode("edit")}>
              Edit title / notes
            </button>
            {thread && (
              <button
                type="button"
                onClick={() => {
                  onClose();
                  actions.onOpenThread?.(thread.id);
                }}
              >
                Open thread
              </button>
            )}
            <button
              type="button"
              className="sheet-list--danger task-menu-delete"
              onClick={() => {
                onClose();
                actions.onDelete(task.id);
              }}
            >
              Delete
            </button>
          </div>
        </>
      )}
    </Sheet>
  );
}
