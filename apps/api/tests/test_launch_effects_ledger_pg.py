"""services/launch_effects_ledger.py against a real Postgres: the first-launch
side effects (applicant sync, job-posting email) fire once per job version,
only for the launch that produced the job's first interview — whatever the
browser's is_initial_launch / send_job_posting_email flags say.

Skipped when no Postgres is reachable (LAUNCH_REPORT_TEST_DSN, then
"dbname=postgres").
"""
import datetime as dt
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from services import launch_effects_ledger as ledger  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")
UTC = dt.timezone.utc

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, jobdiva_id TEXT, created_at TEXT DEFAULT now()::text,
  first_launch_at TIMESTAMPTZ);
CREATE TEMP TABLE engage_interview_audit (
  id SERIAL PRIMARY KEY, candidate_id VARCHAR(255), jobdiva_id VARCHAR(255),
  interview_id VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO monitored_jobs (job_id, jobdiva_id) VALUES ('100', '26-100');
"""


class _Shared:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_FIXTURE_SQL)
            for stmt in ledger.SCHEMA_STATEMENTS:
                cur.execute(stmt)
        monkeypatch.setattr(ledger, "_connect", lambda: _Shared(conn))
        monkeypatch.setattr("services.launch_timing._DB_TIMEZONE", "UTC")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _now():
    return dt.datetime.now(UTC)


def _first_interview(conn, at, *, stamp=True, key="26-100"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) VALUES ('c', %s, 'iv', %s)",
            (key, at.astimezone(UTC).replace(tzinfo=None)),
        )
        if stamp:
            cur.execute("UPDATE monitored_jobs SET first_launch_at = %s WHERE job_id = '100'", (at,))


def _decide(conn_key="26-100", *, started, initial=True, posting=True):
    return ledger.decide(conn_key, is_initial_launch=initial, send_job_posting_email=posting,
                         launch_started_at=started)


def test_the_first_launch_fires_both_once(pg):
    started = _now() - dt.timedelta(minutes=5)
    _first_interview(pg, started + dt.timedelta(minutes=2))       # this launch made the first interview
    assert _decide(started=started) == (True, True, False)
    # The same launch asking again (a retry of the effects) — already fired.
    assert _decide(started=started) == (False, False, False)


def test_a_relaunch_never_fires_them_whatever_the_browser_says(pg):
    first = _now() - dt.timedelta(days=2)
    _first_interview(pg, first)
    relaunch = _now() - dt.timedelta(minutes=1)
    # e.g. edit mode after a Back button, or "Resume Job Setup" on a job first
    # launched from Rankings — the ledger columns are still NULL here.
    assert _decide(started=relaunch) == (False, False, True)
    assert ledger.claim("100", ledger.INITIAL, launch_started_at=relaunch) is False


def test_the_audit_log_backs_a_missing_first_launch_stamp(pg):
    _first_interview(pg, _now() - dt.timedelta(days=1), stamp=False)    # T2 stamp lost
    assert _decide(started=_now()) == (False, False, True)


def test_a_launch_that_creates_nobody_yet_leaves_the_effects_for_the_real_first_launch(pg):
    # The ready pass of a click launched nobody; the needs-contact pass (same
    # click, a moment later) creates the job's first interview.
    click = _now() - dt.timedelta(minutes=10)
    continuation = _now() - dt.timedelta(minutes=2)
    _first_interview(pg, _now() - dt.timedelta(minutes=1))
    assert _decide(started=continuation) == (True, True, False)
    assert _decide(started=click) == (False, False, False)              # once


def test_only_requested_effects_are_claimed(pg):
    started = _now() - dt.timedelta(minutes=5)
    _first_interview(pg, _now())
    assert _decide(started=started, initial=False, posting=False) == (False, False, False)
    assert _decide(started=started, initial=True, posting=False) == (True, False, False)
    assert _decide(started=started, initial=False, posting=True) == (False, True, False)
    with pg.cursor() as cur:
        cur.execute("SELECT initial_launch_effects_at IS NOT NULL, job_posting_email_sent_at IS NOT NULL "
                    "FROM monitored_jobs WHERE job_id = '100'")
        assert cur.fetchone() == (True, True)


def test_release_hands_the_claim_back(pg):
    started = _now() - dt.timedelta(minutes=5)
    _first_interview(pg, _now())
    assert ledger.claim("100", ledger.INITIAL, launch_started_at=started) is True
    ledger.release("26-100", ledger.INITIAL)
    assert ledger.claim("100", ledger.INITIAL, launch_started_at=started) is True


def test_unknown_job_or_db_error_fails_open(pg, monkeypatch):
    assert ledger.claim("nope", ledger.INITIAL, launch_started_at=_now()) is None
    assert ledger.decide("nope", is_initial_launch=True, send_job_posting_email=True,
                         launch_started_at=_now())[:2] == (True, True)

    def broken():
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(ledger, "_connect", broken)
    assert ledger.decide("26-100", is_initial_launch=True, send_job_posting_email=False,
                         launch_started_at=_now()) == (True, False, False)


def test_iso_strings_and_naive_datetimes_are_accepted(pg):
    started = _now() - dt.timedelta(minutes=5)
    _first_interview(pg, _now())
    assert ledger.claim("100", ledger.INITIAL, launch_started_at=started.isoformat()) is True
    assert ledger.claim("100", ledger.JOB_POSTING, launch_started_at=started.replace(tzinfo=None)) is True


def test_unknown_column_is_refused():
    with pytest.raises(ValueError):
        ledger._claim_sql("first_launch_at")
