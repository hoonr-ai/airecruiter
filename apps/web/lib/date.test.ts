import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { normalizeToUtcDate } from "./date.ts";

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
});

