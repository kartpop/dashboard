import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { AgendaRow } from "./panels/calendar/AgendaRow";
import { CalendarStrip } from "./panels/calendar/CalendarStrip";
import { useCalendarStrip } from "./panels/calendar/useCalendarStrip";
import { CapturePanel } from "./panels/scratch/CapturePanel";
import type { CaptureHook } from "./panels/scratch/useCapture";
import { MobileTaskList } from "./panels/tasks/MobileTaskList";
import {
  PINNED_LIST_TITLES,
  PinnedTasksRow,
  TasksToasts,
} from "./panels/tasks/TasksPanel";
import { countOpenTasks } from "./panels/tasks/taskRows";
import { useTasksPanel } from "./panels/tasks/useTasksPanel";
import { ThreadsPanel } from "./panels/threads/ThreadsPanel";
import {
  type ThreadsHook,
  useThreadsPanel,
} from "./panels/threads/useThreadsPanel";
import { useIsMobile } from "./useIsMobile";

/**
 * Home view: the today's-dashboard surface (unchanged). Account controls (settings,
 * avatar, sign-out) moved to the nav rail's bottom-left in goal 11, so the header is
 * just the brand + calendar strip now. At phone width (goal 15) the same lifted
 * state renders through `MobileHome` instead: a sticky header over four tabs.
 */
export function DashboardPage({ capture }: { capture: CaptureHook }) {
  const isMobile = useIsMobile();
  const { setOnRouted } = capture;
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
    // A capture that routed to a Google task refreshes the columns (the capture
    // state lives in AppShell, which doesn't know the tasks hook).
    setOnRouted(tasks.refresh);
  }, [tasks.refresh, setOnRouted]);

  if (isMobile)
    return <MobileHome tasks={tasks} threads={threads} capture={capture} />;

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
        scratchpad={<CapturePanel capture={capture} onRouted={tasks.refresh} />}
      />
      <TasksToasts tasks={tasks} />
    </main>
  );
}

type TasksHook = ReturnType<typeof useTasksPanel>;
type Tab = "mine" | "follow" | "threads" | "scratch";

const DAY_ICON_PREV = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m15 6-6 6 6 6" />
  </svg>
);
const DAY_ICON_NEXT = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.8"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="m9 6 6 6-6 6" />
  </svg>
);
const REFRESH_ICON = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.7"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v4.5h-4.5" />
  </svg>
);

/** "Sun, 4 Oct" for the header's day nav. */
function fmtDay(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString([], {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

/**
 * Home at phone width (goal 15): a sticky header (brand · ‹ day › · refresh, the
 * agenda row, the section tabs) over ONE section at a time. Every pane stays
 * mounted — switching toggles `hidden` — so a scratch draft, an expanded thread, a
 * half-typed task and each tab's scroll position survive a switch (the same reason
 * `.view-pane` keeps rail views mounted). The active tab is ephemeral.
 */
function MobileHome({
  tasks,
  threads,
  capture,
}: {
  tasks: TasksHook;
  threads: ThreadsHook;
  capture: CaptureHook;
}) {
  const cal = useCalendarStrip();
  const [tab, setTab] = useState<Tab>("mine");

  // A thread request from outside the Threads tab (a task's thread chip, "Open
  // thread", the completion toast's Show thread / Set next step) switches to the
  // Threads tab first; ThreadsPanel then focuses the thread as on desktop.
  const [seenNonce, setSeenNonce] = useState(
    () => threads.request?.nonce ?? null,
  );
  const req = threads.request;
  if (req && req.nonce !== seenNonce) {
    setSeenNonce(req.nonce);
    if (req.mode !== "flash") setTab("threads");
  }

  // The page scrolls (not a pane), so remember each tab's offset and put it back
  // on return.
  const scrollPos = useRef<Partial<Record<Tab, number>>>({});
  useEffect(() => {
    const onScroll = () => {
      scrollPos.current[tab] = window.scrollY;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [tab]);
  useLayoutEffect(() => {
    window.scrollTo(0, scrollPos.current[tab] ?? 0);
  }, [tab]);

  // The sticky header's height, as --m-head-h, so the Scratch tab can fill exactly
  // the space between it and the bottom bar (like the desktop column).
  const headRef = useRef<HTMLElement>(null);
  const homeRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const head = headRef.current;
    const home = homeRef.current;
    if (!head || !home) return;
    const sync = () =>
      home.style.setProperty("--m-head-h", `${head.offsetHeight}px`);
    sync();
    const ro = new ResizeObserver(sync);
    ro.observe(head);
    return () => ro.disconnect();
  }, []);

  const [mineTitle, followTitle] = PINNED_LIST_TITLES;
  const listByTitle = (t: string) => tasks.taskLists.find((l) => l.title === t);
  const tabs: [Tab, string, number | null][] = [
    ["mine", mineTitle, countOpenTasks(listByTitle(mineTitle))],
    ["follow", followTitle, countOpenTasks(listByTitle(followTitle))],
    ["threads", "Threads", threads.threads.filter((t) => !t.archived).length],
    ["scratch", "Scratch", null],
  ];

  const refresh = () => {
    if (tab === "threads") threads.refresh();
    else if (tab === "scratch") void capture.scratch.refresh();
    else tasks.refresh();
  };
  const openThread = (id: number) => threads.requestThread(id, "focus");

  return (
    <main className="m-home" ref={homeRef}>
      <header className="m-head" ref={headRef}>
        <div className="m-head-row">
          <img className="m-logo" src="/logo-mark.svg" alt="" />
          <div className="m-daynav">
            <button
              type="button"
              onClick={() => cal.shiftDay(-1)}
              aria-label="Previous day"
            >
              {DAY_ICON_PREV}
            </button>
            <button
              type="button"
              className="m-day"
              onClick={cal.goToday}
              title={cal.isToday ? undefined : "Back to today"}
            >
              {fmtDay(cal.viewedDate)}
              {!cal.isToday && <small>Today ↺</small>}
            </button>
            <button
              type="button"
              onClick={() => cal.shiftDay(1)}
              aria-label="Next day"
            >
              {DAY_ICON_NEXT}
            </button>
          </div>
          <button
            type="button"
            className="m-refresh"
            onClick={refresh}
            aria-label="Refresh"
          >
            {REFRESH_ICON}
          </button>
        </div>
        <AgendaRow cal={cal} />
        <div className="m-tabs" role="tablist" aria-label="Sections">
          {tabs.map(([key, label, count]) => (
            <button
              key={key}
              type="button"
              role="tab"
              id={`m-tab-${key}`}
              aria-selected={tab === key}
              aria-controls={`m-pane-${key}`}
              className="m-tab"
              onClick={() => setTab(key)}
            >
              {label}
              {count !== null && <small>{count}</small>}
            </button>
          ))}
        </div>
      </header>

      <div
        className="m-pane"
        id="m-pane-mine"
        role="tabpanel"
        aria-labelledby="m-tab-mine"
        hidden={tab !== "mine"}
      >
        <MobileTaskList
          title={mineTitle}
          tasks={tasks}
          threadLinks={threads.links}
          onOpenThread={openThread}
        />
      </div>
      <div
        className="m-pane"
        id="m-pane-follow"
        role="tabpanel"
        aria-labelledby="m-tab-follow"
        hidden={tab !== "follow"}
      >
        <MobileTaskList
          title={followTitle}
          tasks={tasks}
          threadLinks={threads.links}
          onOpenThread={openThread}
        />
      </div>
      <div
        className="m-pane"
        id="m-pane-threads"
        role="tabpanel"
        aria-labelledby="m-tab-threads"
        hidden={tab !== "threads"}
      >
        <ThreadsPanel threads={threads} mobile />
      </div>
      <div
        className="m-pane"
        id="m-pane-scratch"
        role="tabpanel"
        aria-labelledby="m-tab-scratch"
        hidden={tab !== "scratch"}
      >
        <CapturePanel capture={capture} onRouted={tasks.refresh} mobile />
      </div>
      <TasksToasts tasks={tasks} />
    </main>
  );
}
