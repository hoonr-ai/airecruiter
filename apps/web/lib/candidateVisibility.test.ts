import assert from "node:assert/strict";
import { test } from "node:test";

import {
  candidateHiddenReason,
  hiddenBreakdown,
  type VisibilityContext,
} from "./candidateVisibility.ts";

const baseCtx = (over: Partial<VisibilityContext> = {}): VisibilityContext => ({
  launchedKeys: new Set<string>(),
  launchedIds: new Set<string>(),
  isExcluded: () => false,
  minScore: 0,
  getScore: (c: any) => Number(c.match_score ?? 0),
  locationFilter: new Set<string>(),
  getLocation: (c: any) => String(c.location || ""),
  searchQuery: "",
  ...over,
});

test("visible row returns null", () => {
  assert.equal(candidateHiddenReason({ candidate_id: "1" }, baseCtx()), null);
});

test("launched matches the composite source:id key", () => {
  const ctx = baseCtx({ launchedKeys: new Set(["JobDiva-JobAgent:1"]) });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", source: "JobDiva-JobAgent" }, ctx),
    "launched"
  );
});

test("launched falls back to the bare candidate_id when the source string drifted", () => {
  const ctx = baseCtx({ launchedIds: new Set(["1"]) });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", source: "JobDiva" }, ctx),
    "launched"
  );
});

test("launched takes precedence over excluded", () => {
  const ctx = baseCtx({
    launchedIds: new Set(["1"]),
    isExcluded: () => true,
  });
  assert.equal(candidateHiddenReason({ candidate_id: "1" }, ctx), "launched");
});

test("excluded takes precedence over the score filter", () => {
  const ctx = baseCtx({ isExcluded: () => true, minScore: 60 });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", match_score: 10 }, ctx),
    "excluded"
  );
});

test("min-score filter hides sub-threshold rows", () => {
  const ctx = baseCtx({ minScore: 60 });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", match_score: 59 }, ctx),
    "filtered"
  );
  assert.equal(
    candidateHiddenReason({ candidate_id: "2", match_score: 60 }, ctx),
    null
  );
});

test("unscored JobDiva-JobAgent rows are exempt from the min-score filter", () => {
  const ctx = baseCtx({ minScore: 60 });
  // No numeric score (the default agent-row policy: no % shown) → the %
  // filter can never hide the row.
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "1", source: "JobDiva-JobAgent", match_score: null },
      ctx
    ),
    null
  );
  assert.equal(
    candidateHiddenReason({ candidate_id: "2", source: "JobDiva-JobAgent" }, ctx),
    null
  );
});

test("scored JobDiva-JobAgent rows (assess_all_sources) filter like everyone else", () => {
  const ctx = baseCtx({ minScore: 60 });
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "1", source: "JobDiva-JobAgent", match_score: 30 },
      ctx
    ),
    "filtered"
  );
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "2", source: "JobDiva-JobAgent", match_score: 75 },
      ctx
    ),
    null
  );
});

test("detail_failed rows are exempt from the min-score filter", () => {
  const ctx = baseCtx({ minScore: 60 });
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "1", match_score: 0, detail_failed: true },
      ctx
    ),
    null
  );
});

test("progressive rows bypass score and location while shimmering", () => {
  const ctx = baseCtx({ minScore: 60, locationFilter: new Set(["Austin, TX"]) });
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "1", _stage: "agent_result", match_score: 0, location: "" },
      ctx
    ),
    null
  );
  // details_loaded bypasses the score filter but NOT the location filter.
  assert.equal(
    candidateHiddenReason(
      { candidate_id: "2", _stage: "details_loaded", match_score: 0, location: "Plano, TX" },
      ctx
    ),
    "filtered"
  );
});

test("location filter hides rows outside the selected set", () => {
  const ctx = baseCtx({ locationFilter: new Set(["Austin, TX"]) });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", location: "Plano, TX" }, ctx),
    "filtered"
  );
  assert.equal(
    candidateHiddenReason({ candidate_id: "2", location: "Austin, TX" }, ctx),
    null
  );
});

test("search query matches across the identity/contact haystack", () => {
  const ctx = baseCtx({ searchQuery: "  RiVeRa " });
  assert.equal(
    candidateHiddenReason({ candidate_id: "1", name: "Ana Rivera" }, ctx),
    null
  );
  assert.equal(
    candidateHiddenReason({ candidate_id: "2", name: "Sam Cole" }, ctx),
    "filtered"
  );
});

test("breakdown categories are disjoint and sum with visible rows to the bucket", () => {
  const rows = [
    { candidate_id: "L1" }, // launched
    { candidate_id: "E1" }, // excluded
    { candidate_id: "F1", match_score: 10 }, // filtered by score
    { candidate_id: "V1", match_score: 90 }, // visible
  ];
  const ctx = baseCtx({
    launchedIds: new Set(["L1"]),
    isExcluded: (c: any) => c.candidate_id === "E1",
    minScore: 60,
  });
  const b = hiddenBreakdown(rows, ctx);
  assert.deepEqual(b, { bucket: 4, launched: 1, excluded: 1, filtered: 1 });
  const visible = rows.filter((c) => candidateHiddenReason(c, ctx) === null);
  assert.equal(b.launched + b.excluded + b.filtered + visible.length, b.bucket);
});

test("all-hidden bucket: breakdown accounts for every row (the empty-state contract)", () => {
  const rows = [
    { candidate_id: "1" },
    { candidate_id: "2" },
    { candidate_id: "3", match_score: 5 },
  ];
  const ctx = baseCtx({
    launchedIds: new Set(["1", "2"]),
    minScore: 50,
  });
  const b = hiddenBreakdown(rows, ctx);
  assert.equal(b.bucket, 3);
  assert.equal(b.launched + b.excluded + b.filtered, 3);
});
