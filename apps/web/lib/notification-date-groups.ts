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

  if (sameLocalDay(item, now)) return "Today";

  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameLocalDay(item, yesterday)) return "Yesterday";

  return new Date(item.getFullYear(), item.getMonth(), item.getDate()).toLocaleDateString(locale, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
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
