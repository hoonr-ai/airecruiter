// Match-score ranking bands (recruiter scoring matrix, 2026-09-11).
//
// Mirrors core/config.py `score_band` on the backend:
//   85–100  Excellent — Priority
//   75–84   Strong — Recommended
//   60–74   Good — Recruiter Review
//   <60     Low Priority — no outreach
//
// Shared by the Step-5 table pill, the candidate details modal and the
// sourcing quality scorecard so the colours, labels and the auto-launch
// floor can never drift apart.

export const SCORE_BAND_EXCELLENT = 85;
export const SCORE_BAND_STRONG = 75;
export const SCORE_BAND_GOOD = 60;
// No PAIR outreach below this score (the Source & Launch floor).
export const OUTREACH_MIN_SCORE = SCORE_BAND_GOOD;

export type ScoreBandTier = "excellent" | "strong" | "good" | "low" | "unscored";

export type ScoreBand = {
  tier: ScoreBandTier;
  label: string;
  action: string;
  // Inclusive lower bound of the band; null for unscored.
  min: number | null;
};

export function getScoreBand(score: number | null | undefined): ScoreBand {
  if (score === null || score === undefined || !Number.isFinite(Number(score))) {
    return { tier: "unscored", label: "Unscored", action: "Limited data", min: null };
  }
  const s = Number(score);
  if (s >= SCORE_BAND_EXCELLENT) {
    return { tier: "excellent", label: "Excellent", action: "Priority", min: SCORE_BAND_EXCELLENT };
  }
  if (s >= SCORE_BAND_STRONG) {
    return { tier: "strong", label: "Strong", action: "Recommended", min: SCORE_BAND_STRONG };
  }
  if (s >= SCORE_BAND_GOOD) {
    return { tier: "good", label: "Good", action: "Recruiter Review", min: SCORE_BAND_GOOD };
  }
  return { tier: "low", label: "Low Priority", action: "No outreach", min: 0 };
}

// Whether a score clears the outreach floor. Unscored (N/A) rows never do —
// "no outreach below 60%" needs a number to compare against.
export function isOutreachEligibleScore(score: unknown): boolean {
  return typeof score === "number" && Number.isFinite(score) && score >= OUTREACH_MIN_SCORE;
}

export type ScoreTone = { ring: string; bg: string; text: string };

// Pill / ring colours per band. Emerald for Excellent, blue for Strong,
// amber for Good, rose for Low. Null for unscored (callers render N/A).
export function getScoreTone(score: number | null | undefined): ScoreTone | null {
  const band = getScoreBand(score);
  switch (band.tier) {
    case "excellent":
      return { ring: "#059669", bg: "#d1fae5", text: "#047857" };
    case "strong":
      return { ring: "#2563eb", bg: "#dbeafe", text: "#1d4ed8" };
    case "good":
      return { ring: "#d97706", bg: "#fef3c7", text: "#b45309" };
    case "low":
      return { ring: "#e11d48", bg: "#ffe4e6", text: "#be123c" };
    default:
      return null;
  }
}
