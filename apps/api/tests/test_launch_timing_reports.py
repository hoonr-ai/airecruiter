"""Launch timings in the reports: the Launch Report's three new columns, the
PAIR Dashboard's two speed tiles and the Admin Analytics timeline's columns
next to the (relabelled) curate_launched_at.

All three read services/launch_timing.timings_for_jobs, so they cannot drift.
The Postgres tests skip unless a server is reachable (LAUNCH_REPORT_TEST_DSN);
the main one checks that the timing's T2 is exactly the Launch Report's PAIR
Launch for every job.
"""

import datetime
import os
from unittest.mock import MagicMock

import pytest

from routers import admin_analytics as aa
from routers import launch_report as lr
from services import launch_timing as lt
from services import pair_dashboard as dash

UTC = datetime.timezone.utc


def _utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def _timing(**overrides):
    t = lt.empty_timing("31990001", "26-001")
    t.update(overrides)
    return t


# ---------------------------------------------------------------------------
# Launch Report
# ---------------------------------------------------------------------------
def _job(**overrides):
    job = {
        "job_id": "31990001",
        "jobdiva_id": "26-001",
        "title": "Data Engineer",
        "version": 1,
        "job_created_at_text": "2026-09-22 08:00:00",
        "first_launch_at": datetime.datetime(2026, 9, 22, 10, 0),
    }
    job.update(overrides)
    return job


def test_launch_report_row_carries_the_launch_timings():
    timing = _timing(
        first_launch_clicked_at=_utc(2026, 9, 22, 9, 40),
        added_to_click_minutes=100.0,
        click_to_launch_minutes=20.0,
    )
    row = lr._build_row(_job(launch_timing=timing), [], [], {})
    assert row["launch_clicked_at"] == "2026-09-22T05:40:00-04:00"  # Eastern, like every timestamp
    assert row["added_to_click_minutes"] == 100.0
    assert row["click_to_launch_minutes"] == 20.0
    # Turn Around Time keeps its own definition (PAIR Launch − PAIR Published).
    assert row["turn_around_time_minutes"] == 120.0


def test_launch_report_row_without_timings_reads_not_tracked():
    for job in (_job(), _job(launch_timing=None), _job(launch_timing=lt.empty_timing("31990001"))):
        row = lr._build_row(job, [], [], {})
        assert (row["launch_clicked_at"], row["added_to_click_minutes"], row["click_to_launch_minutes"]) == (
            None, None, None,
        )


def test_load_report_inputs_attaches_timings_and_keeps_its_shape(monkeypatch):
    conn = MagicMock()
    jobs = [{"job_id": "31990001", "jobdiva_id": "26-001"}, {"job_id": "60", "jobdiva_id": ""}]
    monkeypatch.setattr(lr, "get_db_connection", lambda: conn)
    monkeypatch.setattr(lr, "_fetch_jobs_launched_on", lambda *a, **k: jobs)
    monkeypatch.setattr(lr, "fetch_launched_candidates", lambda *a, **k: [])
    monkeypatch.setattr(lr, "fetch_sourced_candidates", lambda *a, **k: [])
    monkeypatch.setattr(lr, "_load_step_metrics", lambda c, j: {})
    seen = {}

    def fake_timings(job_ids, c):
        seen["ids"], seen["conn"] = list(job_ids), c
        return {"31990001": _timing(click_to_launch_minutes=9.0)}

    monkeypatch.setattr(lr, "timings_for_jobs", fake_timings)
    out = lr._load_report_inputs(datetime.date(2026, 9, 22), datetime.date(2026, 9, 22), None)
    assert len(out) == 4  # (jobs, launched, sourced, step metrics), unchanged
    assert seen == {"ids": ["31990001", "60"], "conn": conn}
    assert out[0][0]["launch_timing"]["click_to_launch_minutes"] == 9.0
    assert out[0][1]["launch_timing"] is None
    assert conn.close.called


def test_launch_report_timing_failure_blanks_only_those_columns(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError('relation "launch_sessions" is broken')

    monkeypatch.setattr(lr, "timings_for_jobs", boom)
    assert lr._load_launch_timings(MagicMock(), [{"job_id": "1"}]) == {}


# ---------------------------------------------------------------------------
# PAIR Dashboard
# ---------------------------------------------------------------------------
def _req(key, job_ids):
    return dash.Req(key=key, ref=key.upper(), link_key=key, job_ids=list(job_ids))


def test_fold_takes_the_family_first_added_and_click_and_the_first_launch_b():
    req = _req("26-001", ["31990001", "26-001-v2"])
    timings = {
        "31990001": _timing(
            pair_added_at=_utc(2026, 9, 1, 8), first_launch_clicked_at=_utc(2026, 9, 1, 10),
            first_launch_at=_utc(2026, 9, 1, 10, 30), click_to_launch_seconds=1800.0,
        ),
        "26-001-v2": _timing(
            job_id="26-001-v2", pair_added_at=_utc(2026, 9, 5), first_launch_clicked_at=_utc(2026, 9, 5, 1),
            first_launch_at=_utc(2026, 9, 5, 1, 6), click_to_launch_seconds=360.0,
        ),
    }
    dash.fold_launch_timings([req], timings)
    assert req.added_to_click_hours == 2.0
    assert req.click_to_launch_hours == 0.5  # v1 launched first


def test_fold_ignores_a_later_versions_click_when_v1_launched_untracked():
    """Review finding: v1 added 08-01 and launched 08-02 with no click on
    record; a v2 clone is clicked 10-11 and launched 30 minutes later. The req
    launched about a day after it arrived — not 1704 hours — and its B is
    v1's (unknown), not v2's half hour."""
    req = _req("26-001", ["31990001", "26-001-v2"])
    timings = {
        "31990001": _timing(pair_added_at=_utc(2026, 8, 1), first_launch_at=_utc(2026, 8, 2)),
        "26-001-v2": _timing(
            job_id="26-001-v2", pair_added_at=_utc(2026, 10, 10), first_launch_clicked_at=_utc(2026, 10, 11),
            first_launch_at=_utc(2026, 10, 11, 0, 30), click_to_launch_seconds=1800.0,
        ),
    }
    dash.fold_launch_timings([req], timings)
    assert req.added_to_click_hours is None
    assert req.click_to_launch_hours is None


def test_fold_counts_a_click_before_the_familys_first_launch():
    """v1 clicked (failed), v2 cloned, clicked and launched: A runs from v1's
    arrival to v1's click, B is v2's (the first version that launched)."""
    req = _req("26-001", ["31990001", "26-001-v2"])
    timings = {
        "31990001": _timing(pair_added_at=_utc(2026, 10, 1), first_launch_clicked_at=_utc(2026, 10, 1, 6)),
        "26-001-v2": _timing(
            job_id="26-001-v2", pair_added_at=_utc(2026, 10, 5), first_launch_clicked_at=_utc(2026, 10, 6),
            first_launch_at=_utc(2026, 10, 6, 0, 15), click_to_launch_seconds=900.0,
        ),
    }
    dash.fold_launch_timings([req], timings)
    assert req.added_to_click_hours == 6.0
    assert req.click_to_launch_hours == 0.25


def test_fold_leaves_untracked_reqs_blank():
    req = _req("26-002", ["2"])
    req.added_to_click_hours = 99.0
    dash.fold_launch_timings([req], {"2": _timing(job_id="2", first_launch_clicked_at=_utc(2026, 9, 1))})
    assert req.added_to_click_hours is None and req.click_to_launch_hours is None


def test_attach_launch_timings_is_best_effort(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(lt, "timings_for_jobs", boom)
    req = _req("26-001", ["1"])
    assert dash.attach_launch_timings(MagicMock(), [req]) is False
    assert req.click_to_launch_hours is None


def test_funnel_speed_has_both_timings_with_n():
    a, b, c = _req("a", ["1"]), _req("b", ["2"]), _req("c", ["3"])
    a.added_to_click_hours, a.click_to_launch_hours = 1.0, 0.25
    b.added_to_click_hours, b.click_to_launch_hours = 3.0, 0.75
    c.added_to_click_hours = 5.0  # clicked, never launched
    funnel = dash.compute_funnel(
        cohort=[a, b, c], people=[], activities=[], attributor=dash.Attributor([a, b, c], []),
        activities_available=False, now=_utc(2026, 9, 25),
    )
    speed = {s["key"]: s for s in funnel["speed"]}
    a_tile, b_tile = speed["added_to_launch_click"], speed["launch_click_to_launch"]
    assert (a_tile["median_hours"], a_tile["mean_hours"], a_tile["jobs"]) == (3.0, 3.0, 3)
    assert (b_tile["median_hours"], b_tile["mean_hours"], b_tile["jobs"]) == (0.5, 0.5, 2)
    assert (b_tile["median_minutes"], b_tile["mean_minutes"]) == (30.0, 30.0)
    assert a_tile["label"] and a_tile["from"] and b_tile["label"] and b_tile["from"]
    # The existing tiles are untouched.
    assert {"time_to_launch", "time_to_first_pass", "time_to_first_submit", "time_to_first_submit_posted"} <= set(speed)


def test_funnel_speed_without_timings_reads_no_data():
    a = _req("a", ["1"])
    funnel = dash.compute_funnel(
        cohort=[a], people=[], activities=[], attributor=dash.Attributor([a], []),
        activities_available=False, now=_utc(2026, 9, 25),
    )
    tile = {s["key"]: s for s in funnel["speed"]}["launch_click_to_launch"]
    assert tile["jobs"] == 0 and tile["median_hours"] is None and tile["median_minutes"] is None


# ---------------------------------------------------------------------------
# Admin Analytics
# ---------------------------------------------------------------------------
def test_admin_timeline_merge_fills_the_click_next_to_curate_launched_at():
    rows = [
        {"job_id": "31990001", "curate_launched_at": "2026-09-22T09:50:00+00:00",
         **dict.fromkeys(aa._LAUNCH_TIMING_FIELDS)},
        {"job_id": "60", "curate_launched_at": None, **dict.fromkeys(aa._LAUNCH_TIMING_FIELDS)},
    ]
    aa._merge_launch_timings(rows, {"31990001": _timing(
        first_launch_clicked_at=_utc(2026, 9, 22, 9, 40), added_to_click_minutes=100.0, click_to_launch_minutes=20.0,
    )})
    assert rows[0]["launch_clicked_at"] == "2026-09-22T09:40:00+00:00"
    assert (rows[0]["added_to_click_minutes"], rows[0]["click_to_launch_minutes"]) == (100.0, 20.0)
    assert rows[0]["curate_launched_at"] == "2026-09-22T09:50:00+00:00"  # existing key kept
    assert rows[1]["launch_clicked_at"] is None
    aa._merge_launch_timings(rows, None)  # unavailable: nothing changes, nothing raises


def test_admin_timeline_rows_always_carry_the_timing_keys(monkeypatch):
    # The timeline's catalog probe would eat the mocked fetchall.
    monkeypatch.setattr(aa, "_missing_optional_columns", lambda conn: frozenset())
    mock_conn = MagicMock()
    cur = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (1,)
    now = datetime.datetime.now(UTC)
    row = dict.fromkeys(aa._TIMELINE_COLUMNS)
    row.update(job_id="31990001", jobdiva_id="26-001", title="T", customer_name="C", created_at=now,
               candidates_sourced=0, candidates_launched=0, jobdiva_total_subs=0, pair_external_subs=0)
    cur.fetchall.return_value = [tuple(row[c] for c in aa._TIMELINE_COLUMNS)]
    [out] = aa._compute_jobs_timeline(mock_conn, scope=None, job_metrics={})["rows"]
    # Born None, filled later by _merge_launch_timings; no extra statement here.
    assert cur.execute.call_count == 2
    for key in aa._LAUNCH_TIMING_FIELDS:
        assert key in out and out[key] is None


def test_admin_timeline_timings_failure_is_reported_not_raised(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("timeout")

    monkeypatch.setattr(aa, "timings_for_jobs", boom)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ("30s",)
    assert aa._timeline_launch_timings(conn, [{"job_id": "1"}]) is None
    assert aa._timeline_launch_timings(conn, []) == {}


# ---------------------------------------------------------------------------
# Postgres: the timing's T2 is the Launch Report's PAIR Launch
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")

_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT, jobdiva_id TEXT, title TEXT, enhanced_title TEXT, customer_name TEXT,
  recruiter_emails TEXT, posted_date TEXT, time_to_first_pass DOUBLE PRECISION,
  parent_job_id TEXT, version INT, created_at TEXT,
  pair_posted_by TEXT, pair_launched_by TEXT) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
  id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, interview_id TEXT,
  status TEXT, created_at TIMESTAMP) ON COMMIT DROP;
INSERT INTO monitored_jobs (job_id, jobdiva_id, title, version, created_at) VALUES
  ('26-06182', '26-06182', 'Data Engineer', 1, '2026-08-20 17:30:00 IST'),
  ('26-06182-v2', '26-06182-v2', 'Data Engineer', 2, '2026-08-27 18:00:00'),
  ('60', '', 'Ghost A', 1, '2026-08-27 09:00:00'),
  ('61', '', 'Ghost B', 1, '2026-08-27 09:00:00'),
  ('26-07100', '26-07100', 'Retried', 1, '2026-08-27 09:00:00');
INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, created_at) VALUES
  ('c1', '26-06182', '901', 'Initiated', '2026-08-28 01:00:00'),
  ('c3', '26-06182-v2', '903', 'Initiated', '2026-08-28 02:30:00'),
  ('g1', '60', '960', 'Initiated', '2026-08-28 02:10:00'),
  ('g2', '', '961', 'Initiated', '2026-08-28 02:20:00'),
  ('r1', '26-07100', '', 'failed', '2026-08-28 01:40:00'),
  ('r1', '26-07100', '971', 'Initiated', '2026-08-30 01:40:00');
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
            cur.execute(_FIXTURE_SQL)
            for stmt in lt.SCHEMA_STATEMENTS:
                cur.execute(stmt)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_pg_timing_t2_is_the_launch_reports_pair_launch(pg):
    jobs = lr._fetch_jobs_launched_on(pg, datetime.date(2026, 8, 1), None, datetime.date(2026, 8, 31))
    assert {j["job_id"] for j in jobs} == {"26-06182", "26-06182-v2", "60", "26-07100"}
    timings = lr._load_launch_timings(pg, jobs)
    for job in jobs:
        assert timings[job["job_id"]]["first_launch_at"] == lr._from_db(job["first_launch_at"]), job["job_id"]
    # The connection is still usable for the reads that follow.
    with pg.cursor() as cur:
        cur.execute("SELECT 1")


def test_pg_report_row_end_to_end(pg):
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET first_launch_clicked_at = '2026-08-30T01:30:00Z', "
            "pair_added_at = '2026-08-27T09:00:00Z' WHERE job_id = '26-07100'"
        )
    jobs = {j["job_id"]: j for j in lr._fetch_jobs_launched_on(pg, datetime.date(2026, 8, 29), None)}
    job = jobs["26-07100"]
    job["launch_timing"] = lr._load_launch_timings(pg, [job]).get("26-07100")
    row = lr._build_row(job, [], [], {})
    assert row["launch_clicked_at"] == "2026-08-29T21:30:00-04:00"
    assert row["click_to_launch_minutes"] == 10.0
    assert row["added_to_click_minutes"] == round((2 * 24 * 60) + 16 * 60 + 30, 1)
    assert row["pair_launch_at"] == "2026-08-29T21:40:00-04:00"


def test_pg_admin_timeline_read_restores_the_statement_timeout(pg):
    with pg.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '30s'")
    timings = aa._timeline_launch_timings(pg, [{"job_id": "60"}, {"job_id": "26-06182"}])
    assert set(timings) == {"60", "26-06182"}
    with pg.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')")
        assert cur.fetchone()[0] == "30s"
