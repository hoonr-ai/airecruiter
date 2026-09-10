// The recruiter's "within N mi" search radius (Step 5 → Locations).
//
// There is deliberately NO upper bound (2026-08-27, job 26-26189). The old
// hard 100mi cap silently rewrote anything larger back to 100, so a recruiter
// willing to work a wider catchment had no way to say so — and candidates just
// past 100mi were scored 0% by the backend's location gate with no recourse.
// The backend mirrors this in `core.config.clamp_search_radius`; keep the two
// in step.

export const DEFAULT_SEARCH_RADIUS_MILES = 25;

/** Normalize a typed radius to a positive whole number of miles. */
export function clampSearchRadius(
  value: unknown,
  fallback: number = DEFAULT_SEARCH_RADIUS_MILES,
): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  const rounded = Math.round(parsed);
  return rounded > 0 ? rounded : fallback;
}

/** Pull the miles out of a stored radius label such as "within 150 mi". */
export function parseRadiusLabel(
  label: unknown,
  fallback: number = DEFAULT_SEARCH_RADIUS_MILES,
): number {
  if (typeof label !== "string") return fallback;
  const digits = label.match(/(\d+)/)?.[1];
  return digits ? clampSearchRadius(digits, fallback) : fallback;
}
