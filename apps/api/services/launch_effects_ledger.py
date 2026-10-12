"""The first-launch side effects fire once per job version — decided by the
server, never by the browser's flags alone.

A job's FIRST launch fires two things a relaunch must not repeat:

  initial      the applicant sync (JobDiva applicants imported into the rank
               list) and the recruiter email's "campaign activated" form
  job posting  Email #2 to the job-posting team ("New Job Posting Request")

The browser used to decide both: ``is_initial_launch = wizardMode !== 'source'``
(the URL's ``?mode=``) and ``send_job_posting_email = boards selected``. So a
relaunch from edit mode, a Back-button revisit, "Resume Job Setup" on a job
first launched from Rankings, or the legacy missing-contacts second pass of
the SAME click all re-ran the applicant sync and re-mailed the posting team.

Now each effect is taken through a conditional claim on its monitored_jobs
column (``claim``), which succeeds at most once per job version and only for
the launch that produced the job's first interview:

  * the column is still NULL (nobody fired it yet), AND
  * the job had no successful launch before this launch started — its first
    interview (monitored_jobs.first_launch_at, falling back to the audit log
    when the stamp is missing) is at or after ``launch_started_at`` (1 s of
    app/DB clock skew tolerated).

A request that asks for neither effect never claims. A spawn failure hands the
claim back (``release``). A new version starts with both columns NULL
(routers/jobs.py new-version overrides). A DB error answers None: the caller
then falls back to the request's flags (fail-open — losing the email on a DB
blip is worse than the rare repeat it guards against).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

INITIAL = "initial_launch_effects_at"
JOB_POSTING = "job_posting_email_sent_at"
COLUMNS = (INITIAL, JOB_POSTING)

# Run by routers/jobs._ensure_monitored_jobs_schema at startup (autocommit).
_SCHEMA_STATEMENT = """DO $$
DECLARE
    present INT;
BEGIN
    IF to_regclass('monitored_jobs') IS NULL THEN
        RETURN;
    END IF;
    SELECT count(*) INTO present
      FROM pg_attribute
     WHERE attrelid = to_regclass('monitored_jobs') AND NOT attisdropped
       AND attname IN ('initial_launch_effects_at', 'job_posting_email_sent_at');
    IF present = 2 THEN
        RETURN;
    END IF;
    PERFORM set_config('lock_timeout', '5000ms', true);
    ALTER TABLE monitored_jobs
        ADD COLUMN IF NOT EXISTS initial_launch_effects_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS job_posting_email_sent_at TIMESTAMPTZ NULL;
END $$"""

SCHEMA_STATEMENTS: Tuple[str, ...] = (_SCHEMA_STATEMENT,)

# The row the launch treats as the job (services/launch_timing._JOB_ROW_SQL).
_TARGET_SQL = """
    SELECT job_id, jobdiva_id FROM monitored_jobs
    WHERE job_id::text = %(key)s OR jobdiva_id = %(key)s
    ORDER BY (job_id::text ~ '^[0-9]+$') DESC, created_at DESC
    LIMIT 1
"""


def _claim_sql(column: str) -> str:
    if column not in COLUMNS:
        raise ValueError(f"unknown launch-effects column {column!r}")
    from services.launch_timing import SUCCESSFUL_LAUNCH_AUDIT_SQL

    return f"""
        WITH target AS ({_TARGET_SQL})
        UPDATE monitored_jobs mj
        SET {column} = now()
        FROM target t
        WHERE mj.job_id = t.job_id
          AND mj.{column} IS NULL
          AND COALESCE(
                mj.first_launch_at,
                (SELECT MIN(a.created_at) AT TIME ZONE %(db_tz)s
                   FROM engage_interview_audit a
                  WHERE a.jobdiva_id IN (NULLIF(t.jobdiva_id, ''), t.job_id::text)
                    AND {SUCCESSFUL_LAUNCH_AUDIT_SQL}),
                'infinity'::timestamptz
              ) >= %(started)s::timestamptz - interval '1 second'
        RETURNING mj.job_id
    """


def _connect():
    from core.db import get_db_connection

    return get_db_connection()


def _db_tz() -> str:
    from services import launch_timing

    return launch_timing._DB_TIMEZONE


def _started(value: Any) -> datetime.datetime:
    """An aware launch-start moment from a datetime / ISO string; now() when
    missing (the most conservative boundary: only a first interview created
    from now on would count as this launch's)."""
    from services.launch_timing import _as_dt

    return _as_dt(value) or datetime.datetime.now(datetime.timezone.utc)


def claim(job_key: Any, column: str, *, launch_started_at: Any) -> Optional[bool]:
    """Take ``column``'s effect for this launch. True = fire it (claimed now);
    False = already fired for this version, or the job's first interview came
    from an earlier launch; None = could not tell (DB error / no job row)."""
    key = str(job_key or "").strip()
    if not key:
        return None
    started = _started(launch_started_at)
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(_claim_sql(column), {"key": key, "started": started, "db_tz": _db_tz()})
                won = cur.fetchone() is not None
                if not won:
                    cur.execute(_TARGET_SQL, {"key": key})
                    exists = cur.fetchone() is not None
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_effects_ledger claim failed job=%s column=%s: %s", key, column, exc)
        return None
    if not won and not exists:
        return None
    return won


def release(job_key: Any, column: str) -> None:
    """Hand a claim back (its effect could not be spawned). Never raises."""
    key = str(job_key or "").strip()
    if not key or column not in COLUMNS:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute(
                    f"WITH target AS ({_TARGET_SQL}) "
                    f"UPDATE monitored_jobs mj SET {column} = NULL FROM target t WHERE mj.job_id = t.job_id",
                    {"key": key},
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_effects_ledger release failed job=%s column=%s: %s", key, column, exc)


def first_launch_before(job_key: Any, launch_started_at: Any) -> Optional[bool]:
    """Did the job's first interview come from a launch that started before
    ``launch_started_at``? (This launch is then a RElaunch.) None = unknown."""
    key = str(job_key or "").strip()
    if not key:
        return None
    started = _started(launch_started_at)
    from services.launch_timing import SUCCESSFUL_LAUNCH_AUDIT_SQL

    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(
                    f"""
                    WITH t AS ({_TARGET_SQL})
                    SELECT COALESCE(
                        mj.first_launch_at,
                        (SELECT MIN(a.created_at) AT TIME ZONE %(db_tz)s
                           FROM engage_interview_audit a
                          WHERE a.jobdiva_id IN (NULLIF(t.jobdiva_id, ''), t.job_id::text)
                            AND {SUCCESSFUL_LAUNCH_AUDIT_SQL})
                    ) < %(started)s::timestamptz - interval '1 second'
                    FROM monitored_jobs mj JOIN t ON t.job_id = mj.job_id
                    """,
                    {"key": key, "started": started, "db_tz": _db_tz()},
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_effects_ledger first-launch check failed job=%s: %s", key, exc)
        return None
    if not row or row[0] is None:
        return False if row else None
    return bool(row[0])


def decide(
    job_key: Any,
    *,
    is_initial_launch: bool,
    send_job_posting_email: bool,
    launch_started_at: Any,
) -> Tuple[bool, bool, bool]:
    """(initial, job_posting, relaunch) for this launch: each requested flag
    checked against its claim, and whether the job's first interview predates
    this launch (Email #1's wording). A claim the DB could not answer (None)
    keeps the request's flag (fail-open). Claims are only attempted for
    requested effects, so a request for neither never writes the job row."""
    initial = False
    if is_initial_launch:
        won = claim(job_key, INITIAL, launch_started_at=launch_started_at)
        initial = True if won is None else won
    job_posting = False
    if send_job_posting_email:
        won = claim(job_key, JOB_POSTING, launch_started_at=launch_started_at)
        job_posting = True if won is None else won
    if (is_initial_launch and not initial) or (send_job_posting_email and not job_posting):
        logger.info(
            "launch_effects_ledger job=%s initial=%s->%s job_posting=%s->%s (already fired for this job)",
            job_key, is_initial_launch, initial, send_job_posting_email, job_posting,
        )
    return initial, job_posting, first_launch_before(job_key, launch_started_at) is True
