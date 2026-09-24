"""Per-job candidate outcome counts for the admin reports, in one query.

Admin Analytics' job timeline and the Recruiter Analytics dashboard both need,
for many jobs at once, the numbers the rank list would show if you opened each
job: how many candidates passed / failed the PAIR interview, how much recruiter
feedback exists and of what kind, how the PAIR submits split between internal
(manager review) and external (client), and when the first external submit
happened. The Launch Report computes the same things in Python over its
launched population (routers/launch_report.py); this module is the set-based
equivalent over *every* candidate of the job, the same population as the jobs
dashboard's PAIR SUBMITS / FEEDBACK COMPLETED counters
(services/feedback_metrics.py). The one difference: those counters count a
person with a Submit on ANY of their stored rows, while this reads each
person's newest decision (below). They differ only for a person stored under
both job keys whose two rows hold different decisions.

Definitions are imported, never restated:

  * Pass / Fail / In Progress — `engage_display_sql`, the SQL twin of the rank
    list's `format_engage_status` over the stored candidate blob.
  * Feedback / Submit / Internal / External / Reject / Unreachable — the
    predicates in services/feedback_metrics.py.
  * Awaiting feedback — the interview is decided (Pass or Fail) and the
    recruiter has recorded no decision of any kind yet. The Launch Report's
    "Outstanding" is the same rule over its launched population.

A candidate can be stored under either key of the same job (the JobDiva ref
`monitored_jobs.jobdiva_id` or the numeric `monitored_jobs.job_id`), so every
count is per person across both keys (see JOB_CANDIDATE_METRICS_SQL).

Only the latest recruiter decision per candidate is stored (a JSONB merge), so
`first_external_submit_at` is the earliest *current* external submit.
"""

import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.engage_status import engage_display_sql
from services.feedback_metrics import (
    HAS_DECISION_SQL,
    IS_EXTERNAL_SUBMIT_SQL,
    IS_INTERNAL_SUBMIT_SQL,
    IS_REJECT_SQL,
    IS_SUBMIT_SQL,
    IS_UNREACHABLE_SQL,
)

logger = logging.getLogger(__name__)

# feedback_at is written as datetime.now(timezone.utc).isoformat(). Cast only
# values that look like an ISO timestamp so one malformed blob can't fail the
# whole statement (a bad ::timestamptz cast aborts every row, not just its own).
_ISO_PREFIX_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"


def _feedback_at_sql(alias: str = "sc.") -> str:
    raw = f"{alias}data->>'feedback_at'"
    return f"(CASE WHEN {raw} ~ '{_ISO_PREFIX_RE}' THEN ({raw})::timestamptz END)"


_DISPLAY = engage_display_sql("sc.data")
_FEEDBACK_AT = _feedback_at_sql("sc.")


def _p(fragment: str) -> str:
    return fragment.format(alias="sc.")


# Output columns, in SELECT order after job_key.
METRIC_COLUMNS: Tuple[str, ...] = (
    "passed",
    "failed",
    "in_progress",
    "feedback_total",
    "pair_submits",
    "pair_internal_submits",
    "pair_external_submits",
    "rejects",
    "unreachable",
    "awaiting_feedback",
    "first_feedback_at",
    "first_external_submit_at",
    "first_internal_submit_at",
)

# Counted per PERSON, not per stored row. A candidate stored under both keys
# of a job has two rows, and the recruiter's decision sits on only one of them
# (the feedback endpoint writes one row). Counting rows made that person both
# "submitted" and "awaiting feedback" at once. So rows are first collapsed to
# one line per (job, candidate):
#   * outcome — the furthest-along reading on any row (Pass > Fail >
#     In Progress > Pending), as the rank list never demotes a decided outcome;
#   * decision — the NEWEST recorded decision across the rows (by feedback_at),
#     the same rule the Launch Report applies to its launched population.
JOB_CANDIDATE_METRICS_SQL = f"""
WITH jobs(job_key, ref) AS (
    SELECT * FROM unnest(%s::text[], %s::text[])
),
keys AS (
    SELECT job_key, job_key AS k FROM jobs
    UNION
    SELECT job_key, ref AS k FROM jobs WHERE NULLIF(TRIM(COALESCE(ref, '')), '') IS NOT NULL
),
candidate_rows AS (
    SELECT
        keys.job_key,
        sc.candidate_id,
        sc.id AS row_id,
        {_DISPLAY} AS display,
        CASE
            WHEN {_p(IS_INTERNAL_SUBMIT_SQL)} THEN 'internal'
            WHEN {_p(IS_EXTERNAL_SUBMIT_SQL)} THEN 'external'
            WHEN {_p(IS_REJECT_SQL)} THEN 'reject'
            WHEN {_p(IS_UNREACHABLE_SQL)} THEN 'unreachable'
            WHEN {_p(HAS_DECISION_SQL)} THEN 'other'
        END AS decision,
        {_FEEDBACK_AT} AS feedback_at
    FROM keys
    JOIN sourced_candidates sc ON sc.jobdiva_id = keys.k
    -- OFFSET 0 fences the subquery so display / decision / feedback_at are
    -- computed once per row, not re-derived (re-reading the large blob)
    -- inside every aggregate of `people` that uses them.
    OFFSET 0
),
people AS (
    SELECT
        job_key,
        candidate_id,
        CASE
            WHEN bool_or(display = 'Pass') THEN 'Pass'
            WHEN bool_or(display = 'Fail') THEN 'Fail'
            WHEN bool_or(display = 'In Progress') THEN 'In Progress'
            ELSE 'Pending'
        END AS display,
        (array_agg(decision ORDER BY feedback_at DESC NULLS LAST, row_id DESC)
            FILTER (WHERE decision IS NOT NULL))[1] AS decision,
        (array_agg(feedback_at ORDER BY feedback_at DESC NULLS LAST, row_id DESC)
            FILTER (WHERE decision IS NOT NULL))[1] AS decision_at,
        MIN(feedback_at) FILTER (WHERE decision IS NOT NULL) AS first_decision_at
    FROM candidate_rows
    GROUP BY job_key, candidate_id
)
SELECT
    job_key,
    COUNT(*) FILTER (WHERE display = 'Pass') AS passed,
    COUNT(*) FILTER (WHERE display = 'Fail') AS failed,
    COUNT(*) FILTER (WHERE display = 'In Progress') AS in_progress,
    COUNT(*) FILTER (WHERE decision IS NOT NULL) AS feedback_total,
    COUNT(*) FILTER (WHERE decision IN ('internal', 'external')) AS pair_submits,
    COUNT(*) FILTER (WHERE decision = 'internal') AS pair_internal_submits,
    COUNT(*) FILTER (WHERE decision = 'external') AS pair_external_submits,
    COUNT(*) FILTER (WHERE decision = 'reject') AS rejects,
    COUNT(*) FILTER (WHERE decision = 'unreachable') AS unreachable,
    COUNT(*) FILTER (WHERE display IN ('Pass', 'Fail') AND decision IS NULL) AS awaiting_feedback,
    MIN(first_decision_at) AS first_feedback_at,
    MIN(decision_at) FILTER (WHERE decision = 'external') AS first_external_submit_at,
    MIN(decision_at) FILTER (WHERE decision = 'internal') AS first_internal_submit_at
FROM people
GROUP BY job_key
"""


def empty_metrics() -> Dict[str, Any]:
    return {
        col: (None if col.endswith("_at") else 0)
        for col in METRIC_COLUMNS
    }


def _as_utc(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, datetime.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def fetch_job_candidate_metrics(
    conn,
    jobs: Iterable[Tuple[Any, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Counts for each ``(job_id, jobdiva_id)`` pair, keyed by ``str(job_id)``.

    Every requested job gets an entry — jobs without candidates get zeros /
    None — so callers can index without a membership check. ``conn`` is a
    plain psycopg2 connection; the cursor is tuple-based. Raises on DB errors;
    callers own the fallback (admin analytics wraps it in `_section`).
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for job_id, ref in jobs:
        key = str(job_id or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((key, str(ref or "").strip()))

    result: Dict[str, Dict[str, Any]] = {key: empty_metrics() for key, _ in pairs}
    if not pairs:
        return result

    with conn.cursor() as cur:
        cur.execute(
            JOB_CANDIDATE_METRICS_SQL,
            ([k for k, _ in pairs], [r for _, r in pairs]),
        )
        for row in cur.fetchall():
            key = str(row[0])
            if key not in result:
                continue
            metrics = result[key]
            for col, value in zip(METRIC_COLUMNS, row[1:]):
                if col.endswith("_at"):
                    metrics[col] = _as_utc(value)
                else:
                    metrics[col] = int(value or 0)
    return result
