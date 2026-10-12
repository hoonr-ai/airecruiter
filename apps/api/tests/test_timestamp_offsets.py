"""monitored_jobs.created_at is TEXT in three shapes; every reader must agree.

  "2026-06-09 21:05:35 IST"           readable_ist_now(): India wall clock
  "2026-06-09 21:05:35"               NOW() into a UTC session: UTC
  "2026-06-18 01:51:52.054974+05:30"  NOW() into a non-UTC session: the offset
                                      it carries is the truth

The third shape used to be truncated to 19 chars and read as UTC, which put
"Added on PAIR" (and the launch-timing backfill of pair_added_at) 5h30m late.
"""

import datetime
import os

import pytest

from routers._helpers import _EXPLICIT_OFFSET_RE, _ts_utc
from routers.launch_report import _parse_monitored_jobs_timestamp

UTC = datetime.timezone.utc

CASES = [
    ("2026-06-18 01:51:52.054974+05:30", datetime.datetime(2026, 6, 17, 20, 21, 52, tzinfo=UTC)),
    ("2026-06-18 01:51:52+0530", datetime.datetime(2026, 6, 17, 20, 21, 52, tzinfo=UTC)),
    ("2026-06-09 21:05:35 IST", datetime.datetime(2026, 6, 9, 15, 35, 35, tzinfo=UTC)),
    ("2026-06-09 21:05:35", datetime.datetime(2026, 6, 9, 21, 5, 35, tzinfo=UTC)),
    ("2026-06-09T21:05:35Z", datetime.datetime(2026, 6, 9, 21, 5, 35, tzinfo=UTC)),
    ("2026-06-09 21:05:35+00", datetime.datetime(2026, 6, 9, 21, 5, 35, tzinfo=UTC)),
    ("2026-06-09 21:05:35-04:00", datetime.datetime(2026, 6, 10, 1, 5, 35, tzinfo=UTC)),
    # A bare date's "-18" is not an offset.
    ("2026-06-18", datetime.datetime(2026, 6, 18, 0, 0, 0, tzinfo=UTC)),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_python_parser_honours_every_shape(raw, expected, monkeypatch):
    import routers.launch_report as lr

    monkeypatch.setattr(lr, "_DB_TZ", datetime.timezone.utc)
    got = _parse_monitored_jobs_timestamp(raw)
    assert got is not None
    # Sub-second precision is dropped by the 19-char window; compare to the second.
    assert got.astimezone(UTC).replace(microsecond=0) == expected


def test_sql_fragment_has_an_explicit_offset_branch_before_the_utc_default():
    sql = _ts_utc("created_at")
    assert "'Asia/Kolkata'" in sql
    assert _EXPLICIT_OFFSET_RE in sql
    assert sql.index(_EXPLICIT_OFFSET_RE) < sql.index("AT TIME ZONE 'UTC'")
    # Safe inside parameterised statements.
    assert "%" not in sql


def _dsn():
    return os.getenv("LAUNCH_REPORT_TEST_DSN")


@pytest.mark.parametrize("raw,expected", CASES)
def test_sql_fragment_matches_the_python_parser(raw, expected):
    dsn = _dsn()
    if not dsn:
        pytest.skip("LAUNCH_REPORT_TEST_DSN not set")
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no Postgres at LAUNCH_REPORT_TEST_DSN: {e}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(f"SELECT {_ts_utc('v')} FROM (SELECT %s::text AS v) s", (raw,))
            got = cur.fetchone()[0]
    finally:
        conn.close()
    assert got.astimezone(UTC).replace(microsecond=0) == expected
