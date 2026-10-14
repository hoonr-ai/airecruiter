import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { dateGroupLabel, groupNotificationsByDate } from "./notification-date-groups.ts";

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
    assert.equal(dateGroupLabel(at(2026, 9, 12), NOW), "Oct 12, 2026");
    assert.equal(dateGroupLabel(at(2025, 11, 25), NOW), "Dec 25, 2025");
  });

  it("labels a future date (clock skew) with a formatted date, not a negative day count", () => {
    assert.equal(dateGroupLabel(at(2026, 9, 20), NOW), "Oct 20, 2026");
  });

  it("uses the LOCAL calendar day, not a UTC one — 11pm and 1am on either side of local midnight land in different groups", () => {
    const justBeforeMidnight = new Date(2026, 9, 14, 23, 59).toISOString();
    const justAfterMidnight = new Date(2026, 9, 15, 0, 1).toISOString();
    assert.equal(dateGroupLabel(justBeforeMidnight, NOW), "Yesterday");
    assert.equal(dateGroupLabel(justAfterMidnight, NOW), "Today");
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
    const groups = groupNotificationsByDate(items, NOW);
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
