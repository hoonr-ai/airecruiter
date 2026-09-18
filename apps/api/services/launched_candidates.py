"""One definition of a job's launched candidates, shared by every screen.

The rank list (Rankings page), its header stats and the daily launch report
all describe the same thing — the candidates PAIR launched for a job — and
they must agree with each other. Historically each screen wrote its own SQL
and drifted: the launch report counted *interviews* (a re-launched person
counted twice) and only the ones created on the report day, while the rank
list counted *people* over the job's lifetime. This module is the single
population they now share:

  * A candidate is LAUNCHED when PAIR has an interview id for them on this
    job — either an ``engage_interview_audit`` row (written when the launch
    call to pair-bot succeeded) or ``sourced_candidates.data.engage_interview_id``
    (stamped by the same launch, and by every later status write-back, so it
    also covers a launch whose audit insert was lost).
  * A failed launch writes ``engage_status = 'failed'`` with an EMPTY interview
    id. That person was never launched and is not counted anywhere.
  * One row per PERSON (``candidate_id``), never per interview. A candidate who
    was launched twice for the same job is one launched candidate whose status
    is read from their latest interview — exactly how the rank list's table
    shows them.
  * Lifetime of the job, not a calendar day.

The row shape mirrors what the rank list's candidate query joins together for
each table row (latest audit row + the candidate's JSONB engage fields), so
callers can hand it to ``build_merged_outreach_payload`` and get the same
Pass / Fail / In Progress / Pending the table shows.

Keys: ``sourced_candidates`` / ``engage_interview_audit`` rows were written
under either the JobDiva ref (``26-01234``) or the numeric ``job_id`` text, so
every query here takes both keys (``jobdiva_id = %s OR jobdiva_id = %s``) and
callers pass the same value twice when a job only has one.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple


def key_pair(keys: Sequence[str]) -> Tuple[str, str]:
    """Two-slot key tuple for the ``(jobdiva_id = %s OR jobdiva_id = %s)`` SQL shape.

    A job with a single usable key repeats it so the SQL shape never changes.
    Blank keys are dropped first — ``''`` would match every referenceless job
    at once (see ``_fetch_jobs_launched_on`` in routers/launch_report.py).
    """
    cleaned = [str(k).strip() for k in keys if k is not None and str(k).strip()]
    if not cleaned:
        raise ValueError("at least one non-blank job key is required")
    return cleaned[0], (cleaned[1] if len(cleaned) > 1 else cleaned[0])


# ---------------------------------------------------------------------------
# Rank-list visibility of sourced rows
# ---------------------------------------------------------------------------
def sourced_visibility_sql(alias: str) -> str:
    """WHERE fragment hiding the rows the rank list never shows.

    Consumes TWO ``%s`` params: the job's key pair, for the correlated
    phone-duplicate subquery. Rules (each also mirrored in Python by
    tests/test_candidates_launched_counter.py):

      1. ``Auto_*@jobdiva.com`` synthetic emails.
      2. ``jobdiva.local`` applicants with no verified phone in the DB — the
         number embedded in ``pair-XXXX@no-email.jobdiva.local`` is unverified
         and must never be the sole basis for showing someone as reachable.
      3. ``jobdiva.local`` applicants that are phone-duplicates of a real
         candidate on the same job.

    ``%%`` is a literal percent because these statements are always executed
    with a params tuple (psycopg2 pyformat).
    """
    a = alias
    return f"""({a}.email IS NULL OR {a}.email NOT ILIKE 'Auto!_%%@jobdiva.com' ESCAPE '!')
                      AND NOT (
                          -- Hide jobdiva.local candidates with no verified phone in the DB.
                          -- The number embedded in pair-XXXX@no-email.jobdiva.local is unverified
                          -- and should never be the sole basis for showing a candidate as reachable.
                          {a}.email ILIKE '%%jobdiva.local%%'
                          AND REGEXP_REPLACE(COALESCE({a}.phone, ''), '\\D', '', 'g') = ''
                      )
                      AND NOT (
                          -- Exclude jobdiva.local applicants that are phone-duplicates of a real candidate
                          {a}.email ILIKE '%%jobdiva.local%%'
                          AND REGEXP_REPLACE(SPLIT_PART(COALESCE({a}.email, ''), '@', 1), '\\D', '', 'g') <> ''
                          AND EXISTS (
                              SELECT 1 FROM sourced_candidates sc2
                              WHERE (sc2.jobdiva_id = %s OR sc2.jobdiva_id = %s)
                                AND sc2.candidate_id != {a}.candidate_id
                                AND sc2.email NOT ILIKE '%%jobdiva.local%%'
                                AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g') <> ''
                                AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g')
                                    = REGEXP_REPLACE(SPLIT_PART(COALESCE({a}.email, ''), '@', 1), '\\D', '', 'g')
                          )
                      )"""


# ---------------------------------------------------------------------------
# Launched population
# ---------------------------------------------------------------------------
# JSONB engage fields the rank list promotes onto each table row. Read with
# ->> (text) and parsed in Python: they are written by two services and one
# malformed value must not abort the statement.
_ENGAGE_FIELDS = (
    "engage_status",
    "engage_score",
    "engage_candidate_score",
    "engage_hard_filter_status",
    "engage_completed_at",
    "engage_updated_at",
    "first_attempted_at",
    "first_completed_at",
    "phase",
    "outreach_phase",
    "channel",
    "outreach_channel",
)

_LAUNCHED_CTE_SQL = """
        WITH latest_audit AS (
            -- The rank list's `latest_audit`: newest successful launch row per
            -- person. Rows without an interview id are failed launch attempts.
            SELECT DISTINCT ON (candidate_id)
                candidate_id,
                interview_id,
                status,
                response,
                created_at
            FROM engage_interview_audit
            WHERE (jobdiva_id = %s OR jobdiva_id = %s)
              AND COALESCE(NULLIF(interview_id, ''), '') <> ''
              AND COALESCE(NULLIF(candidate_id, ''), '') <> ''
            ORDER BY candidate_id, id DESC
        ),
        latest_sourced AS (
            -- One JSONB snapshot per person (rows exist under both job keys;
            -- every launch/status write-back updates both, so newest wins).
            SELECT DISTINCT ON (candidate_id)
                candidate_id,
                NULLIF(BTRIM(COALESCE(data->>'engage_interview_id', '')), '') AS engage_interview_id,
                """ + ",\n                ".join(f"data->>'{f}' AS {f}" for f in _ENGAGE_FIELDS) + """
            FROM sourced_candidates
            WHERE (jobdiva_id = %s OR jobdiva_id = %s)
              AND COALESCE(NULLIF(candidate_id, ''), '') <> ''
            ORDER BY candidate_id, id DESC
        ),
        launched AS (
            SELECT
                COALESCE(la.candidate_id, sc.candidate_id)        AS candidate_id,
                -- Same precedence as the rank list row: the JSONB interview id
                -- (refreshed by every status write-back) over the audit one.
                COALESCE(sc.engage_interview_id, la.interview_id) AS interview_id,
                la.interview_id                                   AS audit_interview_id,
                la.status                                         AS audit_status,
                la.response                                       AS audit_response,
                la.created_at                                     AS audit_created_at,
                """ + ",\n                ".join(f"sc.{f}" for f in _ENGAGE_FIELDS) + """
            FROM latest_audit la
            FULL OUTER JOIN latest_sourced sc ON sc.candidate_id = la.candidate_id
            -- Launched = has an interview id from either side. A sourced row
            -- with only engage_status (e.g. 'failed' from a failed launch) is
            -- not launched.
            WHERE la.candidate_id IS NOT NULL OR sc.engage_interview_id IS NOT NULL
        )
"""

LAUNCHED_CANDIDATES_SQL = _LAUNCHED_CTE_SQL + """
        SELECT * FROM launched ORDER BY candidate_id
"""

LAUNCHED_CANDIDATE_COUNT_SQL = _LAUNCHED_CTE_SQL + """
        SELECT COUNT(*) FROM launched
"""


def _launched_params(keys: Sequence[str]) -> Tuple[str, str, str, str]:
    k1, k2 = key_pair(keys)
    return (k1, k2, k1, k2)


def fetch_launched_candidates(conn, keys: Sequence[str]) -> List[Dict[str, Any]]:
    """One dict per launched candidate for the job identified by ``keys``.

    Dict keys: candidate_id, interview_id (the one to ask pair-bot about),
    audit_interview_id, audit_status, audit_response, audit_created_at, and
    every name in ``_ENGAGE_FIELDS`` (raw JSONB text, possibly None).
    """
    with conn.cursor() as cur:
        cur.execute(LAUNCHED_CANDIDATES_SQL, _launched_params(keys))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def count_launched_candidates(conn, keys: Sequence[str]) -> int:
    """``len(fetch_launched_candidates(...))`` without pulling the rows.

    This is the rank list's "Candidates Launched"; the same population the
    header buckets and the launch report row are computed over, so the four
    status buckets always sum to it.
    """
    with conn.cursor() as cur:
        cur.execute(LAUNCHED_CANDIDATE_COUNT_SQL, _launched_params(keys))
        row = cur.fetchone()
        return int((row[0] if row else 0) or 0)


# ---------------------------------------------------------------------------
# Sourced population (the rank list's table rows)
# ---------------------------------------------------------------------------
SOURCED_CANDIDATES_SQL = """
        SELECT DISTINCT ON (sc.candidate_id)
            sc.candidate_id,
            -- Earliest sourcing of this person on the job, whichever key it was
            -- stored under; the DISTINCT ON keeps the newest row's JSONB.
            MIN(sc.created_at) OVER (PARTITION BY sc.candidate_id) AS created_at,
            sc.data->>'feedback_type'                AS feedback_type,
            sc.data->>'feedback_reason'              AS feedback_reason,
            sc.data->>'feedback_at'                  AS feedback_at,
            sc.data->>'first_attempted_at'           AS first_attempted_at,
            sc.data->>'first_completed_at'           AS first_completed_at,
            sc.data->>'engage_completed_at'          AS engage_completed_at,
            sc.data->>'engage_updated_at'            AS engage_updated_at,
            sc.data->>'engage_status'                AS engage_status,
            sc.data->>'engage_score'                 AS engage_score,
            sc.data->>'engage_hard_filter_status'    AS engage_hard_filter_status,
            sc.data->>'engage_interview_id'          AS engage_interview_id
        FROM sourced_candidates sc
        WHERE (sc.jobdiva_id = %s OR sc.jobdiva_id = %s)
          AND COALESCE(NULLIF(sc.candidate_id, ''), '') <> ''
          AND """ + sourced_visibility_sql("sc") + """
        ORDER BY sc.candidate_id, sc.id DESC
"""


def fetch_sourced_candidates(conn, keys: Sequence[str]) -> List[Dict[str, Any]]:
    """One dict per sourced candidate the rank list would list for the job.

    Applies the same visibility rules as the rank list so "Sourced" on the
    launch report equals the rank list's candidate total.
    """
    k1, k2 = key_pair(keys)
    with conn.cursor() as cur:
        cur.execute(SOURCED_CANDIDATES_SQL, (k1, k2, k1, k2))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def interview_id_of(row: Dict[str, Any]) -> Optional[str]:
    """The interview to ask pair-bot about for one launched-candidate row."""
    iid = str(row.get("interview_id") or row.get("engage_interview_id") or row.get("audit_interview_id") or "").strip()
    return iid or None
