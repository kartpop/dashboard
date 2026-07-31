import { useEffect, useMemo, useState } from "react";
import { layoutBlocks, type StripEvent } from "./layoutStrip";
import { useCalendarStrip, type CalendarEvent } from "./useCalendarStrip";

const DEFAULT_START = 8 * 60; // 08:00
const DEFAULT_END = 19 * 60; // 19:00
const STEP = 60; // chevrons shift the window by 1h
const NOW_TICK_MS = 10 * 1000; // now-marker lag stays under 10s of wall clock

/** Minutes-from-midnight for a timed ISO string (local == IST on the owner's box). */
function istMinutes(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Compact viewed-date label for the inline day-nav pill, e.g. "Thu, 30 Jul". */
function fmtViewed(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString([], {
    weekday: "short",
    day: "numeric",
    month: "short",
  });
}

function fmtNow(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** The strip treats "no RSVP entry for me" (solo/own events) as accepted. */
function isAccepted(event: CalendarEvent): boolean {
  return event.my_response === null || event.my_response === "accepted";
}

const RSVP_GLYPH: Record<string, string> = {
  accepted: "✓",
  declined: "✕",
  tentative: "~",
  needsAction: "·",
};

const MAX_TOOLTIP_ATTENDEES = 8;

interface PickerState {
  ids: string[];
  leftPct: number;
}

export function CalendarStrip() {
  const cal = useCalendarStrip();
  const [windowStart, setWindowStart] = useState(DEFAULT_START);
  const [windowEnd, setWindowEnd] = useState(DEFAULT_END);
  const [toast, setToast] = useState<string | null>(null);
  const [picker, setPicker] = useState<PickerState | null>(null);
  const [nowMin, setNowMin] = useState(() =>
    istMinutes(new Date().toISOString()),
  );

  // The now-marker recomputes without a reload.
  useEffect(() => {
    const tick = () => setNowMin(istMinutes(new Date().toISOString()));
    tick();
    const timer = window.setInterval(tick, NOW_TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  // Auto-dismiss the copy toast.
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2000);
    return () => window.clearTimeout(t);
  }, [toast]);

  const byId = useMemo(() => {
    const m = new Map<string, CalendarEvent>();
    for (const e of cal.events) m.set(e.id, e);
    return m;
  }, [cal.events]);

  const layout = useMemo(() => {
    const stripEvents: StripEvent[] = cal.events.map((e) => ({
      id: e.id,
      title: e.title,
      startMin: e.all_day ? 0 : istMinutes(e.start),
      endMin: e.all_day ? 0 : istMinutes(e.end),
      allDay: e.all_day,
      accepted: isAccepted(e),
    }));
    return layoutBlocks(stripEvents, windowStart, windowEnd);
  }, [cal.events, windowStart, windowEnd]);

  // Day/window changes invalidate an open picker's cluster.
  useEffect(() => {
    setPicker(null);
  }, [cal.viewedDate, windowStart, windowEnd]);

  const span = windowEnd - windowStart;
  const nowPct = ((nowMin - windowStart) / span) * 100;
  const showNow = cal.isToday && nowMin >= windowStart && nowMin <= windowEnd;

  // Hour ticks across the window (whole hours only).
  const hourTicks: number[] = [];
  for (
    let h = Math.ceil(windowStart / 60);
    h <= Math.floor(windowEnd / 60);
    h++
  ) {
    hourTicks.push(h);
  }

  const shiftWindow = (delta: number) => {
    setWindowStart((s) => Math.max(0, Math.min(24 * 60 - STEP, s + delta)));
    setWindowEnd((e) => Math.min(24 * 60, Math.max(STEP, e + delta)));
  };

  const copyMeetLink = (event: CalendarEvent) => {
    if (!event.meet_link) {
      setToast("No Meet link for this event");
      return;
    }
    void navigator.clipboard
      .writeText(event.meet_link)
      .then(() => setToast("Meet link copied"))
      .catch(() => setToast("Couldn’t copy — try the tooltip’s open link"));
  };

  const onBlockClick = (
    event: CalendarEvent,
    clusterIds: string[],
    leftPct: number,
    e: React.MouseEvent,
  ) => {
    // Alt+click = secondary open-in-new-tab path; plain click copies the link.
    if (e.altKey) {
      if (event.meet_link) window.open(event.meet_link, "_blank", "noopener");
      return;
    }
    // Overlapping cluster → let the owner pick which event's link to grab.
    if (clusterIds.length > 1) {
      setPicker((p) =>
        p && p.ids.join() === clusterIds.join()
          ? null
          : { ids: clusterIds, leftPct: Math.min(leftPct, 70) },
      );
      return;
    }
    copyMeetLink(event);
  };

  const renderTooltip = (event: CalendarEvent, leftPct: number) => (
    <div
      className={`strip-tooltip${leftPct > 60 ? " tt-right" : ""}`}
      role="tooltip"
    >
      <div className="tt-title">{event.title ?? "(untitled)"}</div>
      <div className="tt-time">
        {fmtTime(event.start)} – {fmtTime(event.end)}
      </div>
      {event.location && <div className="tt-loc">{event.location}</div>}
      {event.organizer && (
        <div className="tt-org">Organizer: {event.organizer}</div>
      )}
      {event.attendees.length > 0 && (
        <div className="tt-att">
          {event.attendees.slice(0, MAX_TOOLTIP_ATTENDEES).map((a, i) => (
            <div key={i} className="tt-att-row">
              <span className="tt-att-glyph">
                {RSVP_GLYPH[a.response_status ?? ""] ?? "·"}
              </span>
              {a.name ?? a.email ?? "(unknown)"}
            </div>
          ))}
          {event.attendees.length > MAX_TOOLTIP_ATTENDEES && (
            <div className="tt-att-row tt-att-more">
              +{event.attendees.length - MAX_TOOLTIP_ATTENDEES} more
            </div>
          )}
        </div>
      )}
      {event.meet_link && (
        <a
          className="tt-open"
          href={event.meet_link}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
        >
          Open in new tab ↗
        </a>
      )}
    </div>
  );

  return (
    <div className="calendar-strip">
      {/* Compact inline day-nav: ‹ · viewed date (+Today) · › — a single-row pill
          beside the title, so the strip no longer stacks a second row. */}
      <div className="strip-daynav">
        <button
          className="strip-daypill"
          onClick={() => cal.shiftDay(-1)}
          title="Previous day"
          aria-label="Previous day"
        >
          ‹
        </button>
        <span className="strip-viewed">
          {fmtViewed(cal.viewedDate)}
          {!cal.isToday && (
            <button className="strip-today" onClick={cal.goToday}>
              Today
            </button>
          )}
        </span>
        <button
          className="strip-daypill"
          onClick={() => cal.shiftDay(1)}
          title="Next day"
          aria-label="Next day"
        >
          ›
        </button>
      </div>

      <div className="strip-body">
        <button
          className="strip-chevron"
          onClick={() => shiftWindow(-STEP)}
          disabled={windowStart <= 0}
          title={
            layout.beforeCount > 0
              ? `Earlier — ${layout.beforeCount} meeting(s) before ${fmtNow(windowStart)}`
              : "Earlier"
          }
          aria-label="Show earlier hours"
        >
          ‹
          {layout.beforeCount > 0 && (
            <span className="strip-hint-badge">{layout.beforeCount}</span>
          )}
        </button>

        <div className="strip-axis">
          {/* Meeting blocks above the axis line — overlaps stack, accepted in front. */}
          <div className="strip-blocks">
            {layout.blocks.map((b) => {
              const event = byId.get(b.id);
              if (!event) return null;
              // Stagger stacked blocks down a few px (aligned bottoms) so every
              // event in a cluster keeps a visible, clickable top sliver.
              const stagger = Math.min(b.stackIndex, 3) * 6;
              return (
                <div
                  key={b.id}
                  className={`strip-block ${b.accepted ? "sb-accepted" : "sb-pending"}`}
                  style={{
                    left: `${b.leftPct}%`,
                    width: `${b.widthPct}%`,
                    top: `${2 + stagger}px`,
                    height: `${34 - stagger}px`,
                    zIndex: b.zIndex,
                  }}
                  role="button"
                  tabIndex={0}
                  onClick={(e) =>
                    onBlockClick(event, b.clusterIds, b.leftPct, e)
                  }
                >
                  <span className="strip-block-title">
                    {event.title ?? "(untitled)"}
                  </span>
                  {b.extraCount > 0 && (
                    <span className="strip-block-more">+{b.extraCount}</span>
                  )}
                  {renderTooltip(event, b.leftPct)}
                </div>
              );
            })}
          </div>

          {/* Axis line with hour ticks + labels. */}
          <div className="strip-line" />
          {hourTicks.map((h) => {
            const pct = ((h * 60 - windowStart) / span) * 100;
            return (
              <div key={h} className="strip-tick" style={{ left: `${pct}%` }}>
                <span className="strip-tick-label">{h % 12 || 12}</span>
              </div>
            );
          })}

          {showNow && (
            <div className="strip-now" style={{ left: `${nowPct}%` }}>
              <span className="strip-now-label">{fmtNow(nowMin)}</span>
            </div>
          )}

          {/* Overlap picker: choose which concurrent meeting's link to copy. */}
          {picker && (
            <>
              <div
                className="strip-picker-backdrop"
                onClick={() => setPicker(null)}
              />
              <div
                className="strip-picker"
                style={{ left: `${picker.leftPct}%` }}
              >
                {picker.ids.map((id) => {
                  const event = byId.get(id);
                  if (!event) return null;
                  return (
                    <button
                      key={id}
                      className="strip-picker-row"
                      onClick={() => {
                        copyMeetLink(event);
                        setPicker(null);
                      }}
                    >
                      <span
                        className={`spr-dot ${isAccepted(event) ? "sb-accepted" : "sb-pending"}`}
                      />
                      <span className="spr-time">
                        {fmtTime(event.start)}–{fmtTime(event.end)}
                      </span>
                      <span className="spr-title">
                        {event.title ?? "(untitled)"}
                      </span>
                      {!event.meet_link && (
                        <span className="spr-nolink">no link</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </>
          )}

          {cal.error && (
            <span className="strip-status strip-error">{cal.error}</span>
          )}
        </div>

        <button
          className="strip-chevron"
          onClick={() => shiftWindow(STEP)}
          disabled={windowEnd >= 24 * 60}
          title={
            layout.afterCount > 0
              ? `Later — ${layout.afterCount} meeting(s) after ${fmtNow(windowEnd)}`
              : "Later"
          }
          aria-label="Show later hours"
        >
          ›
          {layout.afterCount > 0 && (
            <span className="strip-hint-badge">{layout.afterCount}</span>
          )}
        </button>

        <button
          className={`strip-refresh${cal.isLoading ? " loading" : ""}`}
          onClick={cal.refresh}
          title="Refresh calendar"
          aria-label="Refresh calendar"
        >
          ⟳
        </button>
      </div>

      {toast && <div className="strip-toast">{toast}</div>}
    </div>
  );
}
