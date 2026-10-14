// Date-grouped headers for the notifications feed (Today / Yesterday / the
// actual date) — without this, an unread backlog a recruiter never clears
// renders as one endless list of "3d ago", "4d ago", ... with no sense of
// where one day ends and the next begins. Pure (no DOM, no Date.now() baked
// in — `now` is a parameter) so this has unit test coverage without a
// React test renderer, same rationale as lib/notification-filters.ts.
//
// Grouping is done by LOCAL date, not UTC: "Today"/"Yesterday" must match
// what the viewer actually sees on their own clock, not the server's.
// Assumes the input is already sorted newest-first (true for every caller
// today — the feed itself, and notifications-cache.ts's cached copy of
// it) — grouping just merges consecutive same-day items rather than
// re-sorting, so an out-of-order input (e.g. a sticky-pinned row kept out of
// its sorted position — see app/notifications/page.tsx) produces more
// groups of the same label instead of silently re-sorting the feed; the
// caller must key each group by more than just its label (see page.tsx).

export interface NotificationDateGroup<T> {
  label: string;
  items: T[];
}

interface Dated {
  created_at: string;
}

/** "Today" / "Yesterday" / a formatted date ("Oct 12, 2026") for the LOCAL
 * calendar day `iso` falls on, relative to `now` (defaults to the real
 * current time — overridable so this is deterministic in tests). Compares
 * calendar fields (year/month/date) directly rather than subtracting
 * timestamps and dividing by a fixed day length — a local day isn't always
 * exactly 24h across a DST transition, and field comparison sidesteps that
 * instead of relying on rounding to absorb it. A future date (clock skew, a
 * seeded/demo row) falls through to the formatted-date branch rather than a
 * confusing negative day count. `locale` defaults to the viewer's own
 * (undefined = browser default); tests pass "en-US" explicitly so
 * assertions don't depend on the runtime's locale. */
export function dateGroupLabel(iso: string, now: Date = new Date(), locale?: string): string {
  const item = new Date(iso);
  if (Number.isNaN(item.getTime())) return "Earlier";

  const diffDays = localCalendarDayDiff(item, now);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";

  return new Date(item.getFullYear(), item.getMonth(), item.getDate()).toLocaleDateString(locale, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/** How many LOCAL calendar days `to` is after `from` (0 = same day, 1 =
 * `from` was yesterday relative to `to`, negative = `from` is in the
 * future). Built from `Date.UTC` on each side's own Y/M/D fields rather than
 * subtracting the two Dates' real timestamps — that would divide by a fixed
 * 86,400,000ms and misfire across a DST transition, where a local day isn't
 * exactly 24h. Normalizing both sides to a UTC midnight first makes the
 * subtraction an exact day count regardless of either side's real-world
 * length. Exported so any other "age of this timestamp" display (e.g. the
 * per-row relative-time text in app/notifications/page.tsx) can match this
 * module's own definition of a day instead of computing its own, which
 * risks disagreeing with the date-group header a row sits under. */
export function localCalendarDayDiff(from: Date, to: Date): number {
  const fromUTC = Date.UTC(from.getFullYear(), from.getMonth(), from.getDate());
  const toUTC = Date.UTC(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.round((toUTC - fromUTC) / 86_400_000);
}

/** "just now" / "5m ago" / "1d ago" / a formatted date for a single
 * notification row, relative to `now`. Exported (moved from
 * app/notifications/page.tsx) for unit coverage with a fixed `now`.
 *
 * Requires BOTH localCalendarDayDiff >= 1 AND >= 24 real elapsed hours
 * before switching to day-count text — a 2-minute-old notification just
 * after local midnight has a day diff of 1 but should still read "2m ago". */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const created = new Date(iso);
  if (Number.isNaN(created.getTime())) return "Earlier";

  const diffMs = now.getTime() - created.getTime();
  const diffSec = Math.max(0, Math.floor(diffMs / 1000));
  const diffDays = localCalendarDayDiff(created, now);
  const stillSubDay = diffDays <= 0 || (diffDays === 1 && diffSec < 86_400);

  if (stillSubDay) {
    if (diffSec < 60) return "just now";
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    return `${Math.floor(diffMin / 60)}h ago`;
  }

  if (diffDays < 7) return `${diffDays}d ago`;
  return created.toLocaleDateString();
}

/** Buckets `items` (already newest-first) into consecutive same-label runs,
 * in the order they first appear — one group per calendar day, never a
 * day's items split across two groups as long as the input stays sorted. */
export function groupNotificationsByDate<T extends Dated>(
  items: readonly T[],
  now: Date = new Date(),
  locale?: string
): NotificationDateGroup<T>[] {
  const groups: NotificationDateGroup<T>[] = [];
  for (const item of items) {
    const label = dateGroupLabel(item.created_at, now, locale);
    const current = groups[groups.length - 1];
    if (current && current.label === label) {
      current.items.push(item);
    } else {
      groups.push({ label, items: [item] });
    }
  }
  return groups;
}
