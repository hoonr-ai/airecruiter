// Step-5 row-visibility classification, extracted from the wizard page so the
// precedence rules are unit-testable (node --test). One classification is
// shared by the visible-table filter and the all-hidden empty-state breakdown,
// so the two can never drift on what hides a row.

export type HiddenReason = "launched" | "excluded" | "filtered";

export type VisibilityContext = {
  // Composite `${source}:${candidate_id}` keys plus bare candidate_ids of
  // rows already saved for this job (the rank list hides them from Step 5).
  launchedKeys: ReadonlySet<string>;
  launchedIds: ReadonlySet<string>;
  // Non-empty exclusion reason (client employee / offer status) hides a row.
  isExcluded: (candidate: any) => boolean;
  // 0 disables the min-score filter.
  minScore: number;
  getScore: (candidate: any) => number;
  // Empty set disables the location filter.
  locationFilter: ReadonlySet<string>;
  getLocation: (candidate: any) => string;
  // Raw text-box value; trimmed/lowercased here.
  searchQuery: string;
};

export function candidateHiddenReason(
  c: any,
  ctx: VisibilityContext
): HiddenReason | null {
  const candId = c.candidate_id || c.jobdiva_candidate_id || c.id;
  const key = `${c.source ?? ""}:${candId}`;
  // Hide anyone already launched (now in sourced_candidates / the rank
  // list). Match on the composite source:id key, falling back to the bare
  // candidate_id so source-string drift between sourcing runs can't let a
  // launched candidate re-surface.
  if (ctx.launchedKeys.has(key) || ctx.launchedIds.has(String(candId))) {
    return "launched";
  }
  if (ctx.isExcluded(c)) return "excluded";
  // Progressive rows (agent_result / details_loaded) bypass score &
  // location filters so they stay visible while shimmering. Once the
  // scored patch lands they fall back into the normal filter pipeline.
  const stage = String(c?._stage || "");
  const awaitingScore = stage === "agent_result" || stage === "details_loaded";
  const awaitingDetails = stage === "agent_result";
  // Candidates we couldn't score (detail_failed → N/A) are exempt from the
  // min-score filter — a failed detail lookup must not hide a JobDiva row.
  // JobDiva-JobAgent rows are unscored BY DESIGN (no % is shown for agent
  // results), so a % filter can never hide them either.
  const isAgentRow = String(c?.source || "") === "JobDiva-JobAgent";
  if (ctx.minScore > 0 && !awaitingScore && !c?.detail_failed && !isAgentRow) {
    if (ctx.getScore(c) < ctx.minScore) return "filtered";
  }
  if (ctx.locationFilter.size > 0 && !awaitingDetails) {
    const loc = ctx.getLocation(c);
    if (!loc || !ctx.locationFilter.has(loc)) return "filtered";
  }
  const trimmedQuery = ctx.searchQuery.trim().toLowerCase();
  if (trimmedQuery) {
    const haystack = [
      c.name,
      c.firstName,
      c.lastName,
      c.email,
      c.phone,
      c.title,
      c.headline,
    ]
      .map((v) => String(v || "").toLowerCase())
      .join(" ");
    if (!haystack.includes(trimmedQuery)) return "filtered";
  }
  return null;
}

export type HiddenBreakdown = {
  bucket: number;
  launched: number;
  excluded: number;
  filtered: number;
};

// Feeds the all-hidden empty state: counts why each row of the active
// source bucket is not in the table. Categories are disjoint and follow the
// same precedence as candidateHiddenReason (launched > excluded > filtered),
// so bucket === launched + excluded + filtered + visible.
export function hiddenBreakdown(
  bucket: readonly any[],
  ctx: VisibilityContext
): HiddenBreakdown {
  let launched = 0;
  let excluded = 0;
  let filtered = 0;
  for (const c of bucket) {
    const reason = candidateHiddenReason(c, ctx);
    if (reason === "launched") launched++;
    else if (reason === "excluded") excluded++;
    else if (reason === "filtered") filtered++;
  }
  return { bucket: bucket.length, launched, excluded, filtered };
}
