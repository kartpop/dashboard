import type { Bucket, TaskList, useTasksPanel } from "./useTasksPanel";

// Row-level helpers shared by the desktop columns (TasksPanel.tsx) and the phone
// list (goal 15, MobileTaskList.tsx). A plain module, so the component files keep
// exporting only components (react-refresh).

// A minimal {id,title} reference to every list, for the move-to-list picker.
export interface ListRef {
  id: string;
  title: string;
}

// Per-task action handlers, bundled so they thread cleanly down to every task
// (standalone or grouped) without a long prop list at each level. Shared with the
// phone list (goal 15, MobileTaskList) so both rows fire the same writes.
export interface TaskActions {
  otherLists: ListRef[];
  onMoveToList: (taskId: string, targetListId: string) => void;
  onComplete: (taskId: string) => void;
  onEditTitle: (taskId: string, title: string) => void;
  onEditNotes: (taskId: string, notes: string) => void;
  onSetDueDate: (taskId: string, date: string | null) => void;
  onDelete: (taskId: string) => void;
  // Goal 14: the thread whose live next step this task is (badge), if any.
  threadFor?: (taskId: string) => ThreadRef | undefined;
  onOpenThread?: (threadId: number) => void;
}

// A thread linked to a task row, as DashboardPage derives it from the threads
// payload (this panel never imports the threads panel).
export interface ThreadRef {
  id: number;
  title: string;
}

/** RFC3339 UTC due → "YYYY-MM-DD" (IST) for an <input type="date">; "" if unset. */
export function dueToDateInput(due: string | null): string {
  if (!due) return "";
  const ist = new Date(new Date(due).getTime() + 5.5 * 3600 * 1000);
  return ist.toISOString().slice(0, 10);
}

/** "YYYY-MM-DD" (IST) for now + `offsetDays`. Mirrors the backend/hook bucketing. */
export function istDayKey(offsetDays: number): string {
  const ms = Date.now() + 5.5 * 3600 * 1000 + offsetDays * 86_400_000;
  return new Date(ms).toISOString().slice(0, 10);
}

/**
 * The bucket header text. Since the columns are grouped by date, the individual
 * rows drop their dates (see `.task-column--compact-dates`) — so the Today /
 * Tomorrow headers carry the concrete date + weekday instead (e.g.
 * "Tomorrow — Wednesday, 08/07/2026"). Other buckets keep the server label.
 */
export function bucketHeading(bucket: Bucket): string {
  if (bucket.key === "NO_DATE" || bucket.key === "OVERDUE") return bucket.label;
  const prefix =
    bucket.key === istDayKey(0)
      ? "Today"
      : bucket.key === istDayKey(1)
        ? "Tomorrow"
        : null;
  if (!prefix) return bucket.label;
  const d = new Date(`${bucket.key}T00:00:00Z`);
  const weekday = d.toLocaleDateString(undefined, {
    weekday: "long",
    timeZone: "UTC",
  });
  const dmy = d.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });
  return `${prefix} — ${weekday}, ${dmy}`;
}

/**
 * Date-urgency cue class for a bucket (goal 7a): Overdue / Today / Tomorrow get a
 * faint header tint + a 3px colored left edge; every later bucket and NO_DATE stay
 * plain so the near-term dates read at a glance. Colors live in CSS.
 */
export function bucketUrgencyClass(bucket: Bucket): string {
  if (bucket.key === "OVERDUE") return "bucket--overdue";
  if (bucket.key === istDayKey(0)) return "bucket--today";
  if (bucket.key === istDayKey(1)) return "bucket--tomorrow";
  return "";
}

export type TasksHook = ReturnType<typeof useTasksPanel>;

/** One list's row actions, wired to the lifted tasks hook (desktop column + phone list). */
export function buildTaskActions(
  list: TaskList,
  allLists: ListRef[],
  tasks: TasksHook,
  threadLinks?: Map<string, ThreadRef>,
  onOpenThread?: (threadId: number) => void,
): TaskActions {
  return {
    otherLists: allLists.filter((l) => l.id !== list.id),
    onMoveToList: (taskId, targetListId) =>
      tasks.moveTaskToList(list.id, taskId, targetListId),
    onComplete: (taskId) => tasks.completeTask(list.id, taskId),
    onEditTitle: (taskId, title) =>
      tasks.editTaskField(list.id, taskId, { title }),
    onEditNotes: (taskId, notes) =>
      tasks.editTaskField(list.id, taskId, { notes }),
    onSetDueDate: (taskId, date) => tasks.setDueDate(list.id, taskId, date),
    onDelete: (taskId) => tasks.deleteTask(list.id, taskId),
    threadFor: threadLinks ? (taskId) => threadLinks.get(taskId) : undefined,
    onOpenThread,
  };
}

export function allListRefs(taskLists: TaskList[]): ListRef[] {
  return taskLists.map((l) => ({ id: l.id, title: l.title }));
}

/** Open tasks in a list (grouped ones included) — the phone Home tab's count. */
export function countOpenTasks(list: TaskList | undefined): number {
  if (!list) return 0;
  let n = 0;
  for (const b of list.buckets)
    for (const it of b.items) n += it.type === "task" ? 1 : it.items.length;
  return n;
}
