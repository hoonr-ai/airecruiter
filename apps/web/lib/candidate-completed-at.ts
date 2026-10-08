// Single source of truth for "when did this candidate complete their
// interview" — read by the Master Candidate Pool and Rankings tables (cell,
// CSV export, sort, column filter). Review on PR #777 flagged that each call
// site resolved this independently and in a different order, so a candidate
// could be filtered on one timestamp while a different one displayed.
//
// Precedence mirrors the backend exactly, on both sides:
//  - SQL (apps/api/routers/candidates.py, _launched_filter_conditions):
//    COALESCE(data->>'first_completed_at', data->>'engage_completed_at').
//  - The list endpoint already promotes that same precedence onto the
//    top-level field before the row ever reaches the client:
//    cand["engage_completed_at"] = first_completed_at when present, else
//    whatever was already stored under engage_completed_at.
// So the pre-merged top-level field is trusted first; the nested `data`
// fallback (for any candidate shape that skipped that merge) applies the
// identical first_completed_at-before-engage_completed_at order.
// `data` is typed as a bare index signature (not an object literal with only
// first_completed_at/engage_completed_at) so this accepts each page's own,
// unrelated CandidateData interface as-is — those don't declare these two
// keys either (the backend attaches them ad hoc), so a literal-shaped type
// here would fail TS's "no properties in common" structural check at every
// call site.
type CandidateCompletedAtInput = {
  engage_completed_at?: string | null;
  data?: Record<string, unknown> | null;
};

function asOptionalString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

export function getCandidateCompletedAt(c: CandidateCompletedAtInput): string | undefined {
  return (
    asOptionalString(c.engage_completed_at) ||
    asOptionalString(c.data?.first_completed_at) ||
    asOptionalString(c.data?.engage_completed_at)
  );
}
