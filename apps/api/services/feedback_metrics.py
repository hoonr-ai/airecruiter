"""Single source of truth for the dashboard's PAIR feedback metrics.

Two columns come out of the same recruiter action and are deliberately
reported side by side, because they answer different questions:

  FEEDBACK COMPLETED — a recruiter recorded *any* decision on a candidate
    in PAIR (Submit or Reject) via the rank list or the evaluation report.
  PAIR SUBMITS — of those, the ones that were a Submit. PAIR mirrors each
    Submit into JobDiva as a candidate note with the action
        "PAIR External Submission" for plain external submits or
        "PAIR Internal Submission" for manager-review submits, linked to the job.

They sit next to PAIR EXTERNAL SUBS, which is the JobDiva-verified count
(a JobDiva submittal to the job's contact whose candidate carries the
"PAIR Candidates = Pass" qualification) and lives in
`auto_assign_service._count_external_curate_submittals`. PAIR SUBMITS is
"what PAIR recorded"; PAIR EXTERNAL SUBS is "what JobDiva confirms". A gap
between the two is a real signal, so neither replaces the other.

The decision is stored on `sourced_candidates.data` as `feedback_type`
('Submit' | 'Reject') plus, for rejects only, `feedback_reason`.

Three call sites need the same definitions and used to be at risk of
drifting apart:

  1. `routers/candidates.save_candidate_feedback` — write-through so the
     numbers move the moment a recruiter clicks, not 15 minutes later.
  2. `services/auto_assign_service.refresh_job_performance_metrics` — the
     periodic auto-sync recompute.
  3. `routers/jobs._backfill_monitored_jobs_counters_sync` — the
     at-startup backfill that repairs every job at once.

Two historical bugs are pinned here so they can't come back:

  * A Submit carries no reason (the UI only asks for one on Reject), so a
    predicate that required a non-empty `feedback_reason` silently dropped
    every submitted candidate. Completion is about a decision existing,
    not about a reason existing.
  * A candidate can be stored under either key variant of the same job
    (`monitored_jobs.jobdiva_id` or `monitored_jobs.job_id`), so counts are
    DISTINCT on candidate_id — matching the other dashboard counters.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# A row carries a recorded recruiter decision of any kind. `->>` yields SQL
# NULL for both a missing key and a JSON null, so TRIM/NULLIF collapses
# missing, null and blank into "no decision".
_HAS_DECISION = "NULLIF(TRIM({alias}data->>'feedback_type'), '') IS NOT NULL"

# ...and specifically a Submit. Compared case-insensitively so a payload
# written as 'submit' still counts.
_IS_SUBMIT = "LOWER(TRIM({alias}data->>'feedback_type')) = 'submit'"


# Submit split by where it went (the SubmissionModal's choice, stored as
# `submission_type` since 2026-09-11). Internal = sent to a hiring manager for
# review ("PAIR Internal Submission" note); external = sent to the client
# ("PAIR External Submission"). A Submit with no `submission_type` predates
# the split and was an external submit — the endpoint's default — so it counts
# as external. `submission_type` is merged into the blob and never cleared by
# a later Reject/Unreachable, so every split predicate is gated on the row
# currently being a Submit.
_IS_INTERNAL_SUBMIT = (
    _IS_SUBMIT
    + " AND LOWER(TRIM(COALESCE({alias}data->>'submission_type', ''))) = 'internal'"
)
_IS_EXTERNAL_SUBMIT = (
    _IS_SUBMIT
    + " AND LOWER(TRIM(COALESCE({alias}data->>'submission_type', ''))) <> 'internal'"
)
# Reject variants ('Reject', 'Rejected', 'Reject - …') all start with
# "reject". LEFT() instead of LIKE 'reject%' keeps the fragment free of '%',
# so it can be spliced into a parameterised psycopg2 statement as-is.
_IS_REJECT = "LEFT(LOWER(TRIM({alias}data->>'feedback_type')), 6) = 'reject'"
_IS_UNREACHABLE = "LOWER(TRIM({alias}data->>'feedback_type')) = 'unreachable'"

# Public, alias-parameterised forms for report queries (admin analytics,
# recruiter analytics). Call .format(alias="sc.") — or alias="" for an
# unaliased sourced_candidates.
HAS_DECISION_SQL = _HAS_DECISION
IS_SUBMIT_SQL = _IS_SUBMIT
IS_INTERNAL_SUBMIT_SQL = _IS_INTERNAL_SUBMIT
IS_EXTERNAL_SUBMIT_SQL = _IS_EXTERNAL_SUBMIT
IS_REJECT_SQL = _IS_REJECT
IS_UNREACHABLE_SQL = _IS_UNREACHABLE


def _agg(predicate: str) -> str:
    return (
        "COUNT(DISTINCT CASE WHEN "
        + predicate.format(alias="sc.")
        + " THEN sc.candidate_id END)"
    )


# Aggregate forms for the set-based backfill (sourced_candidates aliased sc).
FEEDBACK_COMPLETED_AGG_SQL = _agg(_HAS_DECISION)
PAIR_SUBMITS_AGG_SQL = _agg(_IS_SUBMIT)

# Single-job form, both counts in one pass. The two %s params are the job's
# two key variants (jobdiva_id ref and job_id); a candidate stored under
# both counts once.
FEEDBACK_METRICS_SQL = (
    "SELECT "
    + "COUNT(DISTINCT CASE WHEN " + _HAS_DECISION.format(alias="") + " THEN candidate_id END), "
    + "COUNT(DISTINCT CASE WHEN " + _IS_SUBMIT.format(alias="") + " THEN candidate_id END) "
    + "FROM sourced_candidates WHERE (jobdiva_id = %s OR jobdiva_id = %s)"
)


def has_recorded_feedback(data: Optional[dict]) -> bool:
    """Python mirror of the FEEDBACK COMPLETED predicate."""
    if not isinstance(data, dict):
        return False
    return bool(str(data.get("feedback_type") or "").strip())


def is_pair_submit(data: Optional[dict]) -> bool:
    """Python mirror of the PAIR SUBMITS predicate."""
    if not isinstance(data, dict):
        return False
    return str(data.get("feedback_type") or "").strip().lower() == "submit"


def _feedback_type(data: Optional[dict]) -> str:
    if not isinstance(data, dict):
        return ""
    return str(data.get("feedback_type") or "").strip().lower()


def submission_kind(data: Optional[dict]) -> Optional[str]:
    """'internal' | 'external' for a current Submit, else None.

    Python mirror of IS_INTERNAL_SUBMIT_SQL / IS_EXTERNAL_SUBMIT_SQL: a Submit
    without `submission_type` (pre-2026-09-11) is external.
    """
    if not is_pair_submit(data):
        return None
    kind = str(data.get("submission_type") or "").strip().lower()
    return "internal" if kind == "internal" else "external"


def is_pair_reject(data: Optional[dict]) -> bool:
    """Python mirror of IS_REJECT_SQL."""
    return _feedback_type(data).startswith("reject")


def is_pair_unreachable(data: Optional[dict]) -> bool:
    """Python mirror of IS_UNREACHABLE_SQL."""
    return _feedback_type(data) == "unreachable"


def feedback_label(data: Optional[dict]) -> Optional[str]:
    """The recruiter decision as the rank list shows it: 'Submit' | 'Reject' |
    'Unreachable', or None when there is no decision. Unknown non-empty values
    are returned as stored so nothing is silently hidden."""
    raw = str((data or {}).get("feedback_type") or "").strip() if isinstance(data, dict) else ""
    if not raw:
        return None
    # Same predicates as the counters, so a row labelled Submit is always a
    # row counted in PAIR SUBMITS.
    if is_pair_submit(data):
        return "Submit"
    if is_pair_reject(data):
        return "Reject"
    if is_pair_unreachable(data):
        return "Unreachable"
    return raw


def count_feedback_metrics(cur, ref_id, num_id) -> Dict[str, int]:
    """Run both counts on an already-open plain (tuple) cursor."""
    cur.execute(FEEDBACK_METRICS_SQL, (str(ref_id or ""), str(num_id or "")))
    row = cur.fetchone()
    if not row:
        return {"feedback_completed": 0, "pair_submits": 0}
    return {"feedback_completed": int(row[0] or 0), "pair_submits": int(row[1] or 0)}


def refresh_feedback_metrics_sync(job_ref: str) -> Optional[Dict[str, int]]:
    """Recompute both PAIR feedback counters for one job and store them.

    Returns the fresh counts, or None if the job row was not found or the
    write failed. Runs blocking psycopg2 — call it via asyncio.to_thread
    from async paths.
    """
    from core.db import get_db_connection

    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # This runs inline on the feedback request, so keep it
                # bounded — a contended monitored_jobs row must not hold
                # the recruiter's click open.
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(
                    "SELECT job_id, jobdiva_id FROM monitored_jobs "
                    "WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                    (str(job_ref), str(job_ref)),
                )
                row = cur.fetchone()
                if not row:
                    return None
                resolved_job_id, resolved_ref = row[0], row[1]

                counts = count_feedback_metrics(cur, resolved_ref, resolved_job_id)
                cur.execute(
                    "UPDATE monitored_jobs SET feedback_completed = %s, pair_submits = %s, "
                    "updated_at = NOW() WHERE job_id = %s",
                    (counts["feedback_completed"], counts["pair_submits"], resolved_job_id),
                )
                conn.commit()
            return counts
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[FeedbackMetrics] Could not refresh PAIR feedback counters for job {job_ref}: {e}"
        )
        return None
