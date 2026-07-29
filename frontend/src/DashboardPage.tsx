import { CalendarStrip } from "./panels/calendar/CalendarStrip";
import { CapturePanel } from "./panels/scratch/CapturePanel";
import { PinnedTasksRow, TasksToasts } from "./panels/tasks/TasksPanel";
import { useTasksPanel } from "./panels/tasks/useTasksPanel";

/**
 * Home view: the today's-dashboard surface (unchanged). Account controls (settings,
 * avatar, sign-out) moved to the nav rail's bottom-left in goal 11, so the header is
 * just the brand + calendar strip now.
 */
export function DashboardPage() {
  // Lifted here (not owned inside a single TasksPanel) so the scratchpad can
  // refresh the task columns after routing/confirming creates a Google task, and
  // so the pinned pair + toasts read one shared state.
  const tasks = useTasksPanel();

  return (
    <main className="dashboard">
      {/* Header row: title + today's calendar strip (goal 7b). */}
      <header className="dashboard-header">
        <div className="dashboard-brand">
          <img className="brand-logo" src="/logo-mark.svg" alt="" />
          <h1>Dashboard</h1>
        </div>
        <CalendarStrip />
      </header>
      {/* Full-width, resizable top row: My Tasks | Follow-ups | Scratchpad (goal
          6). The pinned pair shares one DndContext (cross-list drag); the
          scratchpad is passed in as the third column so no panel imports another. */}
      <PinnedTasksRow
        tasks={tasks}
        scratchpad={<CapturePanel onRouted={tasks.refresh} />}
      />
      <TasksToasts tasks={tasks} />
    </main>
  );
}
