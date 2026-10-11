"""Launch timings: when a job was added to PAIR, when a recruiter first clicked
Launch, and when the first interview was created.

Three per-job timestamps on ``monitored_jobs`` (one row per job VERSION; a
"-vN" clone starts with its own), all TIMESTAMPTZ:

  pair_added_at            T0  the row's birth. A column DEFAULT now() stamps
                               it at EVERY insert site (wizard fetch, draft
                               save fallback, external create, /monitor,
                               campaigns via monitor_job_locally); the
                               new-version clone overrides it to NOW().
                               Rows that predate the column are backfilled
                               once from ``created_at`` (backfill_once).
  first_launch_clicked_at  T1  the first "Source & Launch PAIR" click: the
                               server launch session's creation, or
                               POST /api/v1/jobs/{id}/launch-clicked from the
                               legacy browser-driven flow. Never backfilled —
                               no table recorded the click before this. Only a
                               click before the first launch counts: a job
                               launched before the tracking and relaunched
                               later keeps T1 (and so A and B) unknown, never
                               the relaunch's click.
  first_launch_at          T2  the first interview created: the first
                               engage_interview_audit row with an interview id,
                               the Launch Report's "PAIR Launch". Stamped by
                               the launch paths (stamp_first_launch) and
                               backfilled from the audit; every READ here
                               prefers the audit itself, so this module and the
                               Launch Report can never disagree.

Two durations come out of them, computed in ONE place (timings_for_jobs) so the
Launch Report, the PAIR Dashboard, Admin Analytics and the job endpoint agree:

  A  added → click   T1 − T0 at job level: how long the job sat before a
                     recruiter clicked Launch.
  B  click → launch  T2 − T1 of the FIRST launch session that produced an
                     interview (launch_sessions, when that table exists) — if
                     it was clicked no later than T2; a later one is a
                     relaunch — else the job-level T2 − T1. A failed first
                     click is not charged to B: B starts at the click that
                     worked. So A + B can differ from the Launch Report's Turn
                     Around Time (PAIR Launch − PAIR Published), which stays
                     as it was.

A negative span (clocks from different writers, or a launch that predates the
click tracking) is reported as None, never as a negative or zero duration.

Every write here is best-effort: a missing column (the startup ALTER lost a
lock race) or a lock costs the timestamp, never the request that triggered it.
Nothing sent to New Relic carries a name, email or phone number.
"""

import datetime
import logging
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc

# engage_interview_audit.created_at is a naive TIMESTAMP in the DB session zone
# — the setting the Launch Report reads (routers/launch_report.REPORT_DB_TIMEZONE).
_DB_TIMEZONE = os.getenv("REPORT_DB_TIMEZONE", "UTC")

TIMING_COLUMNS = ("pair_added_at", "first_launch_clicked_at", "first_launch_at")

BACKFILL_BATCH_SIZE = 500
# A runaway guard for the keyset loops; 2000 batches of 500 is 1M jobs.
_BACKFILL_MAX_BATCHES = 2000


# Adds the three columns, and pair_added_at's DEFAULT, in ONE transaction.
#
# The catalog is checked first, so every boot after the first takes no lock at
# all: a no-op ``ADD COLUMN IF NOT EXISTS`` still queues for ACCESS EXCLUSIVE
# behind any long reader, and every monitored_jobs reader queues behind the
# ALTER. One block takes that lock once, so the wait is capped at 5 s in all
# (inside the 10 s startup step) — a busy table costs this boot the columns,
# not a stalled site, and the next boot tries again. The columns land together
# or not at all.
#
# pair_added_at is added WITHOUT its default and the default set afterwards in
# the same transaction: rows that already exist stay NULL (the backfill fills
# them from created_at) and every later insert is stamped. A column that exists
# without its default (dropped by hand) gets it back.
_SCHEMA_STATEMENT = """DO $$
DECLARE
    present INT;
    has_default BOOLEAN;
BEGIN
    IF to_regclass('monitored_jobs') IS NULL THEN
        RETURN;
    END IF;
    SELECT count(*), COALESCE(bool_or(attname = 'pair_added_at' AND atthasdef), FALSE)
      INTO present, has_default
      FROM pg_attribute
     WHERE attrelid = to_regclass('monitored_jobs') AND NOT attisdropped
       AND attname IN ('pair_added_at', 'first_launch_clicked_at', 'first_launch_at');
    IF present = 3 AND has_default THEN
        RETURN;
    END IF;
    PERFORM set_config('lock_timeout', '5000ms', true);
    ALTER TABLE monitored_jobs
        ADD COLUMN IF NOT EXISTS pair_added_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS first_launch_clicked_at TIMESTAMPTZ NULL,
        ADD COLUMN IF NOT EXISTS first_launch_at TIMESTAMPTZ NULL;
    ALTER TABLE monitored_jobs ALTER COLUMN pair_added_at SET DEFAULT now();
END $$"""

# Run by routers/jobs._ensure_monitored_jobs_schema at startup, in its own
# autocommit execute. DDL only: the backfill is backfill_once(), a batched
# background task — a long UPDATE inside the 10 s startup schema step would
# hold the boot marker open and queue the next worker's ALTERs behind it.
SCHEMA_STATEMENTS: Tuple[str, ...] = (_SCHEMA_STATEMENT,)


# ---------------------------------------------------------------------------
# "First launch": the one definition every report shares
# ---------------------------------------------------------------------------
# A successful launch is an engage_interview_audit row with a non-empty
# interview_id AND candidate_id (services/launched_candidates.py). The '' guard
# keeps a job with no JobDiva ref from matching every audit row written with an
# empty key. Alias ``a`` = engage_interview_audit. No '%' and no placeholders.
SUCCESSFUL_LAUNCH_AUDIT_SQL = (
    "NULLIF(a.jobdiva_id, '') IS NOT NULL "
    "AND COALESCE(NULLIF(a.interview_id, ''), '') <> '' "
    "AND COALESCE(NULLIF(a.candidate_id, ''), '') <> ''"
)


def first_launch_sql(jobs: str = "monitored_jobs") -> str:
    """``SELECT job_id, first_launch_at`` for every row of ``jobs`` that has a
    successful launch. ``jobs`` is a table or CTE name with ``job_id`` and
    ``jobdiva_id`` columns.

    ``first_launch_at`` is the naive audit ``created_at`` (a REPORT_DB_TIMEZONE
    wall clock); callers convert it (``AT TIME ZONE`` in SQL, or attach the zone
    in Python). Audit rows are keyed by either job key, so the job's audit rows
    are the UNION ALL of two equality lookups — each can use the audit's
    jobdiva_id index, where one ``= ref OR = job_id`` join cannot. An audit row
    under both keys appears twice; the MIN is the same.
    No '%' and no placeholders: safe inside parameterised statements.
    """
    return f"""
        SELECT mj.job_id, MIN(a.created_at) AS first_launch_at
        FROM {jobs} mj
        JOIN LATERAL (
            SELECT a.created_at FROM engage_interview_audit a
            WHERE a.jobdiva_id = NULLIF(mj.jobdiva_id, '')
              AND {SUCCESSFUL_LAUNCH_AUDIT_SQL}
            UNION ALL
            SELECT a.created_at FROM engage_interview_audit a
            WHERE a.jobdiva_id = mj.job_id::text
              AND {SUCCESSFUL_LAUNCH_AUDIT_SQL}
        ) a ON TRUE
        GROUP BY mj.job_id
    """


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _connect():
    from core.db import get_db_connection

    return get_db_connection()


def _as_dt(value: Any) -> Optional[datetime.datetime]:
    """An aware datetime from a datetime or an ISO string (to_jsonb output);
    naive values are UTC. None for anything else."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _span(start: Any, end: Any) -> Optional[float]:
    """Seconds from start to end, or None when either is missing or the span
    is negative."""
    a, b = _as_dt(start), _as_dt(end)
    if a is None or b is None:
        return None
    seconds = (b - a).total_seconds()
    return round(seconds, 1) if seconds >= 0 else None


def _minutes(seconds: Optional[float]) -> Optional[float]:
    return round(seconds / 60.0, 1) if seconds is not None else None


def _iso(value: Any) -> Optional[str]:
    dt = _as_dt(value)
    return dt.astimezone(UTC).isoformat() if dt else None


def _stamp_value(at: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """``at`` made aware for the UPDATE; a naive value is an audit-style
    REPORT_DB_TIMEZONE reading. None means the DB's now()."""
    if at is None:
        return None
    if at.tzinfo is None:
        try:
            return at.replace(tzinfo=ZoneInfo(_DB_TIMEZONE))
        except Exception:  # noqa: BLE001 — a bad zone name reads as UTC
            return at.replace(tzinfo=UTC)
    return at


# ---------------------------------------------------------------------------
# Writes (best-effort, own connection; callers in async code use to_thread)
# ---------------------------------------------------------------------------
# One canonical row per key: a JobDiva internal id or a ref. When a key
# matches more than one row (a ref-keyed duplicate next to the numeric row),
# the tie-break is the launch code's own (routers/engagement.py, the job
# lookup in the interview launch): numeric job_id first, then the newest
# created_at — so the stamp lands on the row the launch treated as the job.
_JOB_ROW_SQL = """
    SELECT job_id FROM monitored_jobs
    WHERE job_id::text = %s OR jobdiva_id = %s
    ORDER BY (job_id::text ~ '^[0-9]+$') DESC, created_at DESC
    LIMIT 1
"""

# The row is written only when the column is blank or the new value is
# EARLIER (two writers arriving out of order still leave the first moment) —
# so the launch paths can call this for every batch without rewriting a hot
# monitored_jobs row each time. The matched job is returned either way.
_STAMP_SQL = """
WITH target AS ({target}), stamped AS (
    UPDATE monitored_jobs mj
    SET {column} = COALESCE(%s::timestamptz, now())
    FROM target
    WHERE mj.job_id = target.job_id
      AND (mj.{column} IS NULL OR mj.{column} > COALESCE(%s::timestamptz, now())){guard}
    RETURNING mj.job_id
)
SELECT job_id::text FROM target
"""

# T1 only: a click AFTER the job's first launch is a relaunch ("launch
# remaining", a retry, a job launched before the click tracking existed), not
# the click that launched it — recording it as the first click would make
# Added → Click span the whole first launch. Such a job keeps T1 blank.
_CLICK_GUARD_SQL = (
    "\n      AND (mj.first_launch_at IS NULL OR mj.first_launch_at >= COALESCE(%s::timestamptz, now()))"
)


def _check_stamp_args(job_key: Any, at: Any) -> None:
    """A wrong call (the spec's old ``(conn, job_id, at)`` shape) is a bug,
    not a DB blip: fail loudly instead of quietly logging a lost stamp."""
    if job_key is not None and (isinstance(job_key, bool) or not isinstance(job_key, (str, int))):
        raise TypeError(f"job key must be a str or int job id / JobDiva ref, not {type(job_key).__name__}")
    if at is not None and not isinstance(at, datetime.datetime):
        raise TypeError(f"at must be a datetime or None, not {type(at).__name__}")


def _stamp(column: str, job_key: Any, at: Optional[datetime.datetime]) -> Optional[str]:
    _check_stamp_args(job_key, at)
    key = str(job_key if job_key is not None else "").strip()
    if not key:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '5000ms'")
                value = _stamp_value(at)
                clicked = column == "first_launch_clicked_at"
                sql = _STAMP_SQL.format(
                    target=_JOB_ROW_SQL, column=column, guard=_CLICK_GUARD_SQL if clicked else ""
                )
                params = (key, key, value, value) + ((value,) if clicked else ())
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        # A missing column or a lock costs only this timestamp.
        logger.warning(f"[LaunchTiming] could not stamp {column} for job {key}: {e}")
        return None
    return str(row[0]) if row and row[0] is not None else None


def stamp_launch_clicked(job_id: Any, at: Optional[datetime.datetime] = None) -> Optional[str]:
    """T1: record the job's first Launch click — a no-op when an earlier click
    is recorded, or when the job's first launch (T2) is already recorded
    before ``at`` (a relaunch is not the first click). ``job_id`` (str or int)
    is the canonical id or the JobDiva ref; ``at`` (datetime) defaults to the
    DB clock. Returns the canonical job_id matched (stamped or not), or None
    when no job matched or the write failed (logged). Never raises on a DB
    failure; a wrong-typed argument (a bug in the caller) raises TypeError."""
    return _stamp("first_launch_clicked_at", job_id, at)


def stamp_first_launch(job_key: Any, at: Optional[datetime.datetime] = None) -> Optional[str]:
    """T2: record the job's first created interview (no-op when an earlier one
    is recorded). Call it when an audit row with an interview id is written.
    Same key / ``at`` / return / raising rules as stamp_launch_clicked; a
    naive ``at`` is read in REPORT_DB_TIMEZONE, like the audit's own column."""
    return _stamp("first_launch_at", job_key, at)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
_PRESENT_COLUMNS_SQL = (
    "SELECT attname FROM pg_attribute "
    "WHERE attrelid = to_regclass('monitored_jobs') AND attnum > 0 AND NOT attisdropped"
)
_TABLES_SQL = "SELECT to_regclass('engage_interview_audit') IS NOT NULL, to_regclass('launch_sessions') IS NOT NULL"
_SESSION_COLUMNS_SQL = (
    "SELECT attname FROM pg_attribute "
    "WHERE attrelid = to_regclass('launch_sessions') AND attnum > 0 AND NOT attisdropped"
)

# The launch_sessions columns session_timing reads, and nothing else: never
# input / checkpoint / error, which are large (candidate lists) and personal.
# A column the table does not have (yet) reads as NULL, never an error.
SESSION_COLUMNS: Tuple[str, ...] = (
    "id", "trigger", "mode", "status", "stage", "attempt", "clicked_at",
    "sourcing_started_at", "sourcing_done_at", "selection_done_at", "enrich_done_at",
    "review_started_at", "review_done_at", "save_started_at", "save_done_at",
    "launch_started_at", "first_interview_created_at", "last_batch_done_at",
    "first_dial_at", "completed_at", "human_wait_ms", "counts",
)


def _sessions_sql(present: Iterable[str], launched_only: bool) -> Optional[str]:
    """SELECT job_id, SESSION_COLUMNS... for the jobs in ``%s``; None when the
    table cannot answer (no job_id column, or ``launched_only`` and no
    first_interview_created_at). ``launched_only``: only the sessions that
    created an interview — all a timing needs; the job endpoint lists all."""
    present = set(present)
    if "job_id" not in present or (launched_only and "first_interview_created_at" not in present):
        return None
    cols = ", ".join(f's."{c}"' if c in present else "NULL" for c in SESSION_COLUMNS)
    where = " AND s.first_interview_created_at IS NOT NULL" if launched_only else ""
    return f"SELECT s.job_id::text, {cols} FROM launch_sessions s WHERE s.job_id::text = ANY(%s){where}"


def _tuple_cursor(conn):
    """A plain tuple cursor even on a get_dict_cursor_connection() connection
    (RealDictCursor by default): the reads here unpack rows by position."""
    import psycopg2.extensions

    return conn.cursor(cursor_factory=psycopg2.extensions.cursor)


def _timings_sql(exprs: Mapping[str, str], with_audit: bool) -> str:
    launches = f", launches AS ({first_launch_sql('jobs')})" if with_audit else ""
    audit_launch = "l.first_launch_at AT TIME ZONE %s" if with_audit else "NULL::timestamptz"
    join = "LEFT JOIN launches l ON l.job_id = j.job_id" if with_audit else ""
    return f"""
        WITH jobs AS (
            SELECT mj.job_id, mj.jobdiva_id,
                   {exprs['pair_added_at']} AS pair_added_at,
                   {exprs['first_launch_clicked_at']} AS first_launch_clicked_at,
                   {exprs['first_launch_at']} AS stamped_first_launch_at
            FROM monitored_jobs mj
            WHERE mj.job_id::text = ANY(%s)
        ){launches}
        SELECT j.job_id::text, j.jobdiva_id::text, j.pair_added_at, j.first_launch_clicked_at,
               j.stamped_first_launch_at, {audit_launch}
        FROM jobs j
        {join}
    """


def empty_timing(job_id: Any = None, jobdiva_id: Any = None) -> Dict[str, Any]:
    return {
        "job_id": str(job_id) if job_id is not None else None,
        "jobdiva_id": str(jobdiva_id or "").strip() or None,
        "pair_added_at": None,
        "first_launch_clicked_at": None,
        "first_launch_at": None,
        "added_to_click_seconds": None,
        "added_to_click_minutes": None,
        "click_to_launch_seconds": None,
        "click_to_launch_minutes": None,
        # "session": B from the first launch session that created an
        # interview; "job": the job-level T2 − T1 (legacy, browser-driven
        # launches); None: B unknown.
        "click_to_launch_source": None,
        "first_launch_session": None,
        # True when the only click on record came AFTER the job's first
        # launch (a relaunch of a job launched before the click tracking):
        # its first click is unknown, so T1, A and B read as not recorded.
        "click_recorded_after_launch": False,
    }


def _clean_label(value: Any, limit: int = 32) -> Optional[str]:
    text = re.sub(r"[^a-z0-9_]", "_", str(value or "").strip().lower())[:limit]
    return text or None


def session_timing(row: Mapping[str, Any]) -> Dict[str, Any]:
    """One launch session's timings from its launch_sessions row (a dict of
    datetimes, or of ISO strings from to_jsonb). Pure.

    click_to_launch_seconds is B for the session (click → first interview
    created); machine_seconds is B minus the human wait (QA review / the
    missing-contacts modal). Stage spans, in seconds:

      sourcing_s    sourcing_started_at → sourcing_done_at
      selection_s   sourcing_done_at (else the click) → selection_done_at
      enrichment_s  selection_done_at → enrich_done_at
      human_wait_s  human_wait_ms, else review_started_at → review_done_at
      saving_s      save_started_at → save_done_at
      pairbot_s     launch_started_at → first_interview_created_at
    """
    def at(key: str) -> Optional[datetime.datetime]:
        return _as_dt(row.get(key))

    clicked = at("clicked_at")
    first_interview = at("first_interview_created_at")
    human_ms = row.get("human_wait_ms")
    if isinstance(human_ms, (int, float)) and not isinstance(human_ms, bool) and human_ms >= 0:
        human_s: Optional[float] = round(human_ms / 1000.0, 1)
    else:
        human_s = _span(at("review_started_at"), at("review_done_at"))
    click_to_launch = _span(clicked, first_interview)
    machine = (
        round(max(click_to_launch - (human_s or 0.0), 0.0), 1) if click_to_launch is not None else None
    )
    counts = row.get("counts")
    return {
        "session_id": str(row.get("id") or row.get("session_id") or "") or None,
        "job_id": str(row.get("job_id") or "") or None,
        "trigger": _clean_label(row.get("trigger")),
        "mode": _clean_label(row.get("mode")),
        "status": _clean_label(row.get("status")),
        "stage": _clean_label(row.get("stage")),
        "attempt": row.get("attempt") if isinstance(row.get("attempt"), int) else None,
        "clicked_at": clicked,
        "first_interview_created_at": first_interview,
        "last_batch_done_at": at("last_batch_done_at"),
        "completed_at": at("completed_at"),
        "first_dial_at": at("first_dial_at"),
        "click_to_launch_seconds": click_to_launch,
        "machine_seconds": machine,
        "click_to_first_dial_seconds": _span(clicked, at("first_dial_at")),
        "sourcing_s": _span(at("sourcing_started_at"), at("sourcing_done_at")),
        "selection_s": _span(at("sourcing_done_at") or clicked, at("selection_done_at")),
        "enrichment_s": _span(at("selection_done_at"), at("enrich_done_at")),
        "human_wait_s": human_s,
        "saving_s": _span(at("save_started_at"), at("save_done_at")),
        "pairbot_s": _span(at("launch_started_at"), first_interview),
        "launch_total_s": _span(at("launch_started_at"), at("last_batch_done_at")),
        "counts": counts if isinstance(counts, dict) else {},
    }


def _first_launch_session(
    sessions: Sequence[Dict[str, Any]], launched: Optional[datetime.datetime]
) -> Optional[Dict[str, Any]]:
    """The earliest-clicked session that created an interview — when it was
    clicked no later than the job's first launch ``launched`` (T2). A session
    clicked after T2 relaunched a job some earlier launch (one from before
    the sessions existed) had already launched; its B is not the job's."""
    candidates = [
        s for s in sessions
        if s["clicked_at"] is not None and s["first_interview_created_at"] is not None
    ]
    if not candidates:
        return None
    first = min(candidates, key=lambda s: s["clicked_at"])
    if launched is not None and first["clicked_at"] > launched:
        return None
    return first


def _job_timing(
    job_id: str,
    jobdiva_id: Any,
    added: Any,
    clicked: Any,
    stamped_launch: Any,
    audit_launch: Any,
    sessions: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    out = empty_timing(job_id, jobdiva_id)
    added, clicked = _as_dt(added), _as_dt(clicked)
    # The audit decides T2 (the Launch Report's PAIR Launch); the stamped
    # column only stands in when the audit has nothing on this job.
    launched = _as_dt(audit_launch) or _as_dt(stamped_launch)
    if clicked is not None and launched is not None and clicked > launched:
        # The recorded click came after the first launch: a relaunch of a job
        # launched before the click tracking (or a lost first stamp). It is
        # not the job's first click, so it is neither T1 nor an end of A / B.
        clicked = None
        out["click_recorded_after_launch"] = True
    first = _first_launch_session(sessions, launched)
    if first is not None and (clicked is None or first["clicked_at"] < clicked):
        # Every session is a Launch click, so the one that launched the job is
        # an upper bound on the first click (it stands in for a lost stamp).
        clicked = first["clicked_at"]
        out["click_recorded_after_launch"] = False
    out.update(pair_added_at=added, first_launch_clicked_at=clicked, first_launch_at=launched)
    a = _span(added, clicked)
    out.update(added_to_click_seconds=a, added_to_click_minutes=_minutes(a))
    if first is not None and first["click_to_launch_seconds"] is not None:
        b, source = first["click_to_launch_seconds"], "session"
        out["first_launch_session"] = first
    else:
        b = _span(clicked, launched)
        source = "job" if b is not None else None
    out.update(click_to_launch_seconds=b, click_to_launch_minutes=_minutes(b), click_to_launch_source=source)
    return out


def _read_timings(
    conn, ids: List[str], include_sessions: bool
) -> Dict[str, Dict[str, Any]]:
    with _tuple_cursor(conn) as cur:
        cur.execute(_PRESENT_COLUMNS_SQL, ())
        present = {str(r[0]) for r in cur.fetchall() or []}
        cur.execute(_TABLES_SQL, ())
        has_audit, has_sessions = cur.fetchone()
        exprs = {
            col: (f"mj.{col}" if col in present else "NULL::timestamptz") for col in TIMING_COLUMNS
        }
        params: List[Any] = [ids]
        if has_audit:
            params.append(_DB_TIMEZONE)
        cur.execute(_timings_sql(exprs, bool(has_audit)), params)
        rows = cur.fetchall()
        sessions_by_job: Dict[str, List[Dict[str, Any]]] = {}
        sessions_sql = None
        if has_sessions:
            cur.execute(_SESSION_COLUMNS_SQL, ())
            session_cols = {str(r[0]) for r in cur.fetchall() or []}
            sessions_sql = _sessions_sql(session_cols, launched_only=not include_sessions)
        if sessions_sql:
            cur.execute(sessions_sql, (ids,))
            for row in cur.fetchall():
                session = dict(zip(SESSION_COLUMNS, row[1:]), job_id=row[0])
                sessions_by_job.setdefault(str(row[0]), []).append(session_timing(session))

    result: Dict[str, Dict[str, Any]] = {}
    for job_id, jobdiva_id, added, clicked, stamped_launch, audit_launch in rows:
        key = str(job_id)
        sessions = sorted(
            sessions_by_job.get(key, []),
            key=lambda s: s["clicked_at"] or datetime.datetime.min.replace(tzinfo=UTC),
        )
        timing = _job_timing(key, jobdiva_id, added, clicked, stamped_launch, audit_launch, sessions)
        if include_sessions:
            timing["sessions"] = sessions
        result[key] = timing
    return result


def timings_for_jobs(
    job_ids: Iterable[Any],
    conn=None,
    *,
    include_sessions: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Launch timings for each canonical monitored_jobs ``job_id``, keyed by
    ``str(job_id)``; a job_id with no monitored_jobs row is absent. Values are
    empty_timing()-shaped with AWARE datetimes (the reports format them).

    ``conn``: the caller's psycopg2 connection (any default cursor factory —
    the read opens plain tuple cursors). The read
    then runs inside a SAVEPOINT, so a failure here (a table or column the
    startup schema step has not created yet, a timeout) cannot abort the
    caller's transaction. Without one, a pooled connection is opened and
    closed. Raises on DB errors either way; callers own the fallback.
    """
    ids: List[str] = []
    seen = set()
    for job_id in job_ids or ():
        key = str(job_id if job_id is not None else "").strip()
        if key and key not in seen:
            seen.add(key)
            ids.append(key)
    if not ids:
        return {}

    if conn is None:
        own = _connect()
        try:
            with own.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '10000ms'")
            return _read_timings(own, ids, include_sessions)
        finally:
            try:
                own.rollback()
            except Exception:  # noqa: BLE001
                pass
            own.close()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT launch_timing_read")
    try:
        result = _read_timings(conn, ids, include_sessions)
    except Exception:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT launch_timing_read")
        raise
    with conn.cursor() as cur:
        cur.execute("RELEASE SAVEPOINT launch_timing_read")
    return result


_RESOLVE_JOB_SQL = f"SELECT job_id::text FROM ({_JOB_ROW_SQL}) target"
# A job page never needs every session of a long-lived job.
MAX_SESSIONS_IN_JOB_TIMING = 20


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def job_timing(job_id: Any) -> Optional[Dict[str, Any]]:
    """One job's timings, JSON-ready (UTC ISO strings), for
    GET /api/v1/jobs/{job_id}/launch-timing. ``job_id`` is the canonical id or
    the JobDiva ref. Includes ``sessions`` (newest last, at most
    MAX_SESSIONS_IN_JOB_TIMING) with each session's stage breakdown. None when
    no job matches; raises on DB errors."""
    key = str(job_id or "").strip()
    if not key:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '10000ms'")
            cur.execute(_RESOLVE_JOB_SQL, (key, key))
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        canonical = str(row[0])
        timing = timings_for_jobs([canonical], conn, include_sessions=True).get(canonical)
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
    if timing is None:
        return None
    timing["sessions"] = timing.get("sessions", [])[-MAX_SESSIONS_IN_JOB_TIMING:]
    return _json_ready(timing)


# ---------------------------------------------------------------------------
# New Relic
# ---------------------------------------------------------------------------
_MAX_COUNT_ATTRIBUTES = 30


def _count_attributes(counts: Any) -> Dict[str, int]:
    """Numeric counters as ``count_<key>`` (one nested level flattened as
    ``count_<key>_<sub>``). Keys are enums (stages, skip reasons); values are
    numbers only, so nothing personal can ride along."""
    out: Dict[str, int] = {}
    if not isinstance(counts, dict):
        return out

    def put(name: str, value: Any) -> None:
        if len(out) >= _MAX_COUNT_ATTRIBUTES:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        label = _clean_label(name, limit=40)
        if label:
            out[f"count_{label}"] = int(value)

    for key, value in counts.items():
        if isinstance(value, dict):
            for sub, sub_value in value.items():
                put(f"{key}_{sub}", sub_value)
        else:
            put(str(key), value)
    return out


def record_timing_event(
    session_row: Mapping[str, Any],
    *,
    job_added_at: Any = None,
    job_first_launch_at: Any = None,
) -> Optional[Dict[str, Any]]:
    """Emit one ``PairLaunchTiming`` New Relic custom event for a finished
    launch session (call it once, when the session completes).

    ``session_row`` is the launch_sessions row (dict). ``job_added_at`` is T0
    for added_to_click_s; the row's own ``job_added_at`` when omitted.
    ``job_first_launch_at`` is the job's T2 (monitored_jobs.first_launch_at),
    when the caller has it: a session clicked AFTER the job's first launch is
    a relaunch — sent with ``relaunch=true`` and without added_to_click_s,
    which would otherwise span the whole first launch. Only ids, enums,
    durations in seconds and counters are sent — never requested_by, names,
    emails, phones or error text. Returns the attributes (for logs and tests),
    or None if building them failed. Never raises.
    """
    try:
        t = session_timing(session_row)
        added = _as_dt(job_added_at) or _as_dt(session_row.get("job_added_at"))
        first_launch = _as_dt(job_first_launch_at)
        relaunch = (
            first_launch is not None and t["clicked_at"] is not None and first_launch < t["clicked_at"]
        ) if job_first_launch_at is not None else None
        attrs: Dict[str, Any] = {
            "job_id": t["job_id"],
            "session_id": t["session_id"],
            "trigger": t["trigger"],
            "mode": t["mode"],
            "status": t["status"],
            "attempt": t["attempt"],
            "relaunch": relaunch,
            "added_to_click_s": None if relaunch else _span(added, t["clicked_at"]),
            "click_to_launch_s": t["click_to_launch_seconds"],
            "machine_s": t["machine_seconds"],
            "click_to_first_dial_s": t["click_to_first_dial_seconds"],
            "sourcing_s": t["sourcing_s"],
            "selection_s": t["selection_s"],
            "enrichment_s": t["enrichment_s"],
            "human_wait_s": t["human_wait_s"],
            "saving_s": t["saving_s"],
            "pairbot_s": t["pairbot_s"],
            "launch_total_s": t["launch_total_s"],
        }
        attrs.update(_count_attributes(t["counts"]))
        attrs = {k: v for k, v in attrs.items() if v is not None}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[LaunchTiming] could not build the timing event: {e}")
        return None
    try:
        from core.newrelic import record_custom_event

        record_custom_event("PairLaunchTiming", attrs)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[LaunchTiming] could not record the timing event: {e}")
    return attrs


# ---------------------------------------------------------------------------
# One-time backfill (startup, background)
# ---------------------------------------------------------------------------
# Only text that starts with a real-looking "YYYY-MM-DD HH:MM:SS" is cast; an
# impossible date that still matches (Feb 30) fails its batch, which then
# retries row by row so one bad row costs only itself.
CREATED_AT_SHAPE_RE = (
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])[ T]([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
)


def _added_ids_sql() -> str:
    return (
        "SELECT job_id::text FROM monitored_jobs "
        "WHERE pair_added_at IS NULL AND created_at::text ~ %s AND job_id::text > %s "
        "ORDER BY job_id::text LIMIT %s"
    )


def _added_update_sql(one: bool) -> str:
    # created_at is TEXT holding readable_ist_now() India wall clock ("… IST")
    # or NOW() UTC; _ts_utc honours the suffix exactly as the reports do.
    # Never `::timestamptz` the text: Postgres reads the "IST" abbreviation
    # as Israel time.
    from routers._helpers import _ts_utc

    match = "job_id::text = %s" if one else "job_id::text = ANY(%s)"
    return (
        f"UPDATE monitored_jobs SET pair_added_at = {_ts_utc('created_at')} "
        f"WHERE {match} AND pair_added_at IS NULL AND created_at::text ~ %s"
    )


_LAUNCH_IDS_SQL = (
    "SELECT job_id::text FROM monitored_jobs "
    "WHERE first_launch_at IS NULL AND job_id::text > %s "
    "ORDER BY job_id::text LIMIT %s"
)


def _launch_update_sql() -> str:
    return f"""
        WITH batch AS (
            SELECT job_id, jobdiva_id FROM monitored_jobs WHERE job_id::text = ANY(%s)
        ),
        launches AS ({first_launch_sql('batch')})
        UPDATE monitored_jobs mj
        SET first_launch_at = l.first_launch_at AT TIME ZONE %s
        FROM launches l
        WHERE mj.job_id = l.job_id AND mj.first_launch_at IS NULL
    """


def _batch_limits(cur) -> None:
    cur.execute("SET LOCAL statement_timeout = '20s'")
    cur.execute("SET LOCAL lock_timeout = '2s'")


def _is_data_error(exc: Exception) -> bool:
    # SQLSTATE class 22: invalid datetime format / field overflow and friends.
    return str(getattr(exc, "pgcode", "") or "").startswith("22")


def _next_ids(conn, sql: str, params: Sequence[Any]) -> List[str]:
    with conn.cursor() as cur:
        _batch_limits(cur)
        cur.execute(sql, params)
        ids = [str(r[0]) for r in cur.fetchall()]
    conn.commit()
    return ids


def _backfill_added(conn, batch_size: int, summary: Dict[str, int]) -> None:
    after = ""
    for _ in range(_BACKFILL_MAX_BATCHES):
        ids = _next_ids(conn, _added_ids_sql(), (CREATED_AT_SHAPE_RE, after, batch_size))
        if not ids:
            return
        after = ids[-1]
        summary["batches"] += 1
        try:
            with conn.cursor() as cur:
                _batch_limits(cur)
                cur.execute(_added_update_sql(one=False), (ids, CREATED_AT_SHAPE_RE))
                summary["pair_added_at"] += max(cur.rowcount or 0, 0)
            conn.commit()
            continue
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            if not _is_data_error(e):
                summary["failed_batches"] += 1
                logger.warning(f"[LaunchTiming] pair_added_at backfill batch after {after!r} skipped: {e}")
                continue
        # One row in the batch is a shape-valid impossible date: go row by
        # row, each in a savepoint, so only that row is left NULL.
        with conn.cursor() as cur:
            _batch_limits(cur)
            for job_id in ids:
                cur.execute("SAVEPOINT launch_timing_row")
                try:
                    cur.execute(_added_update_sql(one=True), (job_id, CREATED_AT_SHAPE_RE))
                    summary["pair_added_at"] += max(cur.rowcount or 0, 0)
                    cur.execute("RELEASE SAVEPOINT launch_timing_row")
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT launch_timing_row")
                    summary["unparseable_rows"] += 1
                    logger.warning(f"[LaunchTiming] job {job_id}: created_at not a timestamp, pair_added_at left blank: {e}")
        conn.commit()


def _backfill_first_launch(conn, batch_size: int, summary: Dict[str, int]) -> None:
    after = ""
    for _ in range(_BACKFILL_MAX_BATCHES):
        ids = _next_ids(conn, _LAUNCH_IDS_SQL, (after, batch_size))
        if not ids:
            return
        after = ids[-1]
        summary["batches"] += 1
        try:
            with conn.cursor() as cur:
                _batch_limits(cur)
                cur.execute(_launch_update_sql(), (ids, _DB_TIMEZONE))
                summary["first_launch_at"] += max(cur.rowcount or 0, 0)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            summary["failed_batches"] += 1
            logger.warning(f"[LaunchTiming] first_launch_at backfill batch after {after!r} skipped: {e}")


def backfill_once(
    batch_size: int = BACKFILL_BATCH_SIZE,
    connect: Optional[Callable[[], Any]] = None,
) -> Dict[str, int]:
    """Fill pair_added_at (from created_at) and first_launch_at (from the
    audit) on rows that have none. Synchronous; run it off the event loop,
    once per boot:

        if await claim_once("backfill_launch_timing_v1"):
            await asyncio.to_thread(launch_timing.backfill_once)

    Idempotent and self-healing: it only ever fills NULLs, so a later boot
    picks up whatever an earlier one could not (a lock, a timeout, a launch
    whose stamp was lost). Keyset batches of ``batch_size`` jobs, each its own
    transaction with ``statement_timeout`` 20 s and ``lock_timeout`` 2 s, so it
    never holds monitored_jobs for long. Returns counts; never raises.
    """
    summary = {"pair_added_at": 0, "first_launch_at": 0, "batches": 0, "failed_batches": 0, "unparseable_rows": 0}
    try:
        conn = (connect or _connect)()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LaunchTiming] backfill skipped, no connection: {e}")
        return summary
    try:
        with conn.cursor() as cur:
            cur.execute(_PRESENT_COLUMNS_SQL, ())
            present = {str(r[0]) for r in cur.fetchall() or []}
            cur.execute(_TABLES_SQL, ())
            has_audit = bool(cur.fetchone()[0])
        conn.commit()
        for column, run, needs_audit in (
            ("pair_added_at", _backfill_added, False),
            ("first_launch_at", _backfill_first_launch, True),
        ):
            if column not in present or (needs_audit and not has_audit):
                continue
            try:
                run(conn, max(int(batch_size), 1), summary)
            except Exception as e:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(f"[LaunchTiming] {column} backfill stopped: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LaunchTiming] backfill failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"[LaunchTiming] backfill done: {summary}")
    return summary
