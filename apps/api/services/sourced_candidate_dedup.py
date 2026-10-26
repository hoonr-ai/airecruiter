"""Shared SQL fragment for picking one row among a multi-sourced
candidate's duplicate sourced_candidates rows.

sourced_candidates' real unique constraint is (jobdiva_id, candidate_id,
source), so a candidate tracked through more than one channel (JobDiva +
LinkedIn + manual, …) can have more than one row for the same candidate_id +
jobdiva_id. Every callback-related lookup that resolves "the" row for a
candidate+job — scheduling a callback, rescheduling it, marking it done,
cancelling it, and the reminder's own stale-check right before it fires —
needs to agree on which duplicate that is, or the two sides can drift:
whichever duplicate a write lands on isn't necessarily the one a later read
picks back up, which either creates a phantom second "Callback Scheduled"
entry (PR #854) or makes the reminder read a mismatched row and skip firing
as stale even though the actual schedule/reschedule succeeded.

Used by routers/candidates.py (save_candidate_feedback's own resolution,
_resolve_sourced_candidate_row_for_callback) and
services/notifications_service.py (_fetch_job_and_candidate's
prefer_callback_scheduled path, used only by the callback reminder's own
stale-check — every other notification type that function serves has no use
for "prefer the row that's already a callback" and keeps the plain
recency-only order).
"""

# COALESCE(..., FALSE), not a bare boolean expression: Postgres defaults to
# NULLS FIRST for DESC order, and data->>'feedback_type' IS NULL for a
# duplicate with no feedback at all — without the COALESCE, that NULL
# (neither true nor false) would sort AHEAD of a sibling that actually IS
# 'Callback Scheduled' (TRUE), defeating this tiebreaker's entire purpose
# (caught by this fix's own interleaving test, PR #854 review).
CALLBACK_ROW_TIEBREAKER = (
    "COALESCE(LOWER(TRIM(sc.data->>'feedback_type')) = 'callback scheduled', FALSE) DESC, "
    "sc.updated_at DESC NULLS LAST, sc.id DESC"
)
