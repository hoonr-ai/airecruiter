import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { getCandidateCompletedAt } from "./candidate-completed-at.ts";

describe("getCandidateCompletedAt", () => {
  it("prefers the pre-merged top-level field when present", () => {
    assert.equal(
      getCandidateCompletedAt({
        engage_completed_at: "2026-09-22T14:00:00Z",
        data: { first_completed_at: "2026-09-01T00:00:00Z", engage_completed_at: "2026-09-10T00:00:00Z" },
      }),
      "2026-09-22T14:00:00Z"
    );
  });

  it("falls back to data.first_completed_at before data.engage_completed_at, matching the SQL COALESCE order", () => {
    assert.equal(
      getCandidateCompletedAt({
        data: { first_completed_at: "2026-09-01T00:00:00Z", engage_completed_at: "2026-09-10T00:00:00Z" },
      }),
      "2026-09-01T00:00:00Z"
    );
  });

  it("falls back to data.engage_completed_at when first_completed_at is absent", () => {
    assert.equal(
      getCandidateCompletedAt({ data: { engage_completed_at: "2026-09-10T00:00:00Z" } }),
      "2026-09-10T00:00:00Z"
    );
  });

  it("returns undefined when nothing is set", () => {
    assert.equal(getCandidateCompletedAt({}), undefined);
    assert.equal(getCandidateCompletedAt({ data: {} }), undefined);
    assert.equal(getCandidateCompletedAt({ data: null }), undefined);
  });

  it("ignores empty strings the same as absent values", () => {
    assert.equal(
      getCandidateCompletedAt({ engage_completed_at: "", data: { first_completed_at: "2026-09-01T00:00:00Z" } }),
      "2026-09-01T00:00:00Z"
    );
  });
});
