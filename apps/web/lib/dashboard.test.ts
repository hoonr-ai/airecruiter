import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  changeDirection,
  conversion,
  countChange,
  customRangeError,
  formatAverage,
  formatChange,
  formatCount,
  formatHours,
  formatRangeLabel,
  formatRate,
  formatShortDay,
  formatWeekLabel,
  hoursAsDays,
  presetRange,
  rangeDays,
  rateChange,
  shareOfTop,
} from "./dashboard.ts";

describe("dashboard presets", () => {
  const today = "2026-09-24";

  it("counts today as the last day of every trailing preset", () => {
    assert.deepEqual(presetRange("7d", today), { start: "2026-09-18", end: today });
    assert.deepEqual(presetRange("30d", today), { start: "2026-08-26", end: today });
    assert.equal(rangeDays(presetRange("7d", today)!), 7);
    assert.equal(rangeDays(presetRange("30d", today)!), 30);
  });

  it("treats Last Quarter as the trailing 90 days", () => {
    const quarter = presetRange("quarter", today)!;
    assert.equal(rangeDays(quarter), 90);
    assert.equal(quarter.end, today);
  });

  it("starts YTD on January 1st and returns null for all time", () => {
    assert.deepEqual(presetRange("ytd", today), { start: "2026-01-01", end: today });
    assert.deepEqual(presetRange("ytd", "2026-01-01"), { start: "2026-01-01", end: "2026-01-01" });
    assert.equal(presetRange("all", today), null);
  });

  it("crosses month and leap-year boundaries at UTC midnight", () => {
    assert.deepEqual(presetRange("7d", "2028-03-02"), { start: "2028-02-25", end: "2028-03-02" });
  });

  it("validates custom ranges", () => {
    assert.equal(customRangeError("2026-09-01", "2026-09-30"), null);
    assert.match(customRangeError("", "2026-09-30")!, /both/);
    assert.match(customRangeError("2026-09-30", "2026-09-01")!, /on or before/);
    assert.match(customRangeError("2025-01-01", "2026-09-30")!, /366/);
    assert.equal(customRangeError("2025-09-24", "2026-09-24"), null); // 366 days inclusive
  });
});

describe("dashboard vs-prev badges", () => {
  it("reports relative change for counts", () => {
    assert.deepEqual(countChange(4, 3), { kind: "pct", value: 33 });
    assert.deepEqual(countChange(7, 8), { kind: "pct", value: -13 });
    assert.deepEqual(countChange(0, 0), { kind: "pct", value: 0 });
  });

  it("says 'new' instead of dividing by an empty previous period", () => {
    assert.deepEqual(countChange(5, 0), { kind: "new" });
    assert.equal(formatChange(countChange(5, 0)), "new");
    assert.equal(changeDirection(countChange(5, 0)), "up");
  });

  it("has nothing to compare for all time or missing data", () => {
    assert.equal(countChange(5, null), null);
    assert.equal(countChange(null, 5), null);
    assert.equal(rateChange(0.3, null), null);
    assert.equal(formatChange(null), "");
    assert.equal(changeDirection(null), "none");
  });

  it("reports rates in percentage points", () => {
    assert.deepEqual(rateChange(0.34, 0.26), { kind: "pts", value: 8 });
    assert.equal(formatChange(rateChange(0.34, 0.26)), "+8 pts");
    assert.equal(formatChange(rateChange(0.2, 0.25)), "-5 pts");
    assert.equal(changeDirection(rateChange(0.2, 0.2)), "flat");
  });

  it("formats signs", () => {
    assert.equal(formatChange({ kind: "pct", value: 200 }), "+200%");
    assert.equal(formatChange({ kind: "pct", value: -13 }), "-13%");
    assert.equal(formatChange({ kind: "pct", value: 0 }), "0%");
  });
});

describe("dashboard formats", () => {
  it("formats counts, averages and rates, with a dash for missing values", () => {
    assert.equal(formatCount(1840), "1,840");
    assert.equal(formatCount(null), "—");
    assert.equal(formatAverage(2.54), "2.5");
    assert.equal(formatAverage(2), "2");
    assert.equal(formatAverage(undefined), "—");
    assert.equal(formatRate(0.345), "35%");
    assert.equal(formatRate(0), "0%");
    assert.equal(formatRate(null), "—");
  });

  it("formats hours with a days hint", () => {
    assert.equal(formatHours(864), "864 hrs");
    assert.equal(formatHours(1240.4), "1,240 hrs");
    assert.equal(formatHours(0.46), "0.5 hrs");
    assert.equal(formatHours(null), "—");
    assert.equal(hoursAsDays(864), "36 days");
    assert.equal(hoursAsDays(30), "1.3 days");
    assert.equal(hoursAsDays(5), "under a day");
    assert.equal(hoursAsDays(null), "");
  });

  it("labels weeks and ranges", () => {
    assert.equal(formatWeekLabel("2026-09-21"), "Sep 21");
    assert.equal(formatShortDay("2026-08-21"), "Aug 21, 2026");
    assert.equal(formatShortDay("2026-08-21T10:10:45"), "Aug 21, 2026");
    assert.equal(formatRangeLabel({ start: "2026-06-10", end: "2026-06-17" }), "Jun 10 – Jun 17, 2026");
    assert.equal(formatRangeLabel({ start: "2025-12-20", end: "2026-01-02" }), "Dec 20, 2025 – Jan 2, 2026");
    assert.equal(formatRangeLabel({ start: "2026-09-24", end: "2026-09-24" }), "Sep 24, 2026");
    assert.equal(formatRangeLabel(null), "All time");
  });

  it("computes funnel conversion and bar shares", () => {
    assert.equal(conversion(8, 5), 0.625);
    assert.equal(conversion(0, 5), null);
    assert.equal(shareOfTop(188, 94), 0.5);
    assert.equal(shareOfTop(0, 3), 0);
    assert.equal(shareOfTop(10, 12), 1);
  });
});
