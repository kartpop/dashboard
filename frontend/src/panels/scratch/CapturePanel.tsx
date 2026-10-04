import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import type { ReviewFields, ScratchEntry } from "./useScratchPanel";
import type { CaptureHook } from "./useCapture";
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

// `capture` is the lifted scratch data + deferred-capture path (goal 15: owned by
// AppShell so the quick-capture sheet shares it). `onRouted` lets the
// (separately-owned) Tasks panel refresh when routing or a review confirmation
// created a Google task — the panels share no state. `mobile` drops the
// editor/RECENT drag split (the phone's Scratch tab fills the screen with the editor
// over a 2-row RECENT, the desktop column's rest sizing — see index.css).
export function CapturePanel({
  capture,
  onRouted,
  mobile,
}: {
  capture: CaptureHook;
  onRouted?: () => void;
  mobile?: boolean;
}) {
  const { scratch, captureError } = capture;
  const [text, setText] = useState("");
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

  // Restore held text into the editor (Undo, or a failed POST): prepend above
  // anything the user typed during the window (blank line between), else just set
  // it. Zero writes.
  const restoreHeld = useCallback((held: string) => {
    setText((cur) => (cur.trim() ? `${held}\n\n${cur}` : held));
  }, []);

  // Capture the WHOLE editor as one entry, verbatim — but defer the write (the
  // shared path in useCapture). Clear the editor now; the POST fires only once the
  // undo window closes. Fired by the Capture button and the Cmd/Ctrl+Enter
  // secondary — never by a single keystroke.
  const submit = () => {
    if (capture.submit(text, restoreHeld)) setText("");
  };

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
    <section
      className={`panel capture-panel${mobile ? " capture-panel--mobile" : ""}`}
      ref={panelRef}
    >
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
          placeholder={
            mobile
              ? "Dump a thought. `- ` starts a bullet; Capture files it…"
              : "Dump a thought: `- ` starts a bullet, Capture button (or ⌘/Ctrl+Enter) files it…"
          }
          rows={mobile ? 6 : 12}
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

      {!mobile && (
        <div
          className="scratch-resize-handle"
          onMouseDown={handleResizeStart}
          role="separator"
          aria-label="Resize editor and recent sections"
        >
          <div className="scratch-resize-bar" />
        </div>
      )}

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
    </section>
  );
}

/**
 * The deferred-capture Undo toast. Rendered once by AppShell (it owns the capture
 * state) so it shows whichever surface captured — the Scratch editor or the quick-
 * capture sheet — and from any view.
 */
export function CaptureUndoToast({ capture }: { capture: CaptureHook }) {
  if (!capture.showUndo) return null;
  return createPortal(
    <div className="toast toast--action toast--capture" role="status">
      <span>Captured — filing in a moment…</span>
      <button className="toast-undo" onClick={capture.undo}>
        Undo
      </button>
    </div>,
    document.body,
  );
}
