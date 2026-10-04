import { useCallback, useEffect, useRef, useState } from "react";
import type { RouterClassification } from "./useScratchPanel";
import { useScratchPanel } from "./useScratchPanel";

// Capture files the whole editor, but the POST is HELD this long so an
// accidental capture is recoverable with one click (undo-by-never-sending — a
// mirror of the g4a deferred-delete). Undo fires zero backend writes.
const CAPTURE_UNDO_MS = 5000;

interface Held {
  text: string;
  // Puts the text back where it came from (the Scratch editor or the quick-capture
  // sheet) on Undo or a failed POST.
  restore: (held: string) => void;
  // The classifier runs the instant a capture is queued (see `submit`), so the LLM
  // works through the ~5s undo window rather than after it — the toast hides its
  // latency. Undo just drops it (the classify call has no side effects); commit
  // hands it to the POST so routing skips a second LLM call.
  classify: Promise<RouterClassification | null>;
}

/**
 * The scratchpad's data + its one deferred-capture path (goal 7a; lifted in goal 15).
 * Owned by `AppShell` so the Scratch tab's editor and the bottom bar's quick-capture
 * sheet go through the SAME `POST /scratch` + inline routing + undo toast, and share
 * one `entries` list — a quick capture shows in Recent at once.
 *
 * `setOnRouted` is called by DashboardPage (which owns the tasks hook) so a capture
 * that filed a Google task refreshes the task columns without this hook knowing them.
 */
export function useCapture() {
  const scratch = useScratchPanel();
  const [showUndo, setShowUndo] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  // The held capture + timer live in refs (survive re-renders, so the deferred
  // write isn't lost); the capture fn is read through a ref so the commit/flush
  // closures stay stable and an unmount can flush without re-firing every render.
  const heldRef = useRef<Held | null>(null);
  const timerRef = useRef<number | null>(null);
  const onRoutedRef = useRef<(() => void) | undefined>(undefined);
  const captureFnRef = useRef(scratch.capture);
  const classifyFnRef = useRef(scratch.classify);
  useEffect(() => {
    captureFnRef.current = scratch.capture;
    classifyFnRef.current = scratch.classify;
  });

  // Send the still-held capture (window lapsed, or a new capture supersedes it —
  // one toast at a time). A POST failure surfaces the error and restores the text.
  const commitPending = useCallback(async () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const held = heldRef.current;
    heldRef.current = null;
    setShowUndo(false);
    if (held === null) return;
    try {
      // Await the classification kicked off at submit (already done, or nearly, by
      // now — it ran through the undo window). Its POST then routes inline (goal 7c)
      // without a second LLM call; if it filed a Google task, refresh the
      // (separately-owned) Tasks panel so it appears without a scheduler tick.
      const classification = await held.classify;
      const created = await captureFnRef.current(held.text, classification);
      setCaptureError(null);
      if (created?.routing_state === "routed_task") onRoutedRef.current?.();
    } catch (err) {
      setCaptureError((err as Error).message);
      held.restore(held.text);
    }
  }, []);

  // Capture `text` as one entry, verbatim — but defer the write. The caller clears
  // its editor now; the POST fires only once the undo window closes. Returns false
  // (nothing queued) for blank text.
  const submit = useCallback(
    (text: string, restore: (held: string) => void): boolean => {
      if (!text.trim()) return false;
      void commitPending(); // flush any previous still-held capture first
      heldRef.current = {
        text,
        restore,
        // Swallow failures to null — commit then sends no classification and the
        // backend classifies inline, so a classify hiccup never blocks a capture.
        classify: classifyFnRef.current(text).catch(() => null),
      };
      setCaptureError(null);
      setShowUndo(true);
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        void commitPending();
      }, CAPTURE_UNDO_MS);
      return true;
    },
    [commitPending],
  );

  const setOnRouted = useCallback((fn: (() => void) | undefined) => {
    onRoutedRef.current = fn;
  }, []);

  // Undo: cancel the held POST and restore the text — never sends anything.
  const undo = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const held = heldRef.current;
    heldRef.current = null;
    setShowUndo(false);
    if (held !== null) held.restore(held.text);
  }, []);

  // On unmount, flush a still-held capture so it is never silently lost (fire the
  // POST directly, no state updates on a gone component).
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      const held = heldRef.current;
      heldRef.current = null;
      if (held && held.text.trim())
        void captureFnRef.current(held.text).catch(() => {});
    };
  }, []);

  return {
    scratch,
    submit,
    undo,
    showUndo,
    captureError,
    setOnRouted,
  };
}

export type CaptureHook = ReturnType<typeof useCapture>;
