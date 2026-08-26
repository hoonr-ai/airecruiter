export const sourcePriority = (c: any): number => {
  const source = String(c?.source || "").toLowerCase();
  if (source.includes("applicant")) return 1;
  if (source.includes("linkedin")) return 2;
  if (source.includes("talentsearch")) return 3;
  return 4;
};

export const getCandidateMatchScore = (c: any): number => {
  const score = Number(c?.match_score);
  return Number.isFinite(score) ? score : 0;
};

export const compareCandidatesByMatch = (a: any, b: any, sortDir: "asc" | "desc"): number => {
  const dirMul = sortDir === "asc" ? 1 : -1;
  const scoreA = getCandidateMatchScore(a);
  const scoreB = getCandidateMatchScore(b);
  const hasScoreA = scoreA > 0;
  const hasScoreB = scoreB > 0;

  // 1. Primary Rule: Scored candidates always float to the top
  if (hasScoreA !== hasScoreB) {
    return hasScoreA ? -1 : 1;
  }

  // 2. Secondary Rule: If both have scores, sort by score based on direction multiplier
  if (hasScoreA && hasScoreB && scoreA !== scoreB) {
    return scoreA > scoreB ? dirMul : -dirMul;
  }

  // 3. Fallback Rules (Applies to ties in scored group OR the entire unscored group)
  
  // Tie-breaker A: api_rank (stable, lower rank always comes first).
  // JobDiva rows carry api_rank (recency for Applicants, JobAgent rank for Talent Search).
  // Match score is a lenient/rough signal for those rows, so api_rank wins on ties.
  const rankA = typeof a?.api_rank === "number" ? a.api_rank : null;
  const rankB = typeof b?.api_rank === "number" ? b.api_rank : null;
  if (rankA !== null && rankB !== null && rankA !== rankB) {
    return rankA - rankB;
  }

  // Tie-breaker B: JobDiva Preference (Only matters for unscored candidates based on original logic).
  // JobDiva-JobAgent rows carry no % (unscored by design, ranked by JobDiva) — treat
  // them as the top band of the unscored group so they float above other unscored candidates.
  if (!hasScoreA) {
    const agentA = a?.source === "JobDiva-JobAgent";
    const agentB = b?.source === "JobDiva-JobAgent";
    if (agentA !== agentB) {
      return agentA ? -1 : 1;
    }
  }

  // Tie-breaker C: Source Priority (stable, higher priority source always comes first)
  const prioA = sourcePriority(a);
  const prioB = sourcePriority(b);
  if (prioA !== prioB) {
    return prioA - prioB;
  }

  return 0;
};
