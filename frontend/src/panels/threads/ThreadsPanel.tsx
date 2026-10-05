import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { Sheet } from "../../Sheet";
import {
  LIST_LABEL,
  type ListKey,
  type Step,
  type StepPatch,
  type Thread,
  type ThreadsHook,
  istDayKey,
  openOf,
  soonestOpen,
} from "./useThreadsPanel";

// ── Date helpers (IST day keys, "YYYY-MM-DD") ─────────────────────────────────

const DAY_MS = 86_400_000;

function daysBetween(from: string, to: string): number {
  return Math.round(
    (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / DAY_MS,
  );
}

function shortDate(key: string): string {
  return new Date(`${key}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function dueLabel(due: string | null): string {
  if (!due) return "No date";
  const n = daysBetween(istDayKey(0), due);
  if (n === 0) return "Today";
  if (n === 1) return "Tomorrow";
  if (n < 0) return `${-n}d overdue`;
  const weekday = new Date(`${due}T00:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    timeZone: "UTC",
  });
  return `${weekday} ${shortDate(due)}`;
}

/** "tomorrow" / "today" / "Wed Oct 14" — for the "Added to …, due …" toast. */
function dueText(due: string): string {
  const label = dueLabel(due);
  return label === "Today" || label === "Tomorrow"
    ? label.toLowerCase()
    : label;
}

const isLate = (due: string | null) => !!due && due < istDayKey(0);
const STALE_DAYS = 10;
const SHOW_DONE = 3;
const SHOW_OPEN = 4; // open steps shown in the Detailed stack before "+N more"
const LIST_SHORT: Record<ListKey, string> = {
  mine: "My task",
  follow: "Follow-up",
};

// ── Filters + sort ────────────────────────────────────────────────────────────

type Filter = "all" | "needs" | "mine" | "follow" | "archived";
const FILTERS: [Filter, string][] = [
  ["all", "All"],
  ["needs", "Needs next step"],
  ["mine", "My task"],
  ["follow", "Follow-up"],
  ["archived", "Archived"],
];

function matchFilter(t: Thread, f: Filter): boolean {
  if (f === "archived") return t.archived;
  if (t.archived) return false;
  // Counts are of threads; one with both kinds of open step is under both chips.
  const open = openOf(t);
  if (f === "needs") return open.length === 0;
  if (f === "mine") return open.some((s) => s.list === "mine");
  if (f === "follow") return open.some((s) => s.list === "follow");
  return true;
}

/** Dangling first (least recently moved first), then by soonest open due. */
function sortActive(list: Thread[]): Thread[] {
  return [...list].sort((a, b) => {
    const na = soonestOpen(a);
    const nb = soonestOpen(b);
    if (!na && nb) return -1;
    if (na && !nb) return 1;
    if (!na && !nb) return a.last_moved_on.localeCompare(b.last_moved_on);
    const da = na!.due ?? "9999-99-99";
    const db = nb!.due ?? "9999-99-99";
    return da.localeCompare(db) || a.id - b.id;
  });
}

type View = "compact" | "detailed";
const VIEW_KEY = "threads.view";

function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "detailed"
      ? "detailed"
      : "compact";
  } catch {
    return "compact";
  }
}

function writeView(v: View) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    // Private windows / blocked storage: the choice just doesn't persist.
  }
}

// ── Icons ─────────────────────────────────────────────────────────────────────

export function ThreadIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      aria-hidden
    >
      <circle cx="2" cy="6" r="1.5" fill="currentColor" />
      <circle cx="6.5" cy="6" r="1.5" fill="currentColor" />
      <circle cx="10.5" cy="6" r="1.3" />
      <path d="M3.5 6h1.5M8 6h1.2" />
    </svg>
  );
}

function NoteIcon() {
  return (
    <svg
      className="thr-noteicon"
      width="11"
      height="11"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      aria-label="has a note"
    >
      <path d="M2 2.5h8M2 5.5h8M2 8.5h5" />
    </svg>
  );
}

const CompactIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    aria-hidden
  >
    <path d="M2 3.5h10M2 7h10M2 10.5h10" />
  </svg>
);

const DetailedIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.4"
    aria-hidden
  >
    <circle cx="2.5" cy="4" r="1.3" fill="currentColor" />
    <circle cx="7" cy="4" r="1.3" fill="currentColor" />
    <circle cx="11.5" cy="4" r="1.3" />
    <path d="M3.8 4h1.9M8.3 4h1.9" />
    <circle cx="2.5" cy="10" r="1.3" fill="currentColor" />
    <circle cx="7" cy="10" r="1.3" />
    <path d="M3.8 10h1.9" />
  </svg>
);

// ── Shared bits ───────────────────────────────────────────────────────────────

function DuePill({ step }: { step: Step }) {
  const list = step.list ?? "mine";
  return (
    <span
      className={`thr-duepill thr-duepill--${list}${isLate(step.due) ? " thr-duepill--late" : ""}`}
    >
      {LIST_SHORT[list]} · {dueLabel(step.due)}
    </span>
  );
}

function ListSegment({
  value,
  onChange,
}: {
  value: ListKey;
  onChange: (v: ListKey) => void;
}) {
  return (
    <span className="thr-seg" role="group" aria-label="Whose move">
      {(["mine", "follow"] as ListKey[]).map((k) => (
        <button
          key={k}
          type="button"
          className={value === k ? `on on--${k}` : ""}
          aria-pressed={value === k}
          onClick={() => onChange(k)}
        >
          {LIST_SHORT[k]}
        </button>
      ))}
    </span>
  );
}

// `due` carries the last-used date across rapid entry (the form can remount when
// the thread's first open step lands and the track switches to the stack).
type Editing =
  | { threadId: number; mode: "log" }
  | { threadId: number; mode: "next"; list: ListKey; due?: string };

// ── Inline forms (Enter submits, Esc cancels) ─────────────────────────────────

function LogForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (label: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  return (
    <div className="thr-form">
      <input
        type="text"
        className="thr-input"
        placeholder="What happened?"
        aria-label="What happened?"
        value={text}
        autoFocus
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && text.trim()) onSubmit(text.trim());
          if (e.key === "Escape") onCancel();
        }}
      />
      <div className="thr-hint">Enter logs it as today · Esc cancels</div>
    </div>
  );
}

/** Rapid entry (goal 14a): Enter creates the step and keeps the form open with
 * the label cleared and the list + due kept; Esc closes it. */
function NextForm({
  initialList,
  initialDue,
  onSubmit,
  onCancel,
}: {
  initialList: ListKey;
  initialDue?: string;
  onSubmit: (label: string, list: ListKey, due: string | null) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const [list, setList] = useState<ListKey>(initialList);
  const [due, setDue] = useState(initialDue ?? istDayKey(1));
  const inputRef = useRef<HTMLInputElement>(null);
  function onKey(e: ReactKeyboardEvent) {
    if (e.key === "Enter" && text.trim()) {
      onSubmit(text.trim(), list, due || null);
      setText("");
      inputRef.current?.focus();
    }
    if (e.key === "Escape") onCancel();
  }
  return (
    <div className="thr-form">
      <input
        type="text"
        className="thr-input"
        placeholder="Next step…"
        aria-label="Next step"
        ref={inputRef}
        value={text}
        autoFocus
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
      />
      <div className="thr-form-row">
        <ListSegment value={list} onChange={setList} />
        <input
          type="date"
          className="thr-date"
          aria-label="Due date"
          value={due}
          onChange={(e) => setDue(e.target.value)}
          onKeyDown={onKey}
        />
      </div>
      <div className="thr-hint">
        Enter creates it in {LIST_LABEL[list]} and keeps going · Esc closes
      </div>
    </div>
  );
}

// ── Step track (Detailed row) ─────────────────────────────────────────────────

interface TrackItem {
  key: string;
  node: ReactNode;
  // Connector style INTO this item from the previous one.
  into: "solid" | "dash" | "dash-warn";
  end?: boolean; // no connector out of this item
  plus?: boolean; // the trailing "+": never connected
  cls?: string;
}

/** A done step on the track (open steps render in the OpenStack). */
function StepButton({
  step,
  onOpen,
}: {
  step: Step;
  onOpen: (el: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      className="thr-step"
      onClick={(e) => onOpen(e.currentTarget)}
    >
      <span className="thr-dot" />
      <span className="thr-lb" title={step.label}>
        {step.label}
      </span>
      <span className="thr-dt">
        {step.occurred_on ? shortDate(step.occurred_on) : ""}
        {step.note && <NoteIcon />}
        {step.via && <span>· via {LIST_LABEL[step.via]}</span>}
      </span>
    </button>
  );
}

/** One row of the open stack: ring, one-line label, due pill. */
function OpenStepRow({
  step,
  onOpen,
}: {
  step: Step;
  onOpen: (el: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      className={`thr-orow thr-orow--${step.list ?? "mine"}`}
      onClick={(e) => onOpen(e.currentTarget)}
    >
      <span className="thr-ring-dot" />
      <span className="thr-olb" title={step.label}>
        {step.label}
      </span>
      <DuePill step={step} />
      {step.note && <NoteIcon />}
    </button>
  );
}

/** The open block (goal 14a): a dashed fork into a vertical stack of parallel
 * open steps, soonest due first, folding after SHOW_OPEN; "+ add open step" (or
 * the rapid-entry form) at the bottom. */
function OpenStack({
  open,
  editing,
  archived,
  onOpenStep,
  onStartNext,
  onSubmitNext,
  onCancelEdit,
}: {
  open: Step[];
  editing: Editing | null;
  archived: boolean;
  onOpenStep: (step: Step, el: HTMLElement) => void;
  onStartNext: (list: ListKey) => void;
  onSubmitNext: (label: string, list: ListKey, due: string | null) => void;
  onCancelEdit: () => void;
}) {
  const [all, setAll] = useState(false);
  const extra = open.length - SHOW_OPEN;
  const shown = all || extra <= 0 ? open : open.slice(0, SHOW_OPEN);
  const fork = open.length > 1 || editing?.mode === "next" || extra > 0;
  return (
    <div className="thr-openwrap">
      <div className={`thr-open${fork ? " thr-open--fork" : ""}`}>
        {shown.map((s) => (
          <div key={s.id} className="thr-obranch">
            <OpenStepRow step={s} onOpen={(el) => onOpenStep(s, el)} />
          </div>
        ))}
        {extra > 0 && (
          <div className="thr-obranch">
            <button
              type="button"
              className="thr-earlier thr-omore"
              onClick={() => setAll((v) => !v)}
            >
              {all ? "‹ fewer" : `+${extra} more`}
            </button>
          </div>
        )}
        {editing?.mode === "next" && (
          <div className="thr-obranch thr-obranch--form">
            <NextForm
              initialList={editing.list}
              initialDue={editing.due}
              onSubmit={onSubmitNext}
              onCancel={onCancelEdit}
            />
          </div>
        )}
      </div>
      {editing?.mode !== "next" && !archived && (
        <button
          type="button"
          className={`thr-oadd${fork ? " thr-oadd--fork" : ""}`}
          onClick={() => onStartNext(open[open.length - 1]?.list ?? "mine")}
        >
          + add open step
        </button>
      )}
    </div>
  );
}

function Track({
  thread,
  showAll,
  editing,
  onToggleAll,
  onOpenStep,
  onStartLog,
  onStartNext,
  onSubmitLog,
  onSubmitNext,
  onCancelEdit,
}: {
  thread: Thread;
  showAll: boolean;
  editing: Editing | null;
  onToggleAll: () => void;
  onOpenStep: (step: Step, el: HTMLElement) => void;
  onStartLog: () => void;
  onStartNext: (list: ListKey) => void;
  onSubmitLog: (label: string) => void;
  onSubmitNext: (label: string, list: ListKey, due: string | null) => void;
  onCancelEdit: () => void;
}) {
  const done = thread.steps.filter((s) => s.kind === "done");
  const open = openOf(thread);
  const next = open.length > 0;
  let shown = done;
  let hidden = 0;
  if (!showAll && done.length > SHOW_DONE) {
    hidden = done.length - SHOW_DONE;
    shown = done.slice(-SHOW_DONE);
  }

  const items: TrackItem[] = [];
  if (hidden) {
    items.push({
      key: "earlier",
      cls: "thr-item--earlier",
      into: "solid",
      node: (
        <button type="button" className="thr-earlier" onClick={onToggleAll}>
          +{hidden} earlier
        </button>
      ),
    });
  } else if (showAll && done.length > SHOW_DONE) {
    items.push({
      key: "fewer",
      cls: "thr-item--earlier",
      into: "solid",
      node: (
        <button type="button" className="thr-earlier" onClick={onToggleAll}>
          ‹ fewer
        </button>
      ),
    });
  }
  for (const s of shown) {
    items.push({
      key: `s${s.id}`,
      into: "solid",
      node: <StepButton step={s} onOpen={(el) => onOpenStep(s, el)} />,
    });
  }
  const logging = editing?.mode === "log";
  if (logging) {
    items.push({
      key: "log",
      into: "solid",
      end: true,
      node: <LogForm onSubmit={onSubmitLog} onCancel={onCancelEdit} />,
    });
  }
  if (next) {
    items.push({
      key: "open",
      into: "dash",
      node: (
        <OpenStack
          open={open}
          editing={editing}
          archived={thread.archived}
          onOpenStep={onOpenStep}
          onStartNext={onStartNext}
          onSubmitNext={onSubmitNext}
          onCancelEdit={onCancelEdit}
        />
      ),
    });
  } else if (editing?.mode === "next") {
    items.push({
      key: "nextform",
      into: "dash",
      node: (
        <NextForm
          initialList={editing.list}
          initialDue={editing.due}
          onSubmit={onSubmitNext}
          onCancel={onCancelEdit}
        />
      ),
    });
  } else if (!logging && !thread.archived) {
    items.push({
      key: "slot",
      into: "dash-warn",
      node: (
        <div className="thr-slot">
          <div className="thr-slot-q">
            What’s next?
            {done.length === 0 && (
              <span> Log what’s happened so far, or set a next step.</span>
            )}
          </div>
          <div className="thr-slot-btns">
            <button
              type="button"
              className="thr-slot-mine"
              onClick={() => onStartNext("mine")}
            >
              + My task
            </button>
            <button
              type="button"
              className="thr-slot-follow"
              onClick={() => onStartNext("follow")}
            >
              + Follow-up
            </button>
            <button type="button" onClick={onStartLog}>
              Log update
            </button>
          </div>
        </div>
      ),
    });
  }
  if (next && !logging && !thread.archived) {
    items.push({
      key: "plus",
      into: "solid",
      plus: true,
      node: (
        <button
          type="button"
          className="thr-plus"
          title="Log an update"
          aria-label={`Log an update on ${thread.title}`}
          onClick={onStartLog}
        >
          +
        </button>
      ),
    });
  }

  // The latest steps matter most: open scrolled to the end (next step / "What's
  // next?"), and back to the start when the earlier steps are unfolded.
  const trackRef = useRef<HTMLDivElement>(null);
  const stepCount = thread.steps.length;
  const editMode = editing?.mode;
  useLayoutEffect(() => {
    const el = trackRef.current;
    if (el) el.scrollLeft = showAll ? 0 : el.scrollWidth;
  }, [showAll, stepCount, editMode]);

  return (
    <div className="thr-track" ref={trackRef}>
      {items.map((it, i) => {
        const after = items[i + 1];
        const linked = after && !it.end && !after.plus;
        const link = linked
          ? ` thr-item--link${after.into !== "solid" ? ` thr-item--${after.into}` : ""}`
          : "";
        return (
          <div
            key={it.key}
            className={`thr-item${it.cls ? ` ${it.cls}` : ""}${link}`}
          >
            {it.node}
          </div>
        );
      })}
    </div>
  );
}

// ── Step popover ──────────────────────────────────────────────────────────────

function StepPopover({
  thread,
  step,
  anchor,
  mobile,
  onPatch,
  onMarkDone,
  onDelete,
  onClose,
}: {
  thread: Thread;
  step: Step;
  anchor: DOMRect;
  // Phone (goal 15): the same editor, in a bottom sheet instead of anchored.
  mobile?: boolean;
  onPatch: (patch: StepPatch) => Promise<void>;
  onMarkDone: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const isNext = step.kind === "next";
  const [label, setLabel] = useState(step.label);
  const [note, setNote] = useState(step.note);
  const [date, setDate] = useState(
    (isNext ? step.due : step.occurred_on) ?? "",
  );
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  // The step changed underneath us (e.g. its task's notes were edited in the
  // tasks panel and the threads refresh landed): adopt the new values in any
  // field the user hasn't touched since it was seeded.
  const stepDate = (isNext ? step.due : step.occurred_on) ?? "";
  const [seen, setSeen] = useState({
    label: step.label,
    note: step.note,
    date: stepDate,
  });
  if (
    step.label !== seen.label ||
    step.note !== seen.note ||
    stepDate !== seen.date
  ) {
    setSeen({ label: step.label, note: step.note, date: stepDate });
    if (label === seen.label) setLabel(step.label);
    if (note === seen.note) setNote(step.note);
    if (date === seen.date) setDate(stepDate);
  }

  // Place under the step (flip above if it would overflow the viewport).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || mobile) return;
    const W = el.offsetWidth;
    const H = el.offsetHeight;
    const left = Math.max(
      12,
      Math.min(anchor.left, window.innerWidth - W - 12),
    );
    let top = anchor.bottom + 6;
    if (top + H > window.innerHeight - 12)
      top = Math.max(12, anchor.top - H - 6);
    setPos({ left, top });
  }, [anchor, mobile]);

  // Only fields that differ from the step's current value are sent.
  function diff(): StepPatch {
    const patch: StepPatch = {};
    const trimmed = label.trim();
    if (trimmed && trimmed !== step.label) patch.label = trimmed;
    if (note !== step.note) patch.note = note;
    if (isNext) {
      if ((date || null) !== step.due) patch.due = date || null;
    } else if (date && date !== step.occurred_on) {
      patch.occurred_on = date;
    }
    return patch;
  }

  function commit(): Promise<void> {
    const patch = diff();
    return Object.keys(patch).length ? onPatch(patch) : Promise.resolve();
  }

  function close() {
    void commit();
    onClose();
  }

  // Outside click / Esc: save, then close.
  const closeRef = useRef(close);
  useEffect(() => {
    closeRef.current = close;
  });
  useEffect(() => {
    // A sheet closes itself (scrim tap / Esc) through `close`.
    if (mobile) return;
    function onDown(e: PointerEvent) {
      if (!ref.current?.contains(e.target as Node)) closeRef.current();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeRef.current();
    }
    // Capture phase: task-row controls stop pointerdown propagation (so they
    // never start a drag), which would otherwise hide the click from us.
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [mobile]);

  const body = (
    <>
      <div className="thr-pk">
        {isNext ? "Next step" : "Step"} · {thread.title}
      </div>
      <input
        type="text"
        className="thr-input"
        aria-label="Step"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        onBlur={() => void commit()}
      />
      {isNext ? (
        <div className="thr-form-row">
          <ListSegment
            value={step.list ?? "mine"}
            onChange={(v) => {
              if (v !== step.list) void onPatch({ ...diff(), list: v });
            }}
          />
          <input
            type="date"
            className="thr-date"
            aria-label="Due date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            onBlur={() => void commit()}
          />
        </div>
      ) : (
        <div className="thr-form-row">
          <label className="thr-label" htmlFor={`thr-happened-${step.id}`}>
            Happened on
          </label>
          <input
            id={`thr-happened-${step.id}`}
            type="date"
            className="thr-date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            onBlur={() => void commit()}
          />
        </div>
      )}
      <div className="thr-nl">
        <label className="thr-label" htmlFor={`thr-note-${step.id}`}>
          Note
        </label>
        <i>
          {isNext
            ? "Same text as the task’s notes"
            : "Context for future you"}
        </i>
      </div>
      <textarea
        id={`thr-note-${step.id}`}
        className="thr-textarea"
        placeholder={
          isNext
            ? "What should you remember when you pick this up?"
            : "What was said, decided, or sent?"
        }
        value={note}
        onChange={(e) => setNote(e.target.value)}
        onBlur={() => void commit()}
      />
      <div className="thr-pa">
        <div className="thr-pa-l">
          {isNext && (
            <button
              type="button"
              className="thr-btn thr-btn--primary"
              onClick={async () => {
                await commit();
                onMarkDone();
              }}
            >
              Mark done
            </button>
          )}
          <button
            type="button"
            className="thr-btn thr-btn--danger"
            onClick={onDelete}
          >
            Delete
          </button>
        </div>
        <button type="button" className="thr-btn" onClick={close}>
          Close
        </button>
      </div>
    </>
  );

  if (mobile)
    return (
      <Sheet
        label={isNext ? "Next step" : "Step"}
        onClose={close}
        className="thr-pop--sheet"
      >
        {body}
      </Sheet>
    );

  return createPortal(
    <div
      ref={ref}
      className="thr-pop"
      role="dialog"
      aria-label={isNext ? "Next step" : "Step"}
      style={
        pos
          ? { left: pos.left, top: pos.top }
          : { left: anchor.left, top: anchor.bottom + 6, visibility: "hidden" }
      }
    >
      {body}
    </div>,
    document.body,
  );
}

// ── ··· menu ──────────────────────────────────────────────────────────────────

function ThreadMenu({
  thread,
  anchor,
  showAll,
  mobile,
  onClose,
  onToggleAll,
  onLog,
  onArchive,
  onRestore,
}: {
  thread: Thread;
  anchor: DOMRect;
  showAll: boolean;
  // Phone (goal 15): the same items, in a bottom sheet.
  mobile?: boolean;
  onClose: () => void;
  onToggleAll: () => void;
  onLog: () => void;
  onArchive: () => void;
  onRestore: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });
  useEffect(() => {
    if (mobile) return; // the sheet handles scrim taps + Esc
    function onDown(e: PointerEvent) {
      const t = e.target as Element;
      if (ref.current?.contains(t) || t.closest?.(".thr-more")) return;
      closeRef.current();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeRef.current();
    }
    // Capture phase: task-row controls stop pointerdown propagation (so they
    // never start a drag), which would otherwise hide the click from us.
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [mobile]);

  const act = (fn: () => void) => () => {
    onClose();
    fn();
  };

  const items = (
    <>
      {thread.archived ? (
        <button
          type="button"
          className="move-to-list-option"
          role="menuitem"
          onClick={act(onRestore)}
        >
          Restore thread
        </button>
      ) : (
        <>
          <button
            type="button"
            className="move-to-list-option"
            role="menuitem"
            onClick={act(onToggleAll)}
          >
            {showAll ? "Show recent steps" : "Show all steps"}
          </button>
          <button
            type="button"
            className="move-to-list-option"
            role="menuitem"
            onClick={act(onLog)}
          >
            Log update
          </button>
          <button
            type="button"
            className="move-to-list-option"
            role="menuitem"
            onClick={act(onArchive)}
          >
            Archive thread
          </button>
        </>
      )}
    </>
  );

  if (mobile)
    return (
      <Sheet label={`Options for ${thread.title}`} onClose={onClose}>
        <h3 className="sheet-title">{thread.title}</h3>
        <div className="sheet-list" role="menu">
          {items}
        </div>
      </Sheet>
    );

  return createPortal(
    <div
      ref={ref}
      className="task-menu-popover thr-menu"
      role="menu"
      style={{
        top: anchor.bottom + 4,
        left: Math.max(12, anchor.right - 170),
      }}
    >
      {items}
    </div>,
    document.body,
  );
}

// ── Rows ──────────────────────────────────────────────────────────────────────

function MoreButton({
  thread,
  onOpen,
}: {
  thread: Thread;
  onOpen: (rect: DOMRect) => void;
}) {
  return (
    <button
      type="button"
      className="thr-more"
      aria-label={`Options for ${thread.title}`}
      onClick={(e) => onOpen(e.currentTarget.getBoundingClientRect())}
    >
      ···
    </button>
  );
}

function CompactRow({
  thread,
  flash,
  onToggle,
  onMenu,
}: {
  thread: Thread;
  flash: boolean;
  onToggle: () => void;
  onMenu: (rect: DOMRect) => void;
}) {
  const open = openOf(thread);
  const n = open[0];
  const others = open.slice(1);
  const moved = daysBetween(thread.last_moved_on, istDayKey(0));
  const done = thread.steps.filter((s) => s.kind === "done");
  const last = done[done.length - 1];
  let mid: ReactNode;
  if (n) {
    mid = (
      <>
        <span className={`thr-ring thr-ring--${n.list ?? "mine"}`} />
        <span className="thr-c-lb" title={n.label}>
          {n.label}
        </span>
        <DuePill step={n} />
        {others.length > 0 && (
          <span
            className="thr-chip thr-chip--more"
            title={others
              .map(
                (s) =>
                  `${s.label} — ${LIST_SHORT[s.list ?? "mine"]} · ${dueLabel(s.due)}`,
              )
              .join("\n")}
          >
            +{others.length}
          </span>
        )}
      </>
    );
  } else if (thread.archived) {
    mid = (
      <span className="thr-c-lb thr-c-lb--dim">
        {last ? last.label : "No steps"}
      </span>
    );
  } else {
    mid = (
      <>
        <span className="thr-chip thr-chip--warn">No next step</span>
        <span className="thr-c-lb thr-c-lb--dim" title={last?.label}>
          {last ? `Last: ${last.label}` : "No steps yet"}
        </span>
      </>
    );
  }
  return (
    <div
      id={`thread-row-${thread.id}`}
      className={`thr-crow${thread.archived ? " thr-archived" : ""}${flash ? " thr-flash" : ""}`}
    >
      <button
        type="button"
        className="thr-c-main"
        aria-expanded={false}
        onClick={onToggle}
      >
        <span className="thr-caret">›</span>
        <span className="thr-c-title" title={thread.title}>
          {thread.title}
        </span>
        <span className="thr-c-mid">{mid}</span>
        <span
          className={`thr-c-moved${moved >= STALE_DAYS ? " thr-stale" : ""}`}
        >
          {moved <= 0 ? "today" : `${moved}d`}
        </span>
      </button>
      <MoreButton thread={thread} onOpen={onMenu} />
    </div>
  );
}

function DetailedRow({
  thread,
  collapsible,
  flash,
  onToggle,
  onMenu,
  children,
}: {
  thread: Thread;
  collapsible: boolean;
  flash: boolean;
  onToggle: () => void;
  onMenu: (rect: DOMRect) => void;
  children: ReactNode;
}) {
  const openCount = openOf(thread).length;
  const moved = daysBetween(thread.last_moved_on, istDayKey(0));
  const count = thread.steps.filter((s) => s.kind === "done").length;
  return (
    <div
      id={`thread-row-${thread.id}`}
      className={`thr-row${thread.archived ? " thr-archived" : ""}${collapsible ? " thr-row--opened" : ""}${flash ? " thr-flash" : ""}`}
    >
      <div className="thr-head">
        {collapsible ? (
          <button
            type="button"
            className="thr-title thr-title--btn"
            aria-expanded
            onClick={onToggle}
          >
            <span className="thr-caret thr-caret--open">›</span>
            {thread.title}
          </button>
        ) : (
          <div className="thr-title">{thread.title}</div>
        )}
        <div className="thr-meta">
          {count} step{count === 1 ? "" : "s"} ·{" "}
          {openCount > 0 && `${openCount} open · `}
          <span className={moved >= STALE_DAYS ? "thr-stale" : ""}>
            moved {moved <= 0 ? "today" : `${moved}d ago`}
          </span>
        </div>
        {!thread.archived && openCount === 0 && (
          <span className="thr-chip thr-chip--warn">No next step</span>
        )}
      </div>
      {children}
      <MoreButton thread={thread} onOpen={onMenu} />
    </div>
  );
}

// ── Phone row (goal 15) ───────────────────────────────────────────────────────

/** The soonest open step at phone width: ring + full text, pill on its own line. */
function MobileStep({
  step,
  more,
  onClick,
}: {
  step: Step;
  more?: number;
  onClick: (el: HTMLElement) => void;
}) {
  return (
    <button
      type="button"
      className={`m-thr-step m-thr-step--${step.list ?? "mine"}`}
      onClick={(e) => onClick(e.currentTarget)}
    >
      <span className={`thr-ring thr-ring--${step.list ?? "mine"}`} />
      <span className="m-thr-step-body">
        <span className="m-thr-step-text">{step.label}</span>
        <span className="m-thr-step-meta">
          <DuePill step={step} />
          {!!more && (
            <span className="m-thr-more-steps">
              +{more} step{more === 1 ? "" : "s"}
            </span>
          )}
          {step.note && <NoteIcon />}
        </span>
      </span>
    </button>
  );
}

/**
 * A thread at phone width: two lines collapsed (title · age · ⋯, then the soonest
 * open step in full), and a VERTICAL list expanded — open steps, the last few done
 * steps (struck) with the "+N earlier" fold, then "+ next step" / "+ log update".
 * Same data, forms and callbacks as the desktop Track; only the shape differs.
 */
function MobileThreadRow({
  thread,
  expanded,
  flash,
  showAll,
  editing,
  onToggle,
  onMenu,
  onToggleAll,
  onOpenStep,
  onStartLog,
  onStartNext,
  onSubmitLog,
  onSubmitNext,
  onCancelEdit,
}: {
  thread: Thread;
  expanded: boolean;
  flash: boolean;
  showAll: boolean;
  editing: Editing | null;
  onToggle: () => void;
  onMenu: (rect: DOMRect) => void;
  onToggleAll: () => void;
  onOpenStep: (step: Step, el: HTMLElement) => void;
  onStartLog: () => void;
  onStartNext: (list: ListKey) => void;
  onSubmitLog: (label: string) => void;
  onSubmitNext: (label: string, list: ListKey, due: string | null) => void;
  onCancelEdit: () => void;
}) {
  const open = openOf(thread);
  const n = open[0];
  const done = thread.steps.filter((s) => s.kind === "done");
  const last = done[done.length - 1];
  const moved = daysBetween(thread.last_moved_on, istDayKey(0));

  let body: ReactNode;
  if (!expanded) {
    if (n) {
      body = <MobileStep step={n} more={open.length - 1} onClick={onToggle} />;
    } else {
      body = (
        <button type="button" className="m-thr-step" onClick={onToggle}>
          <span className="thr-ring thr-ring--none" />
          <span className="m-thr-step-body">
            {!thread.archived && (
              <span className="thr-chip thr-chip--warn">Needs next step</span>
            )}
            <span className="m-thr-step-text m-thr-step-text--dim">
              {last
                ? `${thread.archived ? "" : "Last: "}${last.label}`
                : "No steps yet"}
            </span>
          </span>
        </button>
      );
    }
  } else {
    // Newest done first, folding all but the last SHOW_DONE.
    const recent = [...done].reverse();
    const hidden = showAll ? 0 : Math.max(0, recent.length - SHOW_DONE);
    const shownDone = hidden ? recent.slice(0, SHOW_DONE) : recent;
    const nextForm =
      editing?.mode === "next" ? (
        <NextForm
          initialList={editing.list}
          initialDue={editing.due}
          onSubmit={onSubmitNext}
          onCancel={onCancelEdit}
        />
      ) : null;
    body = (
      <div className="m-thr-detail">
        {open.map((s) => (
          <MobileStep key={s.id} step={s} onClick={(el) => onOpenStep(s, el)} />
        ))}
        {nextForm}
        {!n && !editing && !thread.archived && (
          <div className="thr-slot">
            <div className="thr-slot-q">
              What’s next?
              {done.length === 0 && (
                <span> Log what’s happened so far, or set a next step.</span>
              )}
            </div>
            <div className="thr-slot-btns">
              <button
                type="button"
                className="thr-slot-mine"
                onClick={() => onStartNext("mine")}
              >
                + My task
              </button>
              <button
                type="button"
                className="thr-slot-follow"
                onClick={() => onStartNext("follow")}
              >
                + Follow-up
              </button>
              <button type="button" onClick={onStartLog}>
                Log update
              </button>
            </div>
          </div>
        )}
        {editing?.mode === "log" && (
          <LogForm onSubmit={onSubmitLog} onCancel={onCancelEdit} />
        )}
        {shownDone.map((s) => (
          <button
            key={s.id}
            type="button"
            className="m-thr-done"
            onClick={(e) => onOpenStep(s, e.currentTarget)}
          >
            <span className="thr-dot" />
            <span className="m-thr-done-text">
              <s>{s.label}</s>
              <span className="m-thr-done-date">
                {s.occurred_on ? shortDate(s.occurred_on) : ""}
                {s.via && ` · via ${LIST_LABEL[s.via]}`}
              </span>
            </span>
            {s.note && <NoteIcon />}
          </button>
        ))}
        {(hidden > 0 || (showAll && done.length > SHOW_DONE)) && (
          <button type="button" className="thr-earlier" onClick={onToggleAll}>
            {hidden ? `+${hidden} earlier` : "‹ fewer"}
          </button>
        )}
        {n && !editing && !thread.archived && (
          <div className="m-thr-actions">
            <button
              type="button"
              className="thr-btn"
              onClick={() => onStartNext(open[open.length - 1]?.list ?? "mine")}
            >
              + next step
            </button>
            <button type="button" className="thr-btn" onClick={onStartLog}>
              + log update
            </button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      id={`thread-row-${thread.id}`}
      className={`m-thr${expanded ? " m-thr--open" : ""}${thread.archived ? " thr-archived" : ""}${flash ? " thr-flash" : ""}`}
    >
      <div className="m-thr-head">
        <button
          type="button"
          className="m-thr-main"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          <span className={`thr-caret${expanded ? " thr-caret--open" : ""}`}>
            ›
          </span>
          <span className="m-thr-title">{thread.title}</span>
          <span
            className={`m-thr-age${moved >= STALE_DAYS ? " thr-stale" : ""}`}
          >
            {moved <= 0 ? "today" : `${moved}d`}
          </span>
        </button>
        <MoreButton thread={thread} onOpen={onMenu} />
      </div>
      {body}
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

/**
 * Threads (goal 14): the story behind each task. Local UI state (filter, view,
 * which rows are open, forms, popover) lives here; data + writes live in the
 * lifted `useThreadsPanel` owned by DashboardPage.
 */
export function ThreadsPanel({
  threads,
  mobile,
}: {
  threads: ThreadsHook;
  // Phone (goal 15): two-line rows that expand into a vertical list, popovers in
  // sheets, no view toggle (always the compact list), refresh moved to the Home
  // header.
  mobile?: boolean;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [savedView, setView] = useState<View>(readView);
  const view: View = mobile ? "compact" : savedView;
  const [open, setOpen] = useState<Set<number>>(() => new Set());
  const [showAll, setShowAll] = useState<Set<number>>(() => new Set());
  const [editing, setEditing] = useState<Editing | null>(null);
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [pop, setPop] = useState<{
    threadId: number;
    stepId: number;
    anchor: DOMRect;
  } | null>(null);
  const [menu, setMenu] = useState<{
    threadId: number;
    anchor: DOMRect;
  } | null>(null);
  const [flash, setFlash] = useState<{ id: number; nonce: number } | null>(
    null,
  );

  const all = threads.threads;

  // Consume a request from outside (badge click / "Set next step") while
  // rendering — the "adjust state on prop change" pattern, nonce-guarded.
  const [seenNonce, setSeenNonce] = useState<number | null>(null);
  const req = threads.request;
  if (req && req.nonce !== seenNonce) {
    setSeenNonce(req.nonce);
    const t = all.find((x) => x.id === req.threadId);
    if (t) {
      if (req.mode !== "flash") {
        if (!matchFilter(t, filter)) setFilter(t.archived ? "archived" : "all");
        setOpen((s) => new Set(s).add(t.id));
      }
      if (req.mode === "next") {
        setEditing({
          threadId: t.id,
          mode: "next",
          list: req.list ?? "follow",
        });
      }
      setFlash({ id: t.id, nonce: req.nonce });
    }
  }

  // Scroll a flashed row into view, then fade the flash out.
  useEffect(() => {
    if (!flash) return;
    const el = document.getElementById(`thread-row-${flash.id}`);
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    el?.scrollIntoView({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
    const id = window.setTimeout(() => setFlash(null), 900);
    return () => window.clearTimeout(id);
  }, [flash]);

  // Hold polling while anything is being edited.
  const { setHold } = threads;
  useEffect(() => {
    setHold(!!(editing || pop || creating));
  }, [editing, pop, creating, setHold]);

  function chooseView(v: View) {
    setView(v);
    writeView(v);
    // Compact means compact: collapse every row expanded in place (and drop the
    // in-row form / popover that would otherwise hold one open).
    if (v === "compact") {
      setOpen(new Set());
      setEditing(null);
      setPop(null);
    }
  }

  function toggleOpen(id: number) {
    const closing = open.has(id);
    if (closing && editing?.threadId === id) setEditing(null);
    setOpen((s) => {
      const n = new Set(s);
      if (closing) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function toggleAll(id: number) {
    setShowAll((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function startEdit(e: Editing) {
    setEditing(e);
    setOpen((s) => new Set(s).add(e.threadId));
  }

  async function submitNew() {
    const title = newTitle.trim();
    if (!title) return;
    setCreating(false);
    setNewTitle("");
    const id = await threads.createThread(title);
    if (id != null) {
      startEdit({ threadId: id, mode: "log" });
      setFlash({ id, nonce: Date.now() });
    }
  }

  const counts = Object.fromEntries(
    FILTERS.map(([k]) => [k, all.filter((t) => matchFilter(t, k)).length]),
  ) as Record<Filter, number>;

  let list = all.filter((t) => matchFilter(t, filter));
  if (filter !== "archived") list = sortActive(list);

  const popThread = pop ? all.find((t) => t.id === pop.threadId) : undefined;
  const popStep = popThread?.steps.find((s) => s.id === pop?.stepId);
  const menuThread = menu ? all.find((t) => t.id === menu.threadId) : undefined;

  function renderThread(t: Thread) {
    const editingHere = editing?.threadId === t.id ? editing : null;
    const isFlash = flash?.id === t.id;
    const menuOpen = (rect: DOMRect) =>
      setMenu({ threadId: t.id, anchor: rect });
    if (mobile) {
      return (
        <MobileThreadRow
          key={t.id}
          thread={t}
          expanded={open.has(t.id) || !!editingHere}
          flash={isFlash}
          showAll={showAll.has(t.id)}
          editing={editingHere}
          onToggle={() => toggleOpen(t.id)}
          onMenu={menuOpen}
          onToggleAll={() => toggleAll(t.id)}
          onOpenStep={(s, el) =>
            setPop({
              threadId: t.id,
              stepId: s.id,
              anchor: el.getBoundingClientRect(),
            })
          }
          onStartLog={() => startEdit({ threadId: t.id, mode: "log" })}
          onStartNext={(l) =>
            startEdit({ threadId: t.id, mode: "next", list: l })
          }
          onSubmitLog={(label) => {
            setEditing(null);
            threads.logStep(t.id, label);
            setFlash({ id: t.id, nonce: Date.now() });
          }}
          onSubmitNext={(label, l, due) => {
            setEditing({
              threadId: t.id,
              mode: "next",
              list: l,
              due: due ?? undefined,
            });
            threads.setNextStep(t.id, label, l, due, due ? dueText(due) : "");
            setFlash({ id: t.id, nonce: Date.now() });
          }}
          onCancelEdit={() => setEditing(null)}
        />
      );
    }
    if (view === "compact" && !open.has(t.id) && !editingHere) {
      return (
        <CompactRow
          key={t.id}
          thread={t}
          flash={isFlash}
          onToggle={() => toggleOpen(t.id)}
          onMenu={menuOpen}
        />
      );
    }
    return (
      <DetailedRow
        key={t.id}
        thread={t}
        collapsible={view === "compact"}
        flash={isFlash}
        onToggle={() => toggleOpen(t.id)}
        onMenu={menuOpen}
      >
        <Track
          thread={t}
          showAll={showAll.has(t.id)}
          editing={editingHere}
          onToggleAll={() => toggleAll(t.id)}
          onOpenStep={(s, el) =>
            setPop({
              threadId: t.id,
              stepId: s.id,
              anchor: el.getBoundingClientRect(),
            })
          }
          onStartLog={() => startEdit({ threadId: t.id, mode: "log" })}
          onStartNext={(l) =>
            startEdit({ threadId: t.id, mode: "next", list: l })
          }
          onSubmitLog={(label) => {
            setEditing(null);
            threads.logStep(t.id, label);
            setFlash({ id: t.id, nonce: Date.now() });
          }}
          onSubmitNext={(label, l, due) => {
            // Rapid entry: the form stays open, remembering list + due.
            setEditing({
              threadId: t.id,
              mode: "next",
              list: l,
              due: due ?? undefined,
            });
            threads.setNextStep(t.id, label, l, due, due ? dueText(due) : "");
            setFlash({ id: t.id, nonce: Date.now() });
          }}
          onCancelEdit={() => setEditing(null)}
        />
      </DetailedRow>
    );
  }

  let body: ReactNode;
  if (threads.isLoading) body = <p className="panel-status">Loading…</p>;
  else if (threads.error)
    body = <p className="panel-status panel-error">{threads.error}</p>;
  else
    body = (
      <div className={`thr-list thr-list--${view}`}>
        {creating && (
          <div className="thr-newrow">
            <input
              type="text"
              className="thr-input"
              placeholder="Name the thread, e.g. “Partner NGO pilot”"
              aria-label="Thread name"
              value={newTitle}
              autoFocus
              onChange={(e) => setNewTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void submitNew();
                if (e.key === "Escape") {
                  setCreating(false);
                  setNewTitle("");
                }
              }}
            />
            <button
              type="button"
              className="thr-btn"
              onClick={() => {
                setCreating(false);
                setNewTitle("");
              }}
            >
              Cancel
            </button>
          </div>
        )}
        {list.length === 0 && !creating && (
          <p className="thr-empty">
            {filter === "archived"
              ? "No archived threads."
              : filter === "all"
                ? "No threads yet. Start one with + thread."
                : "Nothing here right now."}
          </p>
        )}
        {list.map(renderThread)}
      </div>
    );

  return (
    <section
      className={`panel threads-panel${mobile ? " threads-panel--mobile" : ""}`}
    >
      <div className="panel-head thr-ph">
        <h2>Threads</h2>
        <div className="thr-fbar" role="group" aria-label="Filter threads">
          {FILTERS.map(([k, label]) => (
            <button
              key={k}
              type="button"
              className={`thr-fchip${filter === k ? " on" : ""}${k === "needs" && counts.needs > 0 ? " thr-fchip--warn" : ""}`}
              aria-pressed={filter === k}
              onClick={() => {
                setFilter(k);
                setCreating(false);
              }}
            >
              {label}
              <span>{counts[k]}</span>
            </button>
          ))}
        </div>
        <span className="thr-sp" />
        {!mobile && (
          <span
            className="thr-seg thr-seg--view"
            role="group"
            aria-label="View"
          >
            <button
              type="button"
              className={view === "compact" ? "on" : ""}
              aria-pressed={view === "compact"}
              title="Compact: one line per thread"
              aria-label="Compact view"
              onClick={() => chooseView("compact")}
            >
              <CompactIcon />
            </button>
            <button
              type="button"
              className={view === "detailed" ? "on" : ""}
              aria-pressed={view === "detailed"}
              title="Detailed: full step track"
              aria-label="Detailed view"
              onClick={() => chooseView("detailed")}
            >
              <DetailedIcon />
            </button>
          </span>
        )}
        <button
          type="button"
          className="thr-btn thr-btn--primary"
          onClick={() => {
            setFilter("all");
            setEditing(null);
            setCreating(true);
          }}
        >
          + thread
        </button>
        {!mobile && (
          <button
            type="button"
            className="panel-refresh"
            aria-label="refresh threads"
            title="Refresh"
            onClick={threads.refresh}
          >
            ⟳
          </button>
        )}
      </div>
      <div className="thr-body">{body}</div>

      {pop && popThread && popStep && (
        <StepPopover
          key={`${pop.threadId}:${pop.stepId}`}
          thread={popThread}
          step={popStep}
          anchor={pop.anchor}
          mobile={mobile}
          onPatch={(patch) =>
            threads.updateStep(popThread.id, popStep.id, patch)
          }
          onMarkDone={() => {
            setPop(null);
            threads.completeStep(popThread.id, popStep.id);
          }}
          onDelete={() => {
            setPop(null);
            threads.deleteStep(popThread.id, popStep.id);
          }}
          onClose={() => setPop(null)}
        />
      )}
      {menu && menuThread && (
        <ThreadMenu
          thread={menuThread}
          anchor={menu.anchor}
          showAll={showAll.has(menuThread.id)}
          mobile={mobile}
          onClose={() => setMenu(null)}
          onToggleAll={() => {
            toggleAll(menuThread.id);
            setOpen((s) => new Set(s).add(menuThread.id));
          }}
          onLog={() => startEdit({ threadId: menuThread.id, mode: "log" })}
          onArchive={() => {
            if (editing?.threadId === menuThread.id) setEditing(null);
            threads.archiveThread(menuThread.id);
          }}
          onRestore={() => threads.restoreThread(menuThread.id)}
        />
      )}
      {threads.toast &&
        createPortal(
          threads.toast.kind === "error" ? (
            <div className="toast toast--threads" role="alert">
              <span>{threads.toast.message}</span>
              <button
                className="toast-dismiss"
                aria-label="dismiss"
                onClick={threads.dismissToast}
              >
                ×
              </button>
            </div>
          ) : (
            <div className="toast toast--action toast--threads" role="status">
              <span>{threads.toast.message}</span>
              {threads.toast.actionLabel && (
                <button className="toast-undo" onClick={threads.runToastAction}>
                  {threads.toast.actionLabel}
                </button>
              )}
            </div>
          ),
          document.body,
        )}
    </section>
  );
}
