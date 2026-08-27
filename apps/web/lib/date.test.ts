import { normalizeToUtcDate } from "./date";

describe("date utils", () => {
  describe("normalizeToUtcDate", () => {
    it("handles null or undefined", () => {
      expect(normalizeToUtcDate(null)).toBeNull();
      expect(normalizeToUtcDate(undefined)).toBeNull();
      expect(normalizeToUtcDate("")).toBeNull();
    });

    it("handles already UTC string with Z", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00Z");
      expect(date).not.toBeNull();
      expect(date!.toISOString()).toBe("2026-08-26T20:10:00.000Z");
    });

    it("handles already UTC string with offset", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00+00:00");
      expect(date).not.toBeNull();
      expect(date!.toISOString()).toBe("2026-08-26T20:10:00.000Z");
    });

    it("appends Z to naive datetime with T", () => {
      const date = normalizeToUtcDate("2026-08-26T20:10:00");
      expect(date).not.toBeNull();
      expect(date!.toISOString()).toBe("2026-08-26T20:10:00.000Z");
    });

    it("replaces space and appends Z to naive datetime with space", () => {
      const date = normalizeToUtcDate("2026-08-26 20:10:00");
      expect(date).not.toBeNull();
      expect(date!.toISOString()).toBe("2026-08-26T20:10:00.000Z");
    });

    it("handles bare date gracefully", () => {
      const date = normalizeToUtcDate("2026-08-27");
      expect(date).not.toBeNull();
      // Bare dates parsed by new Date("2026-08-27") default to UTC midnight in JS
      expect(date!.toISOString()).toBe("2026-08-27T00:00:00.000Z");
    });

    it("returns null for invalid dates", () => {
      expect(normalizeToUtcDate("invalid-date")).toBeNull();
    });
  });
});
