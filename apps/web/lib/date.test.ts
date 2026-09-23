import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  addIsoDays,
  formatDuration,
  inclusiveDateSpanDays,
  formatEasternDate,
  formatEasternDateTime,
  formatEasternTime,
  normalizeToUtcDate,
  todayEastern,
  withEasternLabel,
} from "./date.ts";

describe("date utils", () => {
  describe("normalizeToUtcDate", () => {
    it("handles null or undefined", () => {
      assert.strictEqual(normalizeToUtcDate(null), null);
      assert.strictEqual(normalizeToUtcDate(undefined), null);
      assert.strictEqual(normalizeToUtcDate(""), null);
    });

    it("handles already UTC string with Z", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00Z");
      assert.notStrictEqual(date, null);
      assert.strictEqual(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("handles already UTC string with offset", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00+00:00");
      assert.notStrictEqual(date, null);
      assert.strictEqual(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("appends Z to naive datetime with T", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00");
      assert.notStrictEqual(date, null);
      assert.strictEqual(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("replaces space and appends Z to naive datetime with space", () => {
      const date = normalizeToUtcDate("2026-08-26 20:10:00");
      assert.notStrictEqual(date, null);
      assert.strictEqual(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("handles bare date gracefully", () => {
      const date = normalizeToUtcDate("2026-08-27");
      assert.notStrictEqual(date, null);
      assert.strictEqual(date!.toISOString(), "2026-08-27T00:00:00.000Z");
    });

    it("returns null for invalid dates", () => {
      assert.strictEqual(normalizeToUtcDate("invalid-date"), null);
    });
  });

  describe("formatEasternDateTime", () => {
    it("renders Eastern wall-clock time with no zone suffix", () => {
      // 13:46:55 UTC = 09:46:55 EDT (UTC-4 in September).
      assert.strictEqual(formatEasternDateTime("2026-09-21T13:46:55Z"), "09/21/2026 09:46:55");
    });

    it("honours an explicit offset", () => {
      assert.strictEqual(formatEasternDateTime("2026-09-21T09:46:55-04:00"), "09/21/2026 09:46:55");
      assert.strictEqual(formatEasternDateTime("2026-09-21T19:16:55+05:30"), "09/21/2026 09:46:55");
    });

    it("treats naive strings as UTC", () => {
      assert.strictEqual(formatEasternDateTime("2026-09-21 13:46:55"), "09/21/2026 09:46:55");
    });

    it("uses EST in winter", () => {
      // 15:00 UTC = 10:00 EST (UTC-5 in January).
      assert.strictEqual(formatEasternDateTime("2026-01-15T15:00:00Z"), "01/15/2026 10:00:00");
    });

    it("renders midnight as 00, never 24", () => {
      assert.strictEqual(formatEasternDateTime("2026-09-21T04:00:00Z"), "09/21/2026 00:00:00");
    });

    it("never carries a zone name", () => {
      assert.doesNotMatch(formatEasternDateTime("2026-09-21T13:46:55Z"), /E[DS]T|GMT|UTC/);
    });

    it("returns a dash for empty or invalid input", () => {
      assert.strictEqual(formatEasternDateTime(null), "—");
      assert.strictEqual(formatEasternDateTime(""), "—");
      assert.strictEqual(formatEasternDateTime("not a date"), "—");
    });
  });

  describe("formatEasternTime", () => {
    it("returns only the time of day", () => {
      assert.strictEqual(formatEasternTime("2026-09-21T13:46:55Z"), "09:46:55");
      assert.strictEqual(formatEasternTime(null), "—");
    });
  });

  describe("formatEasternDate", () => {
    it("renders a bare calendar date as written", () => {
      assert.strictEqual(formatEasternDate("2026-09-21"), "09/21/2026");
    });

    it("converts an instant to its Eastern calendar date", () => {
      // 02:00 UTC on the 22nd is still the evening of the 21st in New York.
      assert.strictEqual(formatEasternDate("2026-09-22T02:00:00Z"), "09/21/2026");
    });

    it("returns a dash for empty input", () => {
      assert.strictEqual(formatEasternDate(undefined), "—");
    });
  });

  describe("withEasternLabel", () => {
    it("appends the zone once, for column headers", () => {
      assert.strictEqual(withEasternLabel("PAIR Launch"), "PAIR Launch (ET)");
    });
  });

  describe("todayEastern", () => {
    it("is the New York calendar date", () => {
      assert.strictEqual(todayEastern(new Date("2026-09-22T02:00:00Z")), "2026-09-21");
      assert.strictEqual(todayEastern(new Date("2026-09-22T12:00:00Z")), "2026-09-22");
    });
  });

  describe("calendar-date arithmetic", () => {
    it("adds days across month and DST boundaries", () => {
      assert.strictEqual(addIsoDays("2026-09-30", 1), "2026-10-01");
      assert.strictEqual(addIsoDays("2026-11-01", 1), "2026-11-02");
      assert.strictEqual(addIsoDays("2026-03-01", -1), "2026-02-28");
    });

    it("counts both ends of a range", () => {
      assert.strictEqual(inclusiveDateSpanDays("2026-09-21", "2026-09-21"), 1);
      assert.strictEqual(inclusiveDateSpanDays("2026-08-25", "2026-09-23"), 30);
    });
  });

  describe("formatDuration", () => {
    it("scales minutes to hours and days", () => {
      assert.strictEqual(formatDuration(45), "45m");
      assert.strictEqual(formatDuration(82), "1h 22m");
      assert.strictEqual(formatDuration(120), "2h");
      assert.strictEqual(formatDuration(3060), "2d 3h");
      assert.strictEqual(formatDuration(null), "—");
    });
  });
});
