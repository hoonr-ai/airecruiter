import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { normalizeToUtcDate } from "./date.ts";

describe("date utils", () => {
  describe("normalizeToUtcDate", () => {
    it("handles null or undefined", () => {
      assert.equal(normalizeToUtcDate(null), null);
      assert.equal(normalizeToUtcDate(undefined), null);
      assert.equal(normalizeToUtcDate(""), null);
    });

    it("handles already UTC string with Z", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00Z");
      assert.notEqual(date, null);
      assert.equal(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("handles already UTC string with offset", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00+00:00");
      assert.notEqual(date, null);
      assert.equal(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("appends Z to naive datetime with T", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00");
      assert.notEqual(date, null);
      assert.equal(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("replaces space and appends Z to naive datetime with space", () => {
      const date = normalizeToUtcDate("2026-08-26 20:10:00");
      assert.notEqual(date, null);
      assert.equal(date!.toISOString(), "2026-08-26T20:10:00.000Z");
    });

    it("handles bare date gracefully", () => {
      const date = normalizeToUtcDate("2026-08-27");
      assert.notEqual(date, null);
      // Bare dates parsed by new Date("2026-08-27") default to UTC midnight in JS
      assert.equal(date!.toISOString(), "2026-08-27T00:00:00.000Z");
    });

    it("returns null for invalid dates", () => {
      assert.equal(normalizeToUtcDate("invalid-date"), null);
    });
  });
});
