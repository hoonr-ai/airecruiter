"""Single source of truth for the dashboard's PAIR feedback metrics.

Two columns come out of the same recruiter action and are deliberately
reported side by side, because they answer different questions:

  FEEDBACK COMPLETED — a recruiter recorded *any* decision on a candidate
    in PAIR (Submit or Reject) via the rank list or the evaluation report.
  PAIR SUBMITS — of those, the ones that were a Submit. PAIR mirrors each
    Submit into JobDiva as a candidate note with the action
    "PAIR Submit - Externally Submitted", linked to the job.

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
