import { useEffect, useRef } from "react";

/**
 * Run `tick` every `ms` while the tab is visible (goal 17). Ticks are skipped while
 * the tab is hidden, and one catch-up tick runs when it becomes visible again — a
 * backgrounded laptop or phone tab costs nothing.
 */
export function usePoll(tick: () => void, ms: number): void {
  const tickRef = useRef(tick);
  useEffect(() => {
    tickRef.current = tick;
  });

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState !== "hidden") tickRef.current();
    }, ms);
    const onVisible = () => {
      if (document.visibilityState === "visible") tickRef.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [ms]);
}
