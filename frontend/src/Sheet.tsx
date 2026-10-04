import { type ReactNode, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

/**
 * A generic bottom sheet (goal 15): a scrim plus a panel sliding up from the bottom
 * edge, portaled to <body>. It is the phone's stand-in for anchored popovers and
 * menus, so it knows nothing about any panel — a shared leaf like `api.ts`, which
 * any panel may import. Tapping the scrim or pressing Esc calls `onClose`; the
 * caller decides what closing means (e.g. a step editor saves first).
 */
export function Sheet({
  label,
  onClose,
  children,
  className,
}: {
  /** Accessible name for the dialog. */
  label: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeRef.current();
    }
    document.addEventListener("keydown", onKey);
    // The page behind a sheet shouldn't scroll under the finger.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Move focus into the sheet (an autofocused field may already hold it).
    const panel = panelRef.current;
    if (panel && !panel.contains(document.activeElement)) panel.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, []);

  return createPortal(
    <>
      <div className="sheet-scrim" onClick={() => closeRef.current()} />
      <div
        ref={panelRef}
        className={`sheet${className ? ` ${className}` : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
      >
        <div className="sheet-grab" aria-hidden />
        {children}
      </div>
    </>,
    document.body,
  );
}
