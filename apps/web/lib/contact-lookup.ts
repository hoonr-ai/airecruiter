// Launch PAIR's contact lookup on Step 5: how candidates are grouped per call
// and how the answers are summed up for the recruiter. The lookups run
// server-side (/candidates/enrich-contacts: Apollo → Exa → Exa deep search,
// cheapest first); each answer names who found the phone / the email.

const PROVIDER_LABELS: Record<string, string> = {
  kipplo: "Kipplo",
  zoominfo: "ZoomInfo",
  apollo: "Apollo",
  exa: "Exa",
  exa_deep: "Exa deep search",
  cache: "saved",
};

export function contactProviderLabel(provider: string | null | undefined): string {
  const key = String(provider || "").trim();
  return PROVIDER_LABELS[key] || key;
}

/** `items` in groups of `size`; the last group may be shorter. */
export function inGroups<T>(items: T[], size: number): T[][] {
  const step = Math.max(1, Math.floor(size));
  const groups: T[][] = [];
  for (let i = 0; i < items.length; i += step) groups.push(items.slice(i, i + step));
  return groups;
}

/**
 * Whether an answer's `lookup.kipplo` is a problem (no key on the server, out
 * of credits, rate limited...) rather than a result: "-" = not asked, "miss" =
 * Kipplo has no record, "phone" / "email" / "email,phone" = what it found.
 */
export function isKipploIssue(outcome: string | null | undefined): boolean {
  const value = String(outcome || "").trim();
  if (!value || value === "-" || value === "miss") return false;
  return !value.split(",").every((f) => f === "email" || f === "phone");
}

function describeCounts(counts: Record<string, number>, noun: string): string {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (total === 0) return "";
  const by = entries
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([provider, n]) => `${contactProviderLabel(provider)} ${n}`)
    .join(", ");
  return `${total} ${noun}${total === 1 ? "" : "s"} (${by})`;
}

/** "3 phones (Kipplo 2, Exa 1) and 1 email (Exa 1)"; "" when nothing was found. */
export function describeContactsFound(
  phoneFoundBy: Record<string, number>,
  emailFoundBy: Record<string, number>,
): string {
  return [describeCounts(phoneFoundBy, "phone"), describeCounts(emailFoundBy, "email")]
    .filter(Boolean)
    .join(" and ");
}

/** The most frequent entry of `counts` (ties: alphabetical), or null. */
export function mostCommon(counts: Record<string, number>): { key: string; count: number } | null {
  const top = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  return top ? { key: top[0], count: top[1] } : null;
}
