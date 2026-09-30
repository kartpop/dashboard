import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "../../api";

export type ListKey = "mine" | "follow";

export interface Step {
  id: number;
  kind: "done" | "next";
  label: string;
  note: string;
  occurred_on: string | null; // YYYY-MM-DD (IST), done steps
  list: ListKey | null; // next steps: which pinned list the task is in
  due: string | null; // YYYY-MM-DD (IST), next steps
  tasklist_id: string | null;
  task_id: string | null;
  via: ListKey | null; // done steps that came from a completed task
}

export interface Thread {
  id: number;
  title: string;
  archived: boolean;
  created_at: string;
  last_moved_on: string;
  steps: Step[];
}

export interface ThreadLink {
  id: number;
  title: string;
}

// A request from outside the panel (a task badge click, the tasks toast's "Set next
// step"): the panel consumes it in an effect. `nonce` makes repeats distinct.
export interface ThreadRequest {
  threadId: number;
  mode: "focus" | "flash" | "next";
  list?: ListKey;
  nonce: number;
}

export interface ThreadsToast {
  message: string;
  actionLabel?: string;
  kind: "action" | "error";
}

export interface StepPatch {
  label?: string;
  note?: string;
  occurred_on?: string;
  due?: string | null;
  list?: ListKey;
}

interface ThreadsState {
  threads: Thread[];
  isLoading: boolean;
  error: string | null;
  toast: ThreadsToast | null;
  request: ThreadRequest | null;
}

export interface ThreadsPanelOptions {
  // Fired after a threads write that changed a Google task (create / edit / move /
  // complete), so the tasks columns can pick it up. Owned by DashboardPage.
  onTasksChanged?: () => void;
}

// Same cadence as the tasks panel; a tick is skipped while a form/popover is open.
const POLL_MS = 45_000;
const TOAST_MS = 6000;

export const LIST_LABEL: Record<ListKey, string> = {
  mine: "My Tasks",
  follow: "Follow-ups",
};

/** "YYYY-MM-DD" in IST for now + `offsetDays` (mirrors the backend's today_ist). */
export function istDayKey(offsetDays = 0): string {
  const ms = Date.now() + 5.5 * 3600 * 1000 + offsetDays * 86_400_000;
  return new Date(ms).toISOString().slice(0, 10);
}

export function nextOf(t: Thread): Step | null {
  return t.steps.find((s) => s.kind === "next") ?? null;
}

// Temp ids for optimistic rows are negative so they never collide with DB ids.
let tempSeq = 0;
function tempId(): number {
  tempSeq += 1;
  return -tempSeq;
}

function replaceThread(threads: Thread[], id: number, next: Thread): Thread[] {
  return threads.map((t) => (t.id === id ? next : t));
}

function mapThread(
  threads: Thread[],
  id: number,
  fn: (t: Thread) => Thread,
): Thread[] {
  return threads.map((t) => (t.id === id ? fn(t) : t));
}

function withLastMoved(t: Thread): Thread {
  const dates = t.steps
    .filter((s) => s.kind === "done" && s.occurred_on)
    .map((s) => s.occurred_on as string)
    .sort();
  return {
    ...t,
    last_moved_on: dates.length
      ? dates[dates.length - 1]
      : t.created_at.slice(0, 10),
  };
}

/** Flip a thread's next step to done (optimistic mirror of the backend flip). */
function flipNextDone(t: Thread, stepId: number): Thread {
  return withLastMoved({
    ...t,
    steps: t.steps.map((s) =>
      s.id === stepId && s.kind === "next"
        ? {
            ...s,
            kind: "done",
            occurred_on: istDayKey(0),
            via: s.list,
            list: null,
            due: null,
          }
        : s,
    ),
  });
}

export function useThreadsPanel(options: ThreadsPanelOptions = {}) {
  const [state, setState] = useState<ThreadsState>({
    threads: [],
    isLoading: true,
    error: null,
    toast: null,
    request: null,
  });

  // Latest state + options for callbacks that must read synchronously (the lifted
  // tasks→threads coupling runs inside the tasks hook's event handlers).
  const threadsRef = useRef<Thread[]>([]);
  const optionsRef = useRef(options);
  useEffect(() => {
    threadsRef.current = state.threads;
  }, [state.threads]);
  useEffect(() => {
    optionsRef.current = options;
  });

  const tasksChanged = useCallback(() => {
    optionsRef.current.onTasksChanged?.();
  }, []);

  // ── Toast (one at a time; the action callback lives in a ref) ───────────────
  const toastTimerRef = useRef<number | null>(null);
  const toastActionRef = useRef<(() => void) | null>(null);

  const dismissToast = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
      toastTimerRef.current = null;
    }
    toastActionRef.current = null;
    setState((s) => ({ ...s, toast: null }));
  }, []);

  const showToast = useCallback(
    (
      message: string,
      action?: { label: string; run: () => void },
      kind: ThreadsToast["kind"] = "action",
    ) => {
      if (toastTimerRef.current !== null)
        window.clearTimeout(toastTimerRef.current);
      toastActionRef.current = action?.run ?? null;
      setState((s) => ({
        ...s,
        toast: { message, actionLabel: action?.label, kind },
      }));
      toastTimerRef.current = window.setTimeout(() => {
        toastTimerRef.current = null;
        toastActionRef.current = null;
        setState((s) => ({ ...s, toast: null }));
      }, TOAST_MS);
    },
    [],
  );

  const runToastAction = useCallback(() => {
    const run = toastActionRef.current;
    dismissToast();
    run?.();
  }, [dismissToast]);

  useEffect(
    () => () => {
      if (toastTimerRef.current !== null)
        window.clearTimeout(toastTimerRef.current);
    },
    [],
  );

  const fail = useCallback(
    (what: string, snapshot: Thread[] | null, err: unknown) => {
      if (snapshot) setState((s) => ({ ...s, threads: snapshot }));
      showToast(
        `${what} failed: ${(err as Error).message}`,
        undefined,
        "error",
      );
    },
    [showToast],
  );

  // ── Loading + polling ────────────────────────────────────────────────────────
  const load = useCallback(async () => {
    const data = await apiGet<{ threads: Thread[] }>("/threads");
    setState((s) => ({
      ...s,
      threads: data.threads,
      isLoading: false,
      error: null,
    }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    apiGet<{ threads: Thread[] }>("/threads")
      .then((data) => {
        if (!cancelled)
          setState((s) => ({ ...s, threads: data.threads, isLoading: false }));
      })
      .catch((err: Error) => {
        if (!cancelled)
          setState((s) => ({ ...s, isLoading: false, error: err.message }));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The panel holds polling while a form or popover is open (a refetch would
  // clobber the thread being edited).
  const holdRef = useRef(false);
  const setHold = useCallback((on: boolean) => {
    holdRef.current = on;
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (holdRef.current) return;
      load().catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const refresh = useCallback(() => {
    load().catch((err: Error) =>
      showToast(`Refresh failed: ${err.message}`, undefined, "error"),
    );
  }, [load, showToast]);

  // ── Requests from outside the panel ─────────────────────────────────────────
  const requestThread = useCallback(
    (threadId: number, mode: ThreadRequest["mode"], list?: ListKey) => {
      setState((s) => ({
        ...s,
        request: { threadId, mode, list, nonce: Date.now() + Math.random() },
      }));
    },
    [],
  );

  const clearRequest = useCallback(() => {
    setState((s) => ({ ...s, request: null }));
  }, []);

  // ── Thread CRUD ──────────────────────────────────────────────────────────────

  /** Create a thread; resolves to its real id (the panel opens Log update on it). */
  const createThread = useCallback(
    async (title: string): Promise<number | null> => {
      const trimmed = title.trim();
      if (!trimmed) return null;
      const temp: Thread = {
        id: tempId(),
        title: trimmed,
        archived: false,
        created_at: new Date().toISOString(),
        last_moved_on: istDayKey(0),
        steps: [],
      };
      setState((s) => ({ ...s, threads: [...s.threads, temp] }));
      try {
        const created = await apiPost<Thread>("/threads", { title: trimmed });
        setState((s) => ({
          ...s,
          threads: replaceThread(s.threads, temp.id, created),
        }));
        return created.id;
      } catch (err) {
        setState((s) => ({
          ...s,
          threads: s.threads.filter((t) => t.id !== temp.id),
        }));
        fail("Create", null, err);
        return null;
      }
    },
    [fail],
  );

  const setArchived = useCallback(
    (threadId: number, archived: boolean) => {
      const snapshot = threadsRef.current;
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (t) => ({ ...t, archived })),
      }));
      return apiPatch<Thread>(`/threads/${threadId}`, { archived })
        .then((t) =>
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, t),
          })),
        )
        .catch((err) => fail(archived ? "Archive" : "Restore", snapshot, err));
    },
    [fail],
  );

  /** Archive with an Undo toast. Never touches the linked Google task. */
  const archiveThread = useCallback(
    (threadId: number) => {
      const t = threadsRef.current.find((x) => x.id === threadId);
      if (!t) return;
      void setArchived(threadId, true);
      showToast(
        `Archived ${t.title}${nextOf(t) ? ". Its open task stays in Google Tasks." : "."}`,
        {
          label: "Undo",
          run: () => {
            void setArchived(threadId, false);
            requestThread(threadId, "flash");
          },
        },
      );
    },
    [setArchived, showToast, requestThread],
  );

  const restoreThread = useCallback(
    (threadId: number) => void setArchived(threadId, false),
    [setArchived],
  );

  // ── Steps ────────────────────────────────────────────────────────────────────

  /** Log a done step (dated today unless given), inserted before the next step. */
  const logStep = useCallback(
    (threadId: number, label: string, note?: string) => {
      const trimmed = label.trim();
      if (!trimmed) return;
      const snapshot = threadsRef.current;
      const step: Step = {
        id: tempId(),
        kind: "done",
        label: trimmed,
        note: note ?? "",
        occurred_on: istDayKey(0),
        list: null,
        due: null,
        tasklist_id: null,
        task_id: null,
        via: null,
      };
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (t) => {
          const i = t.steps.findIndex((x) => x.kind === "next");
          const steps = [...t.steps];
          steps.splice(i < 0 ? steps.length : i, 0, step);
          return withLastMoved({ ...t, steps });
        }),
      }));
      apiPost<Thread>(`/threads/${threadId}/steps`, {
        label: trimmed,
        note: note ?? null,
      })
        .then((t) =>
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, t),
          })),
        )
        .catch((err) => fail("Log update", snapshot, err));
    },
    [fail],
  );

  /** Set the next step: creates the Google task in the chosen pinned list. */
  const setNextStep = useCallback(
    (
      threadId: number,
      label: string,
      list: ListKey,
      due: string | null,
      dueText: string,
    ) => {
      const trimmed = label.trim();
      if (!trimmed) return;
      const snapshot = threadsRef.current;
      const step: Step = {
        id: tempId(),
        kind: "next",
        label: trimmed,
        note: "",
        occurred_on: null,
        list,
        due,
        tasklist_id: null,
        task_id: null,
        via: null,
      };
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (t) => ({
          ...t,
          steps: [...t.steps, step],
        })),
      }));
      showToast(
        `Added to ${LIST_LABEL[list]}${due ? `, due ${dueText}` : ""}.`,
      );
      apiPost<Thread>(`/threads/${threadId}/next`, {
        label: trimmed,
        list,
        due,
      })
        .then((t) => {
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, t),
          }));
          tasksChanged();
        })
        .catch((err) => fail("Set next step", snapshot, err));
    },
    [fail, showToast, tasksChanged],
  );

  /** Edit a step. Next-step fields go to Google (label/note/due/list) server-side. */
  const updateStep = useCallback(
    (threadId: number, stepId: number, patch: StepPatch): Promise<void> => {
      if (Object.keys(patch).length === 0) return Promise.resolve();
      const snapshot = threadsRef.current;
      const before = snapshot
        .find((t) => t.id === threadId)
        ?.steps.find((s) => s.id === stepId);
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (t) =>
          withLastMoved({
            ...t,
            steps: t.steps.map((x) =>
              x.id === stepId ? { ...x, ...patch } : x,
            ),
          }),
        ),
      }));
      return apiPatch<Thread>(`/threads/${threadId}/steps/${stepId}`, patch)
        .then((t) => {
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, t),
          }));
          if (before?.kind === "next") tasksChanged();
        })
        .catch((err) => fail("Edit", snapshot, err));
    },
    [fail, tasksChanged],
  );

  const toastLogged = useCallback(
    (t: Thread, label: string, list: ListKey | null) => {
      showToast(`Logged “${label}” in ${t.title}. What’s next?`, {
        label: "Set next step",
        run: () => requestThread(t.id, "next", list ?? undefined),
      });
    },
    [showToast, requestThread],
  );

  /** "Mark done" from the popover: complete the Google task, flip the step. */
  const completeStep = useCallback(
    (threadId: number, stepId: number) => {
      const snapshot = threadsRef.current;
      const t = snapshot.find((x) => x.id === threadId);
      const step = t?.steps.find((x) => x.id === stepId);
      if (!t || !step || step.kind !== "next") return;
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (x) => flipNextDone(x, stepId)),
      }));
      requestThread(threadId, "flash");
      toastLogged(t, step.label, step.list);
      apiPost<Thread>(`/threads/${threadId}/steps/${stepId}/complete`, {})
        .then((nt) => {
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, nt),
          }));
          tasksChanged();
        })
        .catch((err) => fail("Mark done", snapshot, err));
    },
    [fail, requestThread, tasksChanged, toastLogged],
  );

  /** Delete a done step, or UNLINK a next step (its Google task stays put). */
  const deleteStep = useCallback(
    (threadId: number, stepId: number) => {
      const snapshot = threadsRef.current;
      const step = snapshot
        .find((t) => t.id === threadId)
        ?.steps.find((s) => s.id === stepId);
      if (!step) return;
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, threadId, (t) =>
          withLastMoved({
            ...t,
            steps: t.steps.filter((x) => x.id !== stepId),
          }),
        ),
      }));
      if (step.kind === "next") {
        showToast(
          `Unlinked “${step.label}”. The task stays in ${
            step.list ? LIST_LABEL[step.list] : "Google Tasks"
          }.`,
        );
      }
      apiDelete<Thread>(`/threads/${threadId}/steps/${stepId}`)
        .then((t) =>
          setState((s) => ({
            ...s,
            threads: replaceThread(s.threads, threadId, t),
          })),
        )
        .catch((err) => fail("Delete", snapshot, err));
    },
    [fail, showToast],
  );

  // ── Tasks → threads coupling (called from DashboardPage's lifted state) ─────

  // Pre-flip snapshots of threads whose linked task was completed from the tasks
  // panel, so the tasks toast's Undo can revert exactly that thread.
  const completionSnapshots = useRef(new Map<number, Thread>());

  /**
   * A task was completed in the tasks panel. If it is a thread's live next step,
   * flip it to done NOW (the backend learns via reconcile) and return what the
   * tasks toast needs; else null.
   */
  const markLinkedCompleted = useCallback(
    (
      taskId: string,
    ): {
      threadId: number;
      title: string;
      label: string;
      list: ListKey | null;
    } | null => {
      const t = threadsRef.current.find(
        (x) => !x.archived && nextOf(x)?.task_id === taskId,
      );
      const step = t ? nextOf(t) : null;
      if (!t || !step) return null;
      completionSnapshots.current.set(t.id, t);
      setState((s) => ({
        ...s,
        threads: mapThread(s.threads, t.id, (x) => flipNextDone(x, step.id)),
      }));
      requestThread(t.id, "flash");
      return {
        threadId: t.id,
        title: t.title,
        label: step.label,
        list: step.list,
      };
    },
    [requestThread],
  );

  /** Undo of a tasks-panel completion: put the thread back as it was. */
  const revertLinkedCompleted = useCallback((threadId: number) => {
    const snap = completionSnapshots.current.get(threadId);
    completionSnapshots.current.delete(threadId);
    if (!snap) return;
    setState((s) => ({
      ...s,
      threads: replaceThread(s.threads, threadId, snap),
    }));
  }, []);

  /** Is this Google task a live next step? (DashboardPage gates refreshes on it.) */
  const isLinked = useCallback(
    (taskId: string) =>
      threadsRef.current.some((t) =>
        t.steps.some((s) => s.kind === "next" && s.task_id === taskId),
      ),
    [],
  );

  // task_id → thread, for the badges on task rows (active threads only).
  const links = useMemo(() => {
    const m = new Map<string, ThreadLink>();
    for (const t of state.threads) {
      if (t.archived) continue;
      const n = nextOf(t);
      if (n?.task_id) m.set(n.task_id, { id: t.id, title: t.title });
    }
    return m;
  }, [state.threads]);

  return {
    ...state,
    links,
    refresh,
    setHold,
    requestThread,
    clearRequest,
    createThread,
    archiveThread,
    restoreThread,
    logStep,
    setNextStep,
    updateStep,
    completeStep,
    deleteStep,
    markLinkedCompleted,
    revertLinkedCompleted,
    isLinked,
    dismissToast,
    runToastAction,
    showToast,
  };
}

export type ThreadsHook = ReturnType<typeof useThreadsPanel>;
