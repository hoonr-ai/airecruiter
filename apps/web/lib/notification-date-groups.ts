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
// re-sorting, so an out-of-order input would silently produce more groups
// than expected instead of one each.

export interface NotificationDateGroup<T> {
  label: string;
  items: T[];
}

interface Dated {
  created_at: string;
}

function startOfLocalDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** "Today" / "Yesterday" / a formatted date ("Oct 12, 2026") for the LOCAL
 * calendar day `iso` falls on, relative to `now` (defaults to the real
 * current time — overridable so this is deterministic in tests). A future
 * date (clock skew, a seeded/demo row) falls through to the same formatted-
 * date branch as anything else more than a day away, rather than a
 * confusing negative day count. */
export function dateGroupLabel(iso: string, now: Date = new Date()): string {
  const itemDay = startOfLocalDay(new Date(iso));
  const today = startOfLocalDay(now);
  const diffDays = Math.round((today.getTime() - itemDay.getTime()) / 86_400_000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return itemDay.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** Buckets `items` (already newest-first) into consecutive same-label runs,
 * in the order they first appear — one group per calendar day, never a
 * day's items split across two groups as long as the input stays sorted. */
export function groupNotificationsByDate<T extends Dated>(
  items: readonly T[],
  now: Date = new Date()
): NotificationDateGroup<T>[] {
  const groups: NotificationDateGroup<T>[] = [];
  for (const item of items) {
    const label = dateGroupLabel(item.created_at, now);
    const current = groups[groups.length - 1];
    if (current && current.label === label) {
      current.items.push(item);
    } else {
      groups.push({ label, items: [item] });
    }
  }
  return groups;
}
