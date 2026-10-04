import { Sheet } from "../../Sheet";
import type { CaptureHook } from "./useCapture";

/**
 * Quick capture (goal 15): the bottom bar's "+" opens this sheet from any view. It is
 * a second ENTRY POINT to the one capture path — `capture.submit` is the same
 * deferred `POST /scratch` + inline routing + Undo toast the Scratch tab uses — not a
 * second implementation. The draft is owned by the caller (AppShell), so closing the
 * sheet with text in it keeps the text for the next open.
 */
export function QuickCaptureSheet({
  capture,
  draft,
  setDraft,
  onClose,
  onRestore,
}: {
  capture: CaptureHook;
  draft: string;
  setDraft: (next: string | ((cur: string) => string)) => void;
  onClose: () => void;
  // Undo / a failed POST hands the text back: the caller puts it in the draft and
  // reopens the sheet.
  onRestore: (held: string) => void;
}) {
  function submit() {
    if (capture.submit(draft, onRestore)) {
      setDraft("");
      onClose();
    }
  }
  return (
    <Sheet label="Quick capture" onClose={onClose} className="sheet--capture">
      <h3 className="sheet-title">Quick capture</h3>
      <textarea
        className="quick-capture-input"
        placeholder="Dump a thought…"
        aria-label="Quick capture"
        value={draft}
        autoFocus
        rows={5}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          }
        }}
      />
      {capture.captureError && (
        <p className="panel-error">Capture failed: {capture.captureError}</p>
      )}
      <button
        type="button"
        className="capture-submit sheet-primary"
        disabled={!draft.trim()}
        onClick={submit}
      >
        Capture
      </button>
    </Sheet>
  );
}
