// Match-score N/A display policy.
//
// A displayed 0% must mean "assessed and failed a hard filter": either the
// backend's hard-veto fired (exclusion rule / out-of-radius), or exclusion
// penalties zeroed the score (still filter signal). Any other zero/absent
// score means we lacked the data for a fair assessment — those rows display
// "N/A" instead of a misleading red 0%, and are exempt from score-based
// filtering. Mirrors the finalize_candidate N/A policy in
// apps/api/services/unified_candidate_search.py.

export function hasHardFilterFail(matchScoreDetails: any): boolean {
  if (!matchScoreDetails || typeof matchScoreDetails !== "object") return false;
  if (matchScoreDetails.hard_veto?.triggered === true) return true;
  return Object.values(matchScoreDetails).some(
    (d: any) => d && typeof d === "object" && Number(d.exclusion_penalty) > 0
  );
}

export function isScoreNA(score: unknown, matchScoreDetails: any): boolean {
  if (typeof score === "number" && Number.isFinite(score) && score > 0) {
    return false;
  }
  if (score === 0 && hasHardFilterFail(matchScoreDetails)) return false;
  return true;
}

export function isCandidateScoreNA(c: any): boolean {
  return isScoreNA(c?.match_score, c?.match_score_details);
}
