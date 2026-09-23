"""Who posted and who launched a job in PAIR.

The Launch Report and Admin Analytics show "Posted By" / "Launched By".
Before 2026-09-23 nothing recorded either person: `recruiter_emails` is the
list of *assigned* recruiters (JobDiva's recruiter / owner / account-manager
fields plus whoever saved the job), not the person who acted. Two columns on
`monitored_jobs` now carry it:

  pair_posted_by    the signed-in user who first saved the job in PAIR — the
                    job wizard's first save, the external-job create, or a
                    campaign adding the job. First writer wins.
  pair_launched_by  the signed-in user whose Launch PAIR click launched the
                    job. Stamped by POST /candidates/save, which only the
                    Launch PAIR flow calls with a real job. That save runs
                    BEFORE pair-bot is called, so the outcome is not known
                    yet: the column follows each new attempt until the job has
                    a successful launch (an audit row with an interview id —
                    the Launch Report's definition of launched) and is frozen
                    from then on. A failed first attempt by A and a successful
                    retry by B therefore reads "B". The same freeze keeps a job
                    that was already launched before the column existed from
                    being credited to whoever re-launches it; a job that
                    existed but had not launched yet is credited at its first
                    launch.

pair_posted_by cannot infer "already posted" from any other table, so it has
three states:

  ''    NOT_RECORDED — every row that existed when the column was added. The
        schema step adds the column with DEFAULT '' (backfilling existing rows
        in the catalog, no rewrite) and drops the default in the same
        transaction. Never stamped; the reports show a dash.
  NULL  a row created after that — eligible to be stamped.
  email the recorded person.

A job version (-vN clone) is created with both columns NULL, like its
`pair_launched_at`: its own first save / launch records its own people.

Nothing here may fail the request that triggers it.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Marks a job whose poster predates attribution; see the module docstring.
NOT_RECORDED = ""

# Schema statements, run by routers/jobs._ensure_monitored_jobs_schema (each
# in its own autocommit execute). pair_posted_by's backfill-then-drop-default
# must be ONE transaction: if the ADD committed and the DROP DEFAULT then lost
# a lock race, every job inserted until a later boot would be born '' and could
# never be stamped. A DO block runs both in one transaction, and checks the
# catalog first so later boots take no ACCESS EXCLUSIVE lock at all. The
# inner IF NOT EXISTS keeps concurrent workers (8 per host) a no-op.
SCHEMA_STATEMENTS = (
    """DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass('monitored_jobs')
              AND attname = 'pair_posted_by' AND NOT attisdropped
        ) THEN
            ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS pair_posted_by TEXT DEFAULT '';
            ALTER TABLE monitored_jobs ALTER COLUMN pair_posted_by DROP DEFAULT;
        END IF;
    END $$""",
    "ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS pair_launched_by TEXT",
)

# A successful launch of this monitored_jobs row: the same rule as the Launch
# Report's `launches` CTE and services/launched_candidates (non-empty
# interview_id and candidate_id), on either job key.
# The '' guard matches the Launch Report's: a job with no JobDiva ref must not
# match every audit row that was written with an empty key.
_HAS_SUCCESSFUL_LAUNCH_SQL = (
    "EXISTS (SELECT 1 FROM engage_interview_audit a "
    "WHERE NULLIF(a.jobdiva_id, '') IS NOT NULL "
    "AND (a.jobdiva_id = NULLIF(monitored_jobs.jobdiva_id, '') OR a.jobdiva_id = monitored_jobs.job_id::text) "
    "AND COALESCE(a.interview_id, '') <> '' AND COALESCE(a.candidate_id, '') <> '')"
)


def normalize_actor_email(email: Optional[str]) -> Optional[str]:
    value = (email or "").strip().lower()
    # The unauthenticated local-dev fallback identity is not a person.
    if not value or value == "unauthenticated@hoonr.ai":
        return None
    return value


def _stamp(column: str, job_key: Any, email: Optional[str], extra_where: str) -> None:
    actor = normalize_actor_email(email)
    key = str(job_key or "").strip()
    if not actor or not key:
        return
    try:
        from core.db import get_db_connection

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                cur.execute(
                    f"UPDATE monitored_jobs SET {column} = %s "
                    f"WHERE (job_id::text = %s OR jobdiva_id = %s) AND ({extra_where})",
                    (actor, key, key),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        # A missing column (the startup ALTER lost a lock race) or a lock
        # costs only the attribution, never the request that triggered it.
        logger.warning(f"[JobAttribution] could not stamp {column} for job {key}: {e}")


def stamp_job_posted_by(job_key: Any, email: Optional[str]) -> None:
    """Record who posted the job, unless someone already is. Best-effort."""
    _stamp("pair_posted_by", job_key, email, "pair_posted_by IS NULL")


def stamp_job_launched_by(job_key: Any, email: Optional[str]) -> None:
    """Record who is launching the job. Best-effort.

    Writes only while the job has no successful launch yet, so the last
    attempt before the first success is the one credited (see the module
    docstring). Its own statement, so a missing column or a lock costs only
    the attribution, never the `pair_launched_at` stamp the caller runs next.
    """
    _stamp("pair_launched_by", job_key, email, f"NOT {_HAS_SUCCESSFUL_LAUNCH_SQL}")


def attribution_fields(posted_by: Any, launched_by: Any) -> Dict[str, Optional[str]]:
    """The two report fields, normalised (blank → None)."""
    return {
        "posted_by": normalize_actor_email(posted_by if isinstance(posted_by, str) else None),
        "launched_by": normalize_actor_email(launched_by if isinstance(launched_by, str) else None),
    }
