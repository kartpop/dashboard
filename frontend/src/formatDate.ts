/** Google APIs return all-day values as `YYYY-MM-DD` and timed values as full ISO datetimes. */
export function formatDate(value: string): string {
  const date = new Date(value);
  return value.length > 10
    ? date.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : date.toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** A compact relative time ("3h ago", "2d ago") for news timestamps (goal 11). */
export function formatRelative(value: string | null): string {
  if (!value) return "";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(value).toLocaleDateString(undefined, { dateStyle: "medium" });
}

/**
 * A run-day header label from an IST `YYYY-MM-DD` run_date: "Today" / "Yesterday",
 * else weekday + date. Used to spine the News feed by run (goal 11).
 */
export function formatRunDay(runDate: string): string {
  const today = new Date();
  const todayKey = today.toISOString().slice(0, 10);
  const yest = new Date(today.getTime() - 86400000).toISOString().slice(0, 10);
  const weekday = new Date(`${runDate}T00:00:00`).toLocaleDateString(
    undefined,
    {
      weekday: "short",
      day: "2-digit",
      month: "short",
    },
  );
  if (runDate === todayKey) return `Today · ${weekday}`;
  if (runDate === yest) return `Yesterday · ${weekday}`;
  return weekday;
}
