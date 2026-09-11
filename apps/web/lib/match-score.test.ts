import { test } from "node:test";
import assert from "node:assert/strict";

import {
  getScoreBand,
  getScoreTone,
  isOutreachEligibleScore,
  OUTREACH_MIN_SCORE,
  SCORE_BAND_EXCELLENT,
  SCORE_BAND_GOOD,
  SCORE_BAND_STRONG,
} from "./match-score.ts";

test("band thresholds follow the recruiter matrix (85 / 75 / 60)", () => {
  assert.equal(SCORE_BAND_EXCELLENT, 85);
  assert.equal(SCORE_BAND_STRONG, 75);
  assert.equal(SCORE_BAND_GOOD, 60);
  assert.equal(OUTREACH_MIN_SCORE, 60);
});

test("getScoreBand maps scores onto the four bands, inclusive at the edges", () => {
  assert.equal(getScoreBand(100).tier, "excellent");
  assert.equal(getScoreBand(85).tier, "excellent");
  assert.equal(getScoreBand(84).tier, "strong");
  assert.equal(getScoreBand(75).tier, "strong");
  assert.equal(getScoreBand(74).tier, "good");
  assert.equal(getScoreBand(60).tier, "good");
  assert.equal(getScoreBand(59).tier, "low");
  assert.equal(getScoreBand(0).tier, "low");
});

test("getScoreBand treats null / undefined / NaN as unscored", () => {
  assert.equal(getScoreBand(null).tier, "unscored");
  assert.equal(getScoreBand(undefined).tier, "unscored");
  assert.equal(getScoreBand(Number.NaN).tier, "unscored");
  assert.equal(getScoreTone(null), null);
});

test("outreach eligibility needs a numeric score at or above the floor", () => {
  assert.equal(isOutreachEligibleScore(60), true);
  assert.equal(isOutreachEligibleScore(59), false);
  assert.equal(isOutreachEligibleScore(null), false);
  assert.equal(isOutreachEligibleScore(undefined), false);
  assert.equal(isOutreachEligibleScore("90"), false);
});

test("each band has a distinct tone", () => {
  const tones = [100, 80, 65, 10].map((s) => getScoreTone(s)?.ring);
  assert.equal(new Set(tones).size, 4);
});
