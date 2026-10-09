// Shared score -> tier/color mapping for notification badges. No red: every
// in-app notification only fires on a Pass outcome (see
// apps/api/services/notifications_service.py), so the "this failed" red
// semantic never applies here.
export type ScoreTier = "excellent" | "good" | "fair" | "low";

export function scoreTier(score: number): ScoreTier {
  if (score >= 90) return "excellent";
  if (score >= 80) return "good";
  if (score >= 65) return "fair";
  return "low";
}

const TIER_CLASSES: Record<ScoreTier, string> = {
  excellent: "bg-emerald-700 text-white",
  good: "bg-green-500 text-white",
  fair: "bg-yellow-400 text-yellow-950",
  low: "bg-orange-400 text-orange-950",
};

export function scoreColorClasses(score: number): string {
  return TIER_CLASSES[scoreTier(score)];
}
