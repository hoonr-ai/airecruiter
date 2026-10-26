import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { callbackDayLabel, callbackRelativeLabel, isCallbackOverdue, isValidFutureCallbackTime } from "./callback-time.ts";

// Built from LOCAL date/time components (not a UTC "Z" string) so this is
// deterministic regardless of the machine running the suite — a fixed UTC
// instant would land on a different local calendar day depending on the
// runner's own timezone, which is exactly the "Today"/"Tomorrow" distinction
// these functions exist to get right.
const NOW = new Date(2026, 0, 14, 12, 0, 0);
const localIso = (y: number, m: number, d: number, h = 9, min = 0) => new Date(y, m, d, h, min, 0).toISOString();

describe("callbackDayLabel", () => {
  it("labels the same local day as Today", () => {
    assert.equal(callbackDayLabel(localIso(2026, 0, 14, 18, 30), NOW, "en-US"), "Today");
  });

  it("labels the next local day as Tomorrow", () => {
    assert.equal(callbackDayLabel(localIso(2026, 0, 15), NOW, "en-US"), "Tomorrow");
  });

  it("labels an overdue callback from the previous local day as Yesterday", () => {
    assert.equal(callbackDayLabel(localIso(2026, 0, 13), NOW, "en-US"), "Yesterday");
  });

  it("falls back to a formatted date further out in either direction", () => {
    assert.equal(callbackDayLabel(localIso(2026, 0, 20), NOW, "en-US"), "Jan 20, 2026");
    assert.equal(callbackDayLabel(localIso(2026, 0, 10), NOW, "en-US"), "Jan 10, 2026");
  });

  it("returns a safe label for an unparsable timestamp", () => {
    assert.equal(callbackDayLabel("not-a-date", NOW, "en-US"), "Unknown date");
  });
});

describe("isCallbackOverdue", () => {
  it("is false for a future time", () => {
    assert.equal(isCallbackOverdue(new Date(NOW.getTime() + 3_600_000).toISOString(), NOW), false);
  });

  it("is true for a past time", () => {
    assert.equal(isCallbackOverdue(new Date(NOW.getTime() - 3_600_000).toISOString(), NOW), true);
  });

  it("is true at the exact moment (no longer strictly in the future)", () => {
    assert.equal(isCallbackOverdue(NOW.toISOString(), NOW), true);
  });

  it("is false (not true) for an unparsable timestamp, rather than throwing", () => {
    assert.equal(isCallbackOverdue("not-a-date", NOW), false);
  });
});

describe("callbackRelativeLabel", () => {
  it("renders a sub-hour countdown in minutes", () => {
    assert.equal(callbackRelativeLabel(new Date(NOW.getTime() + 45 * 60_000).toISOString(), NOW), "in 45m");
  });

  it("renders an under-a-minute countdown distinctly from 1m", () => {
    assert.equal(callbackRelativeLabel(new Date(NOW.getTime() + 30_000).toISOString(), NOW), "in under a minute");
  });

  it("renders an hour-scale countdown in hours", () => {
    assert.equal(callbackRelativeLabel(new Date(NOW.getTime() + 3 * 3_600_000).toISOString(), NOW), "in 3h");
  });

  it("renders a multi-day countdown in days", () => {
    assert.equal(callbackRelativeLabel(new Date(NOW.getTime() + 3 * 86_400_000).toISOString(), NOW), "in 3d");
  });

  it("renders Overdue once the moment has passed, not a negative duration", () => {
    assert.equal(callbackRelativeLabel(new Date(NOW.getTime() - 3_600_000).toISOString(), NOW), "Overdue");
  });
});

describe("isValidFutureCallbackTime", () => {
  it("is true for a time strictly after now", () => {
    assert.equal(isValidFutureCallbackTime(new Date(NOW.getTime() + 60_000).toISOString(), NOW), true);
  });

  it("is false for the exact current moment (not strictly future)", () => {
    assert.equal(isValidFutureCallbackTime(NOW.toISOString(), NOW), false);
  });

  it("is false for a past time", () => {
    assert.equal(isValidFutureCallbackTime(new Date(NOW.getTime() - 60_000).toISOString(), NOW), false);
  });

  it("is false for an unparsable value, unlike a naive !isCallbackOverdue check", () => {
    assert.equal(isValidFutureCallbackTime("not-a-date", NOW), false);
    // The regression this guards: isCallbackOverdue reads "not-a-date" as
    // NOT overdue, which would wrongly look like a valid future time.
    assert.equal(isCallbackOverdue("not-a-date", NOW), false);
  });
});
