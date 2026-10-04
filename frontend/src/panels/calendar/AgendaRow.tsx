import {
  type ReactNode,
  type Ref,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Sheet } from "../../Sheet";
import {
  type CalendarEvent,
  type CalendarStripState,
  toISODate,
} from "./useCalendarStrip";

const NOW_TICK_MS = 30 * 1000;
const MAX_ATTENDEES = 12;

const RSVP_GLYPH: Record<string, string> = {
  accepted: "✓",
  declined: "✕",
  tentative: "~",
  needsAction: "·",
};

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}

/**
 * The phone's calendar (goal 15): the viewed day's timed events as one row of cards
 * that scrolls sideways — past ones dimmed, a red now marker between past and
 * upcoming (today only), the next one flagged "Next ·". The hour axis can't fit
 * 390px, so this replaces `CalendarStrip`'s axis; it reads the same
 * `useCalendarStrip` state (passed in, so the header's day nav drives it). Tapping a
 * card opens its details in a sheet with Join Meet — the tap path for the desktop
 * hover card and click-to-copy.
 */
export function AgendaRow({ cal }: { cal: CalendarStripState }) {
  const [now, setNow] = useState(() => Date.now());
  const [openId, setOpenId] = useState<string | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  const nowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), NOW_TICK_MS);
    return () => window.clearInterval(t);
  }, []);

  const timed = useMemo(
    () =>
      cal.events
        .filter((e) => !e.all_day)
        .sort((a, b) => Date.parse(a.start) - Date.parse(b.start)),
    [cal.events],
  );

  // An event is past once it has ended; the now marker sits before the first one
  // that hasn't. "Next" is the first that hasn't started yet.
  const firstLive = timed.findIndex((e) => Date.parse(e.end) > now);
  const nowIndex = cal.isToday
    ? firstLive === -1
      ? timed.length
      : firstLive
    : -1;
  const nextId = cal.isToday
    ? timed.find((e) => Date.parse(e.start) > now)?.id
    : undefined;

  // On load (and on day change), scroll so the now marker sits near the left edge.
  const eventKey = timed.map((e) => e.id).join();
  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    const marker = nowRef.current;
    row.scrollLeft = marker ? Math.max(0, marker.offsetLeft - 16) : 0;
  }, [cal.viewedDate, eventKey]);

  const open = openId ? timed.find((e) => e.id === openId) : undefined;
  const nowLabel = new Date(now).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });

  const pastDay = !cal.isToday && cal.viewedDate < toISODate(new Date());
  const items: ReactNode[] = [];
  timed.forEach((e, i) => {
    if (i === nowIndex)
      items.push(<NowMarker key="now" ref={nowRef} label={nowLabel} />);
    const past = cal.isToday ? Date.parse(e.end) <= now : pastDay;
    const isNext = e.id === nextId;
    items.push(
      <button
        key={e.id}
        type="button"
        className={`agenda-card${past ? " agenda-card--past" : ""}${isNext ? " agenda-card--next" : ""}`}
        onClick={() => setOpenId(e.id)}
      >
        <b>
          {isNext && "Next · "}
          {fmtTime(e.start)}
        </b>
        <span>{e.title ?? "(untitled)"}</span>
      </button>,
    );
  });
  if (nowIndex === timed.length)
    items.push(<NowMarker key="now" ref={nowRef} label={nowLabel} />);

  return (
    <>
      <div className="agenda-row" ref={rowRef} aria-label="Events">
        {cal.error ? (
          <div className="agenda-card agenda-card--empty">{cal.error}</div>
        ) : timed.length === 0 ? (
          <div className="agenda-card agenda-card--empty">
            {cal.isLoading ? "Loading…" : "No events"}
          </div>
        ) : (
          items
        )}
      </div>
      {open && <EventSheet event={open} onClose={() => setOpenId(null)} />}
    </>
  );
}

function NowMarker({
  ref,
  label,
}: {
  ref: Ref<HTMLDivElement>;
  label: string;
}) {
  return (
    <div className="agenda-now" ref={ref} aria-label={`Now, ${label}`}>
      {label}
    </div>
  );
}

function EventSheet({
  event,
  onClose,
}: {
  event: CalendarEvent;
  onClose: () => void;
}) {
  return (
    <Sheet label="Event details" onClose={onClose}>
      <h3 className="sheet-title sheet-title--wrap">
        {event.title ?? "(untitled)"}
      </h3>
      <div className="sheet-sub">
        {fmtTime(event.start)} – {fmtTime(event.end)}
      </div>
      {event.location && <p className="agenda-sheet-loc">{event.location}</p>}
      {event.organizer && (
        <p className="agenda-sheet-org">Organizer: {event.organizer}</p>
      )}
      {event.attendees.length > 0 && (
        <ul className="agenda-sheet-att">
          {event.attendees.slice(0, MAX_ATTENDEES).map((a, i) => (
            <li key={i}>
              <span className="tt-att-glyph">
                {RSVP_GLYPH[a.response_status ?? ""] ?? "·"}
              </span>
              {a.name ?? a.email ?? "(unknown)"}
            </li>
          ))}
          {event.attendees.length > MAX_ATTENDEES && (
            <li className="agenda-sheet-more">
              +{event.attendees.length - MAX_ATTENDEES} more
            </li>
          )}
        </ul>
      )}
      {event.meet_link && (
        <a
          className="sheet-primary agenda-join"
          href={event.meet_link}
          target="_blank"
          rel="noopener noreferrer"
        >
          Join Meet
        </a>
      )}
    </Sheet>
  );
}
