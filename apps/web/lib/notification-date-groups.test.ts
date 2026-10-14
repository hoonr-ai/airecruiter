import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { dateGroupLabel, groupNotificationsByDate, localCalendarDayDiff } from "./notification-date-groups.ts";

// A fixed "now" so every test is deterministic regardless of when it runs.
// Thursday, 2026-10-15, 14:30 local time.
const NOW = new Date(2026, 9, 15, 14, 30, 0);

function at(year: number, month: number, day: number, hour = 12): string {
  return new Date(year, month, day, hour).toISOString();
}

describe("dateGroupLabel", () => {
  it("labels the same local calendar day as Today, any time of day", () => {
    assert.equal(dateGroupLabel(at(2026, 9, 15, 0), NOW), "Today");
    assert.equal(dateGroupLabel(at(2026, 9, 15, 23), NOW), "Today");
  });

  it("labels the previous local calendar day as Yesterday", () => {
    assert.equal(dateGroupLabel(at(2026, 9, 14, 0), NOW), "Yesterday");
    assert.equal(dateGroupLabel(at(2026, 9, 14, 23), NOW), "Yesterday");
  });

  it("labels anything older with a formatted date", () => {
    // Explicit locale: toLocaleDateString(undefined, ...) — the production
    // default — follows the runtime's own locale, which would make this
    // assertion flaky on a non-en-US CI/dev machine. The page itself still
    // calls this with no locale, so real users still see their own locale.
    assert.equal(dateGroupLabel(at(2026, 9, 12), NOW, "en-US"), "Oct 12, 2026");
    assert.equal(dateGroupLabel(at(2025, 11, 25), NOW, "en-US"), "Dec 25, 2025");
  });

  it("labels a future date (clock skew) with a formatted date, not a negative day count", () => {
    assert.equal(dateGroupLabel(at(2026, 9, 20), NOW, "en-US"), "Oct 20, 2026");
  });

  it("falls back to a non-crashing label for an unparsable date instead of 'Invalid Date'", () => {
    assert.equal(dateGroupLabel("not-a-real-date", NOW), "Earlier");
  });

  it("is unaffected by a local day that isn't exactly 24h (a DST transition)", () => {
    // Spring-forward in the US: 2026-03-08 is 23 wall-clock hours in
    // America-style DST. Comparing calendar fields (not timestamp / 86.4M
    // ms) means this day's shorter length can't throw the day-diff off.
    const dstNow = new Date(2026, 2, 9, 10, 0); // 2026-03-09, the day after
    assert.equal(dateGroupLabel(at(2026, 2, 8, 10), dstNow), "Yesterday");
    assert.equal(dateGroupLabel(at(2026, 2, 7, 10), dstNow, "en-US"), "Mar 7, 2026");
  });

  it("uses the LOCAL calendar day, not a UTC one — 11pm and 1am on either side of local midnight land in different groups", () => {
    const justBeforeMidnight = new Date(2026, 9, 14, 23, 59).toISOString();
    const justAfterMidnight = new Date(2026, 9, 15, 0, 1).toISOString();
    assert.equal(dateGroupLabel(justBeforeMidnight, NOW), "Yesterday");
    assert.equal(dateGroupLabel(justAfterMidnight, NOW), "Today");
  });
});

describe("localCalendarDayDiff", () => {
  it("is 0 for the same local calendar day, regardless of time of day", () => {
    assert.equal(localCalendarDayDiff(new Date(2026, 9, 15, 0, 1), NOW), 0);
    assert.equal(localCalendarDayDiff(new Date(2026, 9, 15, 23, 59), NOW), 0);
  });

  it("is 1 for yesterday, 2+ for further back", () => {
    assert.equal(localCalendarDayDiff(new Date(2026, 9, 14, 10), NOW), 1);
    assert.equal(localCalendarDayDiff(new Date(2026, 9, 12, 10), NOW), 3);
  });

  it("is negative for a future date (clock skew)", () => {
    assert.equal(localCalendarDayDiff(new Date(2026, 9, 16, 10), NOW), -1);
  });

  it("is unaffected by a local day that isn't exactly 24h (a DST transition)", () => {
    const dstNow = new Date(2026, 2, 9, 10, 0); // 2026-03-09
    assert.equal(localCalendarDayDiff(new Date(2026, 2, 8, 10), dstNow), 1);
    assert.equal(localCalendarDayDiff(new Date(2026, 2, 7, 10), dstNow), 2);
  });
});

describe("groupNotificationsByDate", () => {
  it("returns one group per calendar day, in first-seen order", () => {
    const items = [
      { id: 1, created_at: at(2026, 9, 15, 10) },
      { id: 2, created_at: at(2026, 9, 15, 9) },
      { id: 3, created_at: at(2026, 9, 14, 20) },
      { id: 4, created_at: at(2026, 9, 12, 8) },
    ];
    const groups = groupNotificationsByDate(items, NOW, "en-US");
    assert.deepEqual(
      groups.map((g) => g.label),
      ["Today", "Yesterday", "Oct 12, 2026"]
    );
    assert.deepEqual(groups[0].items.map((i) => i.id), [1, 2]);
    assert.deepEqual(groups[1].items.map((i) => i.id), [3]);
    assert.deepEqual(groups[2].items.map((i) => i.id), [4]);
  });

  it("returns an empty array for an empty list", () => {
    assert.deepEqual(groupNotificationsByDate([], NOW), []);
  });

  it("returns a single group when everything is the same day", () => {
    const items = [
      { id: 1, created_at: at(2026, 9, 15, 10) },
      { id: 2, created_at: at(2026, 9, 15, 8) },
    ];
    const groups = groupNotificationsByDate(items, NOW);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].label, "Today");
    assert.equal(groups[0].items.length, 2);
  });

  it("does not merge non-consecutive same-day runs (a mis-sorted input produces two groups, not a silent re-sort)", () => {
    const items = [
      { id: 1, created_at: at(2026, 9, 15, 10) }, // Today
      { id: 2, created_at: at(2026, 9, 14, 10) }, // Yesterday
      { id: 3, created_at: at(2026, 9, 15, 9) }, // Today again, out of order
    ];
    const groups = groupNotificationsByDate(items, NOW);
    assert.deepEqual(
      groups.map((g) => g.label),
      ["Today", "Yesterday", "Today"]
    );
  });
});
