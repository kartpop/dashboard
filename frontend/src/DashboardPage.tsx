import { useEffect, useRef } from "react";

import { CalendarStrip } from "./panels/calendar/CalendarStrip";
import { CapturePanel } from "./panels/scratch/CapturePanel";
import { PinnedTasksRow, TasksToasts } from "./panels/tasks/TasksPanel";
import { useTasksPanel } from "./panels/tasks/useTasksPanel";
import { ThreadsPanel } from "./panels/threads/ThreadsPanel";
import { useThreadsPanel } from "./panels/threads/useThreadsPanel";

/**
 * Home view: the today's-dashboard surface (unchanged). Account controls (settings,
 * avatar, sign-out) moved to the nav rail's bottom-left in goal 11, so the header is
 * just the brand + calendar strip now.
 */
export function DashboardPage() {
  // Tasks and threads state are both lifted here (goal 14) so the two panels can
  // couple — a completed next-step task flips its thread, a thread write refreshes
  // the task columns — without either panel importing the other.
  const tasksRefreshRef = useRef<() => void>(() => {});
  const threads = useThreadsPanel({
    onTasksChanged: () => tasksRefreshRef.current(),
  });
  const tasks = useTasksPanel({
    onTaskCompleted: (taskId) => {
      const hit = threads.markLinkedCompleted(taskId);
      if (!hit) return null;
      // Siblings still open (goal 14a): no "What's next?", just a way to the thread.
      if (hit.remaining > 0)
        return {
          message: `Logged “${hit.label}” in ${hit.title}. ${hit.remaining} still open.`,
          actionLabel: "Show thread",
          onAction: () => threads.requestThread(hit.threadId, "focus"),
          onUndo: () => threads.revertLinkedCompleted(hit.threadId),
        };
      return {
        message: `Logged “${hit.label}” in ${hit.title}. What’s next?`,
        actionLabel: "Set next step",
        onAction: () =>
          threads.requestThread(hit.threadId, "next", hit.list ?? undefined),
        onUndo: () => threads.revertLinkedCompleted(hit.threadId),
      };
    },
    // Tasks-panel edits of a linked task (title, notes, due, move, complete) land
    // in the thread on a refresh rather than duplicated optimistic logic.
    onTaskWritten: (taskId) => {
      if (threads.isLinked(taskId)) threads.refresh();
    },
  });
  useEffect(() => {
    tasksRefreshRef.current = tasks.refresh;
  }, [tasks.refresh]);

  return (
    <main className="dashboard">
      {/* Header row: title + today's calendar strip (goal 7b). The brand mark lives
          only on the nav rail now, so the title stands alone here. */}
      <header className="dashboard-header">
        <h1>Dashboard</h1>
        <CalendarStrip />
      </header>
      {/* One resizable grid: My Tasks | Follow-ups over Threads on the left, the
          Scratchpad spanning both rows on the right (goal 14). The pinned pair
          shares one DndContext; threads + scratchpad are passed in as nodes so no
          panel imports another. */}
      <PinnedTasksRow
        tasks={tasks}
        threads={<ThreadsPanel threads={threads} />}
        threadLinks={threads.links}
        onOpenThread={(id) => threads.requestThread(id, "focus")}
        scratchpad={<CapturePanel onRouted={tasks.refresh} />}
      />
      <TasksToasts tasks={tasks} />
    </main>
  );
}
