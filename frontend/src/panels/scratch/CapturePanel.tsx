import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import type {
  ReviewFields,
  RouterClassification,
  ScratchEntry,
} from "./useScratchPanel";
import { useScratchPanel } from "./useScratchPanel";
import { ReviewQueue } from "./ReviewPanel";
import {
  handleEnter,
  indentLine,
  outdentLine,
  type EditorState,
} from "./bulletEditor";

const STATE_LABEL: Record<string, string> = {
  unrouted: "Unrouted",
  // A capture the router is filing right now (the atomic route-once claim). The poll
  // can observe it mid-flight — a long paste takes ~25s — so it needs a label of its
  // own; without one it rendered as a blank chip.
  routing: "Filing…",
  routed_task: "→ Task",
  kept_note: "Note",
  in_review: "In review",
  resolved: "Resolved",
};

// A capture is "unresolved" until the router files it — those stay at the top of
// RECENT; everything else is a routed/resolved confirmation tail (dimmed, capped).
const UNRESOLVED_STATES = new Set(["unrouted", "routing", "in_review"]);
const ROUTED_TAIL_MAX = 5;

// Recent rows truncate to one line (full text via copy button + hover title).
const firstLine = (text: string) => text.split("\n")[0];

// A kept note's chip stays labeled "Note"; its hover shows WHERE the note was filed
// (the hierarchy path, or the default Doc), and clicking it opens that Doc — the
// newest entry is at the top, so no per-entry anchor is needed (goal 9).
function noteChipTitle(path: string | null): string {
  return path ? path.split("/").join(" / ") : "Dashboard — Notes";
}

// Capture files the whole editor, but the POST is HELD this long so an
// accidental capture is recoverable with one click (undo-by-never-sending — a
// mirror of the g4a deferred-delete). Undo fires zero backend writes.
const CAPTURE_UNDO_MS = 5000;

// `onRouted` lets the (separately-owned) Tasks panel refresh when routing or a
// review confirmation created a Google task — the panels share no state.
export function CapturePanel({ onRouted }: { onRouted?: () => void }) {
  const scratch = useScratchPanel();
  const [text, setText] = useState("");
  const [captureError, setCaptureError] = useState<string | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // A bullet keystroke sets both value and caret; the textarea is controlled, so
  // stash the desired selection and re-apply it once React has flushed the value.
  const pendingSel = useRef<{ start: number; end: number } | null>(null);

  // ── Editor/Recent split resize (goal 7a) ───────────────────────────────────
  // The editor and recent sections share space; dragging the handle between them
  // adjusts the split. Stored as a CSS custom property on the capture-panel.
  const panelRef = useRef<HTMLDivElement>(null);
  const [isResizing, setIsResizing] = useState(false);

  const handleResizeStart = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsResizing(true);
  };

  useEffect(() => {
    if (!isResizing) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!panelRef.current) return;
      const panel = panelRef.current;
      const rect = panel.getBoundingClientRect();
      // Dragging down increases editor, up decreases editor
      const offset = e.clientY - rect.top;
      const ratio = Math.max(0.5, Math.min(0.85, offset / rect.height));
      panel.style.setProperty("--editor-ratio", ratio.toString());
      // Leave the 2-row RECENT rest size for the dragged ratio split (see CSS).
      panel.dataset.split = "";
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizing]);

  useLayoutEffect(() => {
    if (pendingSel.current && taRef.current) {
      taRef.current.selectionStart = pendingSel.current.start;
      taRef.current.selectionEnd = pendingSel.current.end;
      pendingSel.current = null;
    }
  }, [text]);

  const apply = (next: EditorState) => {
    pendingSel.current = { start: next.selectionStart, end: next.selectionEnd };
    setText(next.value);
  };

  // ── Deferred-capture undo toast (goal 7a) ─────────────────────────────────
  // Shift+Enter clears the editor immediately but HOLDS the POST for ~5s behind
  // an "Undo" toast. The held text lives in a ref (survives re-renders); the
  // capture fn is read through a ref so the commit/flush closures stay stable and
  // an unmount can flush without re-firing on every render.
  const [showUndo, setShowUndo] = useState(false);
  const pendingRef = useRef<string | null>(null);
  // The classifier runs the instant a capture is queued (see `submit`), so the LLM
  // works through the ~5s undo window rather than after it — the toast hides its
  // latency. The in-flight proposal rides in a ref alongside the held text; undo
  // just drops it (the classify call has no side effects), commit hands it to the
  // POST so routing skips a second LLM call.
  const pendingClassifyRef =
    useRef<Promise<RouterClassification | null> | null>(null);
  const timerRef = useRef<number | null>(null);
  const captureFnRef = useRef(scratch.capture);
  const classifyFnRef = useRef(scratch.classify);
  const onRoutedRef = useRef(onRouted);
  useEffect(() => {
    captureFnRef.current = scratch.capture;
    classifyFnRef.current = scratch.classify;
    onRoutedRef.current = onRouted;
  });

  // Restore held text into the editor: prepend above anything the user typed
  // during the window (blank line between), else just set it. Zero writes.
  const restoreHeld = useCallback((held: string) => {
    setText((cur) => (cur.trim() ? `${held}\n\n${cur}` : held));
  }, []);

  // Send the still-held capture (window lapsed, or a new capture supersedes it —
  // one toast at a time). A POST failure surfaces the error and restores the text.
  const commitPending = useCallback(async () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const held = pendingRef.current;
    const classifyP = pendingClassifyRef.current;
    pendingRef.current = null;
    pendingClassifyRef.current = null;
    setShowUndo(false);
    if (held === null) return;
    try {
      // Await the classification kicked off at submit (already done, or nearly, by
      // now — it ran through the undo window). Its POST then routes inline (goal 7c)
      // without a second LLM call; if it filed a Google task, refresh the
      // (separately-owned) Tasks panel so it appears without a scheduler tick.
      const classification = classifyP ? await classifyP : null;
      const created = await captureFnRef.current(held, classification);
      setCaptureError(null);
      if (created?.routing_state === "routed_task") onRoutedRef.current?.();
    } catch (err) {
      setCaptureError((err as Error).message);
      restoreHeld(held);
    }
  }, [restoreHeld]);

  // Capture the WHOLE editor as one entry, verbatim — but defer the write. Clear
  // the editor now; the POST fires only once the undo window closes. Fired by the
  // Capture button and the Cmd/Ctrl+Enter secondary — never by a single keystroke.
  const submit = () => {
    if (!text.trim()) return;
    void commitPending(); // flush any previous still-held capture first
    pendingRef.current = text;
    // Kick the classifier off NOW so it runs during the undo window, not after it.
    // Swallow failures to null — commit then sends no classification and the backend
    // classifies inline (old behaviour), so a classify hiccup never blocks a capture.
    pendingClassifyRef.current = classifyFnRef.current(text).catch(() => null);
    setText("");
    setCaptureError(null);
    setShowUndo(true);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void commitPending();
    }, CAPTURE_UNDO_MS);
  };

  // Undo: cancel the held POST and restore the text — never sends anything.
  const undoCapture = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const held = pendingRef.current;
    pendingRef.current = null;
    // Drop the in-flight classification — it has no side effects, so an unresolved
    // classify call just gets ignored; nothing was persisted or written.
    pendingClassifyRef.current = null;
    setShowUndo(false);
    if (held !== null) restoreHeld(held);
  };

  // On unmount, flush a still-held capture so it is never silently lost (fire the
  // POST directly, no state updates on a gone component).
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      const held = pendingRef.current;
      pendingRef.current = null;
      if (held && held.trim()) void captureFnRef.current(held).catch(() => {});
    };
  }, []);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget;
    const snap: EditorState = {
      value: text,
      selectionStart: ta.selectionStart,
      selectionEnd: ta.selectionEnd,
    };
    // Cmd|Ctrl+Enter = deliberate secondary capture. Shift+Enter was removed —
    // it fired accidental captures during normal editing (goal 7d). Plain Enter
    // never submits (it continues a bullet); capture is button-first now.
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void submit();
      return;
    }
    if (e.key === "Enter") {
      const next = handleEnter(snap);
      if (next) {
        e.preventDefault(); // bullet continue/exit; else fall through to a plain newline
        apply(next);
      }
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault(); // Tab is captive inside the editor
      apply(e.shiftKey ? outdentLine(snap) : indentLine(snap));
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault(); // blur so keyboard users can tab away past the captive editor
      ta.blur();
    }
  };

  const routeNow = () => {
    scratch.routeNow().then((created) => {
      if (created) onRouted?.();
    });
  };

  const confirmItem = async (
    itemId: number,
    override?: { destination?: string; fields?: ReviewFields },
  ) => {
    const created = await scratch.confirmItem(itemId, override);
    if (created) onRouted?.();
    return created;
  };

  // RECENT shows unresolved captures (unrouted + in-review) first, then only the
  // ~5 most-recent routed/resolved as a dimmed confirmation tail — nothing older.
  // `entries` is already newest-first (server orders by desc id).
  const unresolved = scratch.entries.filter((e) =>
    UNRESOLVED_STATES.has(e.routing_state),
  );
  const routedTail = scratch.entries
    .filter((e) => !UNRESOLVED_STATES.has(e.routing_state))
    .slice(0, ROUTED_TAIL_MAX);
  const recent: Array<ScratchEntry & { dimmed?: boolean }> = [
    ...unresolved,
    ...routedTail.map((e) => ({ ...e, dimmed: true })),
  ];

  // "Route now" is the ~15-min backstop for captures that failed inline routing —
  // only meaningful when an unrouted entry exists (routing is instant otherwise).
  const hasUnrouted = scratch.entries.some(
    (e) => e.routing_state === "unrouted",
  );

  return (
    <section className="panel capture-panel" ref={panelRef}>
      <div className="panel-head">
        <h2>Scratchpad</h2>
      </div>

      <form
        className="capture-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={taRef}
          className="capture-input"
          value={text}
          placeholder="Dump a thought: `- ` starts a bullet, Capture button (or ⌘/Ctrl+Enter) files it…"
          rows={12}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          type="submit"
          className="capture-submit"
          disabled={!text.trim()}
        >
          Capture
        </button>
      </form>

      {captureError && (
        <p className="panel-error">Capture failed: {captureError}</p>
      )}
      {scratch.error && <p className="panel-error">{scratch.error}</p>}

      <ReviewQueue
        items={scratch.reviewItems}
        docPaths={scratch.docPaths}
        onConfirm={confirmItem}
        onDismiss={scratch.dismissItem}
      />

      <div
        className="scratch-resize-handle"
        onMouseDown={handleResizeStart}
        role="separator"
        aria-label="Resize editor and recent sections"
      >
        <div className="scratch-resize-bar" />
      </div>

      <div className="scratch-recent">
        <div className="scratch-recent-head">
          <h3>Recent</h3>
          {hasUnrouted && (
            <button
              className="route-now-btn"
              onClick={routeNow}
              disabled={scratch.busy}
              title="Route all unrouted entries now"
            >
              {scratch.busy ? "Routing…" : "Route now"}
            </button>
          )}
        </div>
        {scratch.isLoading ? (
          <p className="panel-status">Loading…</p>
        ) : recent.length === 0 ? (
          <p className="panel-status">Nothing captured yet.</p>
        ) : (
          <ul className="scratch-entries">
            {recent.map((e) => (
              <li key={e.id} className={e.dimmed ? "scratch-entry--dim" : ""}>
                <span className="scratch-text" title={e.text}>
                  {firstLine(e.text)}
                </span>
                <button
                  type="button"
                  className="scratch-copy"
                  title="Copy full text"
                  onClick={() => navigator.clipboard.writeText(e.text)}
                >
                  ⧉
                </button>
                {e.routing_state === "kept_note" && e.routed_doc_url ? (
                  <a
                    className="scratch-badge state-kept_note scratch-badge--link"
                    href={e.routed_doc_url}
                    target="_blank"
                    rel="noreferrer"
                    title={`Open in Docs — ${noteChipTitle(e.routed_doc_path)}`}
                  >
                    {STATE_LABEL.kept_note}
                  </a>
                ) : (
                  <span className={`scratch-badge state-${e.routing_state}`}>
                    {STATE_LABEL[e.routing_state] ?? e.routing_state}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {showUndo &&
        createPortal(
          <div className="toast toast--action toast--capture" role="status">
            <span>Captured — filing in a moment…</span>
            <button className="toast-undo" onClick={undoCapture}>
              Undo
            </button>
          </div>,
          document.body,
        )}
    </section>
  );
}
