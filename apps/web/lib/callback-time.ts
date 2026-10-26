// Display helpers for the Upcoming Callbacks list. A callback's time is a
// FUTURE moment (or one just missed), the opposite of
// lib/notification-date-groups.ts's always-past notification timestamps —
// so "Today"/"Yesterday" there needs "Today"/"Tomorrow"/dated here instead,
// and "5m ago" needs "in 5m"/"Overdue".

/** How many LOCAL calendar days `to` is after `from` (0 = same day, 1 =
 * tomorrow, -1 = yesterday), via each side's own UTC-normalized Y/M/D
 * rather than subtracting real timestamps — that would divide by a fixed
 * 86,400,000ms and misfire across a DST transition, where a local day isn't
 * always exactly 24h. Deliberately duplicated from
 * notification-date-groups.ts's identical helper instead of imported: that
 * file is itself imported with the "@/lib/..." alias everywhere else in
 * this app, which Next's bundler resolves fine but this repo's node-native
 * test runner (no bundler, no path-alias config) cannot. */
function localCalendarDayDiff(from: Date, to: Date): number {
  const fromUTC = Date.UTC(from.getFullYear(), from.getMonth(), from.getDate());
  const toUTC = Date.UTC(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((toUTC - fromUTC) / 86_400_000);
}

/** "Today" / "Tomorrow" / "Yesterday" (a callback gone by just one day,
 * i.e. overdue since yesterday) / a formatted date, for the LOCAL calendar
 * day `iso` falls on relative to `now`. An unparsable `iso` reads as
 * "Unknown date" rather than throwing or silently sorting as "Today". */
export function callbackDayLabel(iso: string, now: Date = new Date(), locale?: string): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) return "Unknown date";

  const diffDays = localCalendarDayDiff(now, target);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Tomorrow";
  if (diffDays === -1) return "Yesterday";

  return new Date(target.getFullYear(), target.getMonth(), target.getDate()).toLocaleDateString(locale, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/** True once `iso` has already passed `now` — a callback nobody has marked
 * done yet past its own scheduled time. */
export function isCallbackOverdue(iso: string, now: Date = new Date()): boolean {
  const target = new Date(iso);
  return !Number.isNaN(target.getTime()) && target.getTime() <= now.getTime();
}

/** True if `iso` parses to a real moment strictly after `now` — the guard
 * behind the Callback Scheduled time picker (PR #845 review: a browser's
 * datetime-local `min` is only a hint, not enforcement). Not simply
 * `!isCallbackOverdue`: that function reads an unparsable value as "not
 * overdue", which would wrongly pass here as a valid future time. */
export function isValidFutureCallbackTime(iso: string, now: Date = new Date()): boolean {
  const target = new Date(iso);
  return !Number.isNaN(target.getTime()) && target.getTime() > now.getTime();
}

/** "in 45m" / "in 3h" / "in 2d" / "Overdue" for a single row, relative to
 * `now`. Mirrors formatRelativeTime's granularity (minutes, then hours,
 * then days) but counts forward instead of back, and collapses to a single
 * "Overdue" instead of a negative duration once the moment has passed —
 * isCallbackOverdue is the thing that actually flags it visually; this is
 * just the row's own time label. */
export function callbackRelativeLabel(iso: string, now: Date = new Date()): string {
  const target = new Date(iso);
  if (Number.isNaN(target.getTime())) return "";
  if (isCallbackOverdue(iso, now)) return "Overdue";

  const diffSec = Math.floor((target.getTime() - now.getTime()) / 1000);
  if (diffSec < 60) return "in under a minute";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `in ${diffMin}m`;
  const diffHours = Math.round(diffMin / 60);
  if (diffHours < 24) return `in ${diffHours}h`;
  return `in ${Math.round(diffHours / 24)}d`;
}
