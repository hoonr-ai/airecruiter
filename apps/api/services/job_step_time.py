"""Time recruiters spend on a job-wizard step — Step 5 ("Source") for the reports.

Nothing measured this before 2026-09-23: Step 5 is the wizard's last step, so
the Amplitude `job_wizard_step_completed.duration_ms` timer never fires for it,
and PAIR's own tables only know when the job was launched. The wizard now
reports ACTIVE time while Step 5 is open: the tab is visible and the recruiter
has interacted within the last few minutes (apps/web/lib/step-time.ts). The
page sends it about once a minute, as a delta, to POST
/api/v1/jobs/{job}/step-time.

One row per (job, step, recruiter), so the reports can show both a job's total
and who spent it:

  active_ms         summed over every visit by that recruiter
  first_entered_at  the first time that recruiter opened the step on the job

Two numbers come out of it, computed in ONE place (fetch_step_metrics) so the
Launch Report, Admin Analytics and Recruiter Analytics can never disagree:

  Step 5 Active Time  SUM(active_ms) over all recruiters on the job.
  Step 5 → Launch     wall-clock from the job's first Step 5 entry (any
                      recruiter) to its first SUCCESSFUL launch — the Launch
                      Report's definition of launched (an audit row with an
                      interview id). None when either end is missing, or when
                      the launch came first (a job launched before this
                      tracking existed and re-opened later).

Jobs worked on before the tracking existed have no rows, and the reports show a
dash for them.
"""

import datetime
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

STEP_SOURCE = 5
# The page flushes about every 60s; a single report can never carry more than
# this, whatever the client sends (a bug, a clock jump, a replayed request).
MAX_ACTIVE_MS_PER_REPORT = 10 * 60 * 1000

# Naive engage_interview_audit.created_at is in the DB session zone — the same
# setting the Launch Report reads (routers/launch_report.REPORT_DB_TIMEZONE).
_DB_TIMEZONE = os.getenv("REPORT_DB_TIMEZONE", "UTC")

# Run by routers/jobs._ensure_monitored_jobs_schema at startup.
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS job_step_time (
        job_id TEXT NOT NULL,
        step INT NOT NULL,
        user_email TEXT NOT NULL,
        active_ms BIGINT NOT NULL DEFAULT 0,
        first_entered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (job_id, step, user_email)
    )""",
)

_UPSERT_SQL = """
INSERT INTO job_step_time (job_id, step, user_email, active_ms, first_entered_at, last_seen_at)
VALUES (%s, %s, %s, %s, NOW(), NOW())
ON CONFLICT (job_id, step, user_email) DO UPDATE
SET active_ms = job_step_time.active_ms + EXCLUDED.active_ms,
    last_seen_at = NOW()
"""


def clamp_active_ms(value: Any) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(ms, MAX_ACTIVE_MS_PER_REPORT))


def record_step_time(conn, job_id: str, step: int, user_email: str, active_ms: Any) -> None:
    """Add ``active_ms`` to the recruiter's row for this job/step (creating it,
    which stamps first_entered_at, when this is their first report). ``job_id``
    must already be the canonical monitored_jobs.job_id. Raises on DB errors;
    the endpoint decides how to degrade."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '2000ms'")
        cur.execute("SET LOCAL statement_timeout = '5000ms'")
        cur.execute(_UPSERT_SQL, (job_id, int(step), user_email, clamp_active_ms(active_ms)))
    conn.commit()


# One statement for many jobs. %s order: job keys, refs (the jobs CTE), step,
# the DB timezone for the audit's naive created_at.
STEP_METRICS_SQL = """
WITH jobs(job_key, ref) AS (
    SELECT * FROM unnest(%s::text[], %s::text[])
),
keys AS (
    SELECT job_key, job_key AS k FROM jobs
    UNION
    SELECT job_key, ref AS k FROM jobs WHERE NULLIF(TRIM(COALESCE(ref, '')), '') IS NOT NULL
),
step_time AS (
    SELECT
        t.job_id AS job_key,
        SUM(t.active_ms) AS active_ms,
        MIN(t.first_entered_at) AS first_entered_at,
        jsonb_object_agg(t.user_email, t.active_ms) AS by_user
    FROM job_step_time t
    JOIN jobs ON jobs.job_key = t.job_id
    WHERE t.step = %s
    GROUP BY t.job_id
),
launches AS (
    -- First SUCCESSFUL launch, the Launch Report's rule
    -- (routers/launch_report._fetch_jobs_launched_on): an audit row on either
    -- job key with a non-empty interview_id and candidate_id. The '' guard
    -- keeps a job with no JobDiva ref from matching unkeyed audit rows.
    SELECT keys.job_key, MIN(a.created_at) AT TIME ZONE %s AS first_launch_at
    FROM keys
    JOIN engage_interview_audit a ON a.jobdiva_id = keys.k
    WHERE NULLIF(a.jobdiva_id, '') IS NOT NULL
      AND COALESCE(NULLIF(a.interview_id, ''), '') <> ''
      AND COALESCE(NULLIF(a.candidate_id, ''), '') <> ''
    GROUP BY keys.job_key
)
SELECT jobs.job_key, st.active_ms, st.first_entered_at, st.by_user, l.first_launch_at
FROM jobs
LEFT JOIN step_time st ON st.job_key = jobs.job_key
LEFT JOIN launches l ON l.job_key = jobs.job_key
"""


def empty_step_metrics() -> Dict[str, Any]:
    return {
        "active_ms": None,
        "active_minutes": None,
        "first_entered_at": None,
        "first_launch_at": None,
        "to_launch_minutes": None,
        "by_user": {},
    }


def _aware(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, datetime.datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def to_launch_minutes(
    first_entered_at: Optional[datetime.datetime],
    first_launch_at: Optional[datetime.datetime],
) -> Optional[float]:
    """Minutes from first Step 5 entry to first successful launch, or None
    when either is missing or the launch came first."""
    entered, launched = _aware(first_entered_at), _aware(first_launch_at)
    if not entered or not launched or launched < entered:
        return None
    return round((launched - entered).total_seconds() / 60.0, 1)


def _merge_by_user(by_user: Any) -> Dict[str, int]:
    """{email: ms} with keys normalised the way the write path normalises them
    (strip + lowercase), SUMMING any keys that collide rather than letting one
    overwrite the other."""
    merged: Dict[str, int] = {}
    for key, value in (by_user or {}).items():
        email = str(key or "").strip().lower()
        if not email:
            continue
        merged[email] = merged.get(email, 0) + int(value or 0)
    return merged


def fetch_step_metrics(
    conn,
    jobs: Iterable[Tuple[Any, Any]],
    step: int = STEP_SOURCE,
) -> Dict[str, Dict[str, Any]]:
    """Step metrics for each ``(job_id, jobdiva_id)``, keyed by ``str(job_id)``.

    Every requested job gets an entry (empty_step_metrics() when nothing was
    recorded). ``active_ms`` / ``active_minutes`` are None — not 0 — for a job
    nobody has timed, so a report can tell "not tracked" from "0 minutes".
    ``by_user`` maps lowercased recruiter email → active ms. Plain tuple-cursor
    psycopg2 connection; raises on DB errors (callers own the fallback).
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for job_id, ref in jobs:
        key = str(job_id or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((key, str(ref or "").strip()))

    result: Dict[str, Dict[str, Any]] = {key: empty_step_metrics() for key, _ in pairs}
    if not pairs:
        return result

    with conn.cursor() as cur:
        cur.execute(
            STEP_METRICS_SQL,
            ([k for k, _ in pairs], [r for _, r in pairs], int(step), _DB_TIMEZONE),
        )
        for job_key, active_ms, first_entered_at, by_user, first_launch_at in cur.fetchall():
            key = str(job_key)
            if key not in result:
                continue
            entered = _aware(first_entered_at)
            launched = _aware(first_launch_at)
            ms = int(active_ms) if active_ms is not None else None
            result[key] = {
                "active_ms": ms,
                "active_minutes": round(ms / 60000.0, 1) if ms is not None else None,
                "first_entered_at": entered,
                "first_launch_at": launched,
                "to_launch_minutes": to_launch_minutes(entered, launched),
                "by_user": _merge_by_user(by_user),
            }
    return result
