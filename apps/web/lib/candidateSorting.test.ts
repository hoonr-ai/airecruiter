import assert from "node:assert/strict";
import { test } from "node:test";

import { compareCandidatesByMatch } from "./candidateSorting.ts";

test("scored-vs-unscored candidates float to the top in both sort directions", () => {
  const scored = { match_score: 85 };
  const unscored = { match_score: 0 };

  // High -> Low
  assert.ok(compareCandidatesByMatch(scored, unscored, "desc") < 0);
  assert.ok(compareCandidatesByMatch(unscored, scored, "desc") > 0);

  // Low -> High
  assert.ok(compareCandidatesByMatch(scored, unscored, "asc") < 0);
  assert.ok(compareCandidatesByMatch(unscored, scored, "asc") > 0);
});

test("scored ties sorted correctly according to sortDir", () => {
  const high = { match_score: 90 };
  const low = { match_score: 45 };

  // High -> Low (descending: higher score comes first)
  assert.ok(compareCandidatesByMatch(high, low, "desc") < 0);
  assert.ok(compareCandidatesByMatch(low, high, "desc") > 0);

  // Low -> High (ascending: lower score comes first)
  assert.ok(compareCandidatesByMatch(high, low, "asc") > 0);
  assert.ok(compareCandidatesByMatch(low, high, "asc") < 0);
});

test("scored ties broken by api_rank", () => {
  const betterRank = { match_score: 80, api_rank: 1 };
  const worseRank = { match_score: 80, api_rank: 5 };

  // Should always prefer the lower api_rank first, regardless of sortDir
  assert.ok(compareCandidatesByMatch(betterRank, worseRank, "desc") < 0);
  assert.ok(compareCandidatesByMatch(betterRank, worseRank, "asc") < 0);
});

test("unscored JobDiva-JobAgent preference", () => {
  const agent = { match_score: 0, source: "JobDiva-JobAgent" };
  const otherUnscored = { match_score: 0, source: "LinkedIn" };

  // JobAgent should always be preferred above other unscored candidates
  assert.ok(compareCandidatesByMatch(agent, otherUnscored, "desc") < 0);
  assert.ok(compareCandidatesByMatch(agent, otherUnscored, "asc") < 0);
});

test("final fallback to sourcePriority", () => {
  const applicant = { match_score: 0, source: "Applicant" };
  const linkedin = { match_score: 0, source: "LinkedIn" };

  // Applicant priority (1) is better than LinkedIn (2)
  assert.ok(compareCandidatesByMatch(applicant, linkedin, "desc") < 0);
  assert.ok(compareCandidatesByMatch(applicant, linkedin, "asc") < 0);
});
