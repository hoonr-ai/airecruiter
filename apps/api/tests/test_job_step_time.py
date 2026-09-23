"""services/job_step_time: recording Step 5 active time and the per-job read
all three reports share (Launch Report, Admin Analytics, Recruiter Analytics).

The Postgres tests skip when no server is reachable (set LAUNCH_REPORT_TEST_DSN,
e.g. to a pgserver instance). The session is pinned to UTC like managed PROD.
"""

import datetime
import os

import pytest

from services import job_step_time as jst

UTC = datetime.timezone.utc


def test_clamp_active_ms_bounds_every_report():
    assert jst.clamp_active_ms(30_000) == 30_000
    assert jst.clamp_active_ms(-5) == 0
    assert jst.clamp_active_ms("12") == 12
    assert jst.clamp_active_ms(None) == 0
    assert jst.clamp_active_ms("abc") == 0
    assert jst.clamp_active_ms(10**12) == jst.MAX_ACTIVE_MS_PER_REPORT


def test_to_launch_minutes():
    entered = datetime.datetime(2026, 9, 23, 13, 0, tzinfo=UTC)
    assert jst.to_launch_minutes(entered, entered + datetime.timedelta(minutes=95)) == 95.0
    assert jst.to_launch_minutes(entered, None) is None
    assert jst.to_launch_minutes(None, entered) is None
    # Launched before Step 5 was ever timed (a job re-opened after launch).
    assert jst.to_launch_minutes(entered, entered - datetime.timedelta(minutes=1)) is None
    # Naive values are UTC.
    assert jst.to_launch_minutes(entered.replace(tzinfo=None), entered + datetime.timedelta(hours=1)) == 60.0


def test_by_user_keys_are_normalised_and_summed():
    assert jst._merge_by_user({"A@x.com": 1000, " a@x.com ": 500, "b@x.com": None, "": 7}) == {
        "a@x.com": 1500,
        "b@x.com": 0,
    }
    assert jst._merge_by_user(None) == {}


def test_step_metrics_sql_placeholders():
    assert jst.STEP_METRICS_SQL.count("%s") == 4
    assert "%" not in jst.STEP_METRICS_SQL.replace("%s", "")


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_AUDIT_SQL = """
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), status VARCHAR(50) DEFAULT 'sent',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
"""


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            # The shipped DDL, as a temp table: same columns, key and defaults.
            cur.execute(jst.SCHEMA_STATEMENTS[0].replace("CREATE TABLE", "CREATE TEMP TABLE") + " ON COMMIT DROP")
            cur.execute(_AUDIT_SQL)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _set(conn, job_id, email, *, ms, entered, step=jst.STEP_SOURCE):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_step_time (job_id, step, user_email, active_ms, first_entered_at) VALUES (%s, %s, %s, %s, %s)",
            (job_id, step, email, ms, entered),
        )


def _audit(conn, key, interview_id, created, candidate_id="c1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) VALUES (%s, %s, %s, %s)",
            (candidate_id, key, interview_id, created),
        )


class _NoCommit:
    """record_step_time commits; the temp tables must outlive it."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass


def test_record_step_time_accumulates_per_recruiter(pg):
    pg = _NoCommit(pg)
    jst.record_step_time(pg, "31990001", 5, "a@x.com", 60_000)
    with pg.cursor() as cur:
        cur.execute("SELECT first_entered_at FROM job_step_time WHERE user_email = 'a@x.com'")
        first = cur.fetchone()[0]
    jst.record_step_time(pg, "31990001", 5, "a@x.com", 45_000)
    jst.record_step_time(pg, "31990001", 5, "a@x.com", 10**9)  # clamped
    jst.record_step_time(pg, "31990001", 5, "b@x.com", 0)  # an entry ping: row, no time
    with pg.cursor() as cur:
        cur.execute(
            "SELECT user_email, active_ms, first_entered_at FROM job_step_time "
            "WHERE job_id = '31990001' AND step = 5 ORDER BY user_email"
        )
        rows = cur.fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("a@x.com", 60_000 + 45_000 + jst.MAX_ACTIVE_MS_PER_REPORT),
        ("b@x.com", 0),
    ]
    assert rows[0][2] == first  # later reports never move the first entry


def test_fetch_step_metrics_per_job(pg):
    t = lambda h, m=0: datetime.datetime(2026, 9, 23, h, m, tzinfo=UTC)  # noqa: E731
    # Job A (ref 26-29267): two recruiters; first entry 13:00; a failed launch
    # attempt at 13:30, the first successful one at 15:00 (audited on the ref).
    _set(pg, "31990001", "Priya@x.com", ms=40 * 60_000, entered=t(13))
    _set(pg, "31990001", "sam@x.com", ms=20 * 60_000, entered=t(14))
    _set(pg, "31990001", "priya@x.com", ms=99, entered=t(12), step=4)  # another step: ignored
    _audit(pg, "26-29267", "", t(13, 30).replace(tzinfo=None))
    _audit(pg, "26-29267", "iv-1", t(15).replace(tzinfo=None))
    _audit(pg, "26-29267", "iv-2", t(16).replace(tzinfo=None))
    # Job B (no JobDiva ref): timed, launched under its numeric id; an unkeyed
    # audit row must not count as its launch.
    _set(pg, "-4", "ext@x.com", ms=5 * 60_000, entered=t(10))
    _audit(pg, "", "iv-unkeyed", t(9).replace(tzinfo=None))
    _audit(pg, "-4", "iv-3", t(11).replace(tzinfo=None))
    # Job C: launched before anyone timed Step 5 (tracking started later).
    _set(pg, "31990003", "late@x.com", ms=60_000, entered=t(18))
    _audit(pg, "26-29288", "iv-4", t(8).replace(tzinfo=None))
    # Job D: never timed, never launched.

    got = jst.fetch_step_metrics(
        pg, [("31990001", "26-29267"), ("-4", ""), ("31990003", "26-29288"), ("31990009", "26-29999")]
    )

    a = got["31990001"]
    assert a["active_ms"] == 60 * 60_000 and a["active_minutes"] == 60.0
    assert a["first_entered_at"] == t(13)
    assert a["first_launch_at"] == t(15)  # the failed 13:30 attempt is not a launch
    assert a["to_launch_minutes"] == 120.0
    assert a["by_user"] == {"priya@x.com": 40 * 60_000, "sam@x.com": 20 * 60_000}

    b = got["-4"]
    assert b["first_launch_at"] == t(11) and b["to_launch_minutes"] == 60.0

    c = got["31990003"]
    assert c["active_minutes"] == 1.0
    assert c["to_launch_minutes"] is None  # launched before Step 5 was timed

    assert got["31990009"] == jst.empty_step_metrics()


def test_fetch_step_metrics_empty_input_skips_the_query(pg):
    assert jst.fetch_step_metrics(pg, []) == {}
