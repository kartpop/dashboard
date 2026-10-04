import { useSyncExternalStore } from "react";

/**
 * The phone breakpoint (goal 15), in CSS px. KEEP IN SYNC with every
 * `@media (max-width: 640px)` block in `index.css` — the hook decides which tree
 * renders (bottom bar vs rail, tabs vs grid, sheet vs popover) and the CSS styles it,
 * so the two must flip at the same width.
 */
export const MOBILE_MAX = 640;

const QUERY = `(max-width: ${MOBILE_MAX}px)`;

function subscribe(onChange: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

const getSnapshot = () => window.matchMedia(QUERY).matches;
const getServerSnapshot = () => false;

/**
 * True at phone width. Re-renders when the phone rotates or a desktop window is
 * resized across the line. Use it ONLY where the mobile DOM is a different shape;
 * sizing, spacing and hiding are CSS's job (see `.claude/rules/frontend.md`).
 */
export function useIsMobile(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
