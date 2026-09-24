"""Admin Analytics report columns (2026-09-23) against a real Postgres.

  * the "Added on PAIR is +5:30" fix: IST-suffixed created_at text reads back
    at the true instant in the timeline, aged-unlaunched and weekly buckets;
  * the timeline SELECT's column names match _TIMELINE_COLUMNS;
  * per-job candidate metrics merge into the timeline without duplicating a
    job whose candidates sit under both job keys (the old feedback_times join
    did), and the metrics / submission split only ever see scoped jobs;
  * Step 5 time (services/job_step_time) merges the same way, for scoped jobs
    only, and a failed read blanks just its two columns.

Skips when no server is reachable (set LAUNCH_REPORT_TEST_DSN, e.g. to a
pgserver instance). The session is pinned to UTC like managed PROD; the
machine default here may be Asia/Kolkata, which would hide the bug.
"""

import datetime
import json
import logging
import os

import pytest

import routers.admin_analytics as aa
from services import job_candidate_metrics
from services import job_step_time

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

UTC = datetime.timezone.utc
IST_OFFSET = datetime.timedelta(hours=5, minutes=30)

_SCHEMA_SQL = """
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY, jobdiva_id TEXT, enhanced_title TEXT, title TEXT, customer_name TEXT,
    posted_date TEXT, created_at TEXT, pair_launched_at TIMESTAMP, outreach_stopped_at TIMESTAMP,
    is_archived BOOLEAN DEFAULT FALSE, archive_reason TEXT, status TEXT,
    candidates_sourced INT, candidates_launched INT, jobdiva_total_subs INT, pair_external_subs INT,
    complete_submissions INT, pass_submissions INT, pair_submits INT,
    campaign_id TEXT, recruiter_emails TEXT, pair_posted_by TEXT, pair_launched_by TEXT,
    parent_job_id TEXT, version INT DEFAULT 1
) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT, email TEXT, phone TEXT, data JSONB, status TEXT DEFAULT 'sourced',
    resume_match_percentage INT DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, interview_id TEXT,
    status TEXT DEFAULT 'sent', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE jobdiva_submittals (
    id SERIAL PRIMARY KEY, job_id TEXT NOT NULL, jobdiva_ref TEXT, candidate_id TEXT NOT NULL DEFAULT '',
    recipient_name TEXT, submit_date TIMESTAMP NULL, data JSONB, synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE unipile_account_usage (
    account_id TEXT PRIMARY KEY, account_name TEXT, use_count INT, last_used_at TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ, last_error TEXT
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
            cur.execute(_SCHEMA_SQL)
            # The shipped job_step_time DDL, as a temp table.
            cur.execute(
                job_step_time.SCHEMA_STATEMENTS[0].replace("CREATE TABLE", "CREATE TEMP TABLE")
                + " ON COMMIT DROP"
            )
        yield conn
    finally:
        conn.rollback()
        conn.close()


class _SharedConn:
    """The test connection as get_db_connection() hands it to the router:
    commit/close are no-ops so the ON COMMIT DROP tables outlive the call."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def rollback(self):
        self._conn.rollback()

    def commit(self):
        pass

    def close(self):
        pass


def _ist_text(instant: datetime.datetime) -> str:
    """What readable_ist_now() would have written at `instant`."""
    return (instant.astimezone(UTC) + IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S IST")


def _add_job(conn, job_id, **cols):
    cols = {"job_id": job_id, **cols}
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO monitored_jobs ({names}) VALUES ({marks})", list(cols.values()))


def _add_candidate(conn, key, cid, data, source="LinkedIn"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES (%s, %s, %s, %s)",
            (key, cid, source, json.dumps(data)),
        )


def _parse(iso):
    return datetime.datetime.fromisoformat(iso) if iso else None


# ---------------------------------------------------------------------------
# SELECT shape
# ---------------------------------------------------------------------------


def test_timeline_select_columns_match_the_unpacking(pg):
    with pg.cursor() as cur:
        cur.execute(aa._jobs_timeline_sql("TRUE"), [])
        names = tuple(d[0] for d in cur.description)
    assert names == aa._TIMELINE_COLUMNS


# ---------------------------------------------------------------------------
# +5:30: created_at written as India wall-clock text
# ---------------------------------------------------------------------------

TRUTH = datetime.datetime(2026, 9, 21, 13, 46, 55, tzinfo=UTC)  # 09:46:55 EDT


def test_ist_created_at_reads_back_at_the_true_instant(pg):
    _add_job(pg, "ist", created_at=_ist_text(TRUTH))  # "2026-09-21 19:16:55 IST"
    _add_job(pg, "utc_text", created_at="2026-09-21 13:46:55")
    _add_job(pg, "utc_frac", created_at="2026-09-21 13:46:55.123456")
    _add_job(pg, "blank", created_at="")

    rows = {r["job_id"]: r for r in aa._compute_jobs_timeline(pg, None, None)["rows"]}

    assert _ist_text(TRUTH) == "2026-09-21 19:16:55 IST"
    assert _parse(rows["ist"]["added_to_curate_at"]) == TRUTH
    assert rows["ist"]["added_to_curate_at"] == "2026-09-21T13:46:55+00:00"
    assert _parse(rows["utc_text"]["added_to_curate_at"]) == TRUTH
    assert _parse(rows["utc_frac"]["added_to_curate_at"]) == TRUTH
    assert rows["blank"]["added_to_curate_at"] is None


def test_timeline_sorts_ist_and_utc_rows_by_true_instant(pg):
    # Truly added 2h apart; read as UTC the IST row would sort 3h30m "later".
    _add_job(pg, "earlier_ist", created_at=_ist_text(TRUTH))
    _add_job(pg, "later_utc", created_at=(TRUTH + datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    rows = aa._compute_jobs_timeline(pg, None, None)["rows"]
    assert [r["job_id"] for r in rows] == ["later_utc", "earlier_ist"]


def test_aged_unlaunched_uses_the_true_instant(pg):
    now = datetime.datetime.now(UTC)
    # Truly 7 days 3 hours old. Read as UTC the IST text looks 6d 21h30m old,
    # which the old query did not count as aged.
    _add_job(pg, "aged", created_at=_ist_text(now - datetime.timedelta(days=7, hours=3)))
    _add_job(pg, "fresh", created_at=_ist_text(now - datetime.timedelta(days=6)))
    speed = aa._compute_launch_speed(pg, None)
    assert speed["unlaunched_active_jobs"] == 2
    assert speed["aged_unlaunched_jobs"] == 1


def test_weekly_jobs_added_bucket_uses_the_true_instant(pg):
    with pg.cursor() as cur:
        cur.execute("SELECT date_trunc('week', NOW())::date")
        this_monday = cur.fetchone()[0]
    # Truly Sunday 20:00 UTC of last week = Monday 01:30 IST of this week.
    sunday_evening = datetime.datetime.combine(
        this_monday - datetime.timedelta(days=1), datetime.time(20, 0), tzinfo=UTC
    )
    _add_job(pg, "sunday", created_at=_ist_text(sunday_evening))

    trends = aa._compute_weekly_trends(pg, None)

    assert trends["weeks"][-1] == this_monday.isoformat()
    assert trends["jobs_added"][-2:] == [1, 0]


# ---------------------------------------------------------------------------
# Version families: v1 and v2 of one JobDiva job count once
# ---------------------------------------------------------------------------


def test_jobdiva_totals_count_a_version_family_once(pg):
    """v2+ clones re-resolve to the same JobDiva job, so their JobDiva-derived
    counters repeat v1's. Summed per row, 26-06182 + its v2 read 6 confirmed
    here while Recruiter Analytics (MAX per family) read 3."""
    _add_job(pg, "26-06182", jobdiva_id="26-06182", pair_external_subs=3, jobdiva_total_subs=9,
             complete_submissions=2, pass_submissions=1, pair_submits=1)
    _add_job(pg, "26-06182-v2", jobdiva_id="26-06182-v2", parent_job_id="26-06182", version=2,
             pair_external_subs=3, jobdiva_total_subs=9,
             complete_submissions=4, pass_submissions=3, pair_submits=2)
    # No parent_job_id: still folds in through the stripped "-vN" suffix.
    _add_job(pg, "26-06182-V3", jobdiva_id="26-06182-V3", version=3,
             pair_external_subs=2, jobdiva_total_subs=8, pair_submits=1)
    _add_job(pg, "26-11111", jobdiva_id="26-11111", pair_external_subs=1, jobdiva_total_subs=2,
             pair_submits=5)

    sm = aa._compute_submission_metrics(pg, None, {})
    assert sm["pair_external_subs"] == 3 + 1        # MAX in the family, then summed
    assert sm["jobdiva_total_submittals"] == 9 + 2
    # Local PAIR counters are per version: plain sums.
    assert sm["complete_submissions"] == 6
    assert sm["pass_submissions"] == 4
    assert sm["pair_submits"] == 9

    # A team scope that holds only the clone still counts it.
    scoped = aa._compute_submission_metrics(pg, {"job_ids": ["26-06182-v2"]}, {})
    assert scoped["pair_external_subs"] == 3
    assert scoped["pair_submits"] == 2


@pytest.mark.parametrize("parent, jobdiva_id, job_id", [
    ("26-06182", "26-06182-v2", "26-06182-v2"),
    (" 26-06182 ", "26-06182-v2", "x"),
    (None, "26-06182-V3", "26-06182-V3"),
    ("", "26-06182", "31990001"),
    (None, None, "31990001-v2"),
    (None, "  ", "AbC-v12"),
    (None, "26-v2x", "j"),
])
def test_family_sql_matches_recruiter_analytics_family_key(pg, parent, jobdiva_id, job_id):
    """_JOB_FAMILY_SQL is the SQL twin of recruiter_analytics._family_key; if
    they drift, the two reports' JobDiva totals disagree again."""
    from routers.recruiter_analytics import _family_key

    _add_job(pg, job_id, jobdiva_id=jobdiva_id, parent_job_id=parent)
    with pg.cursor() as cur:
        cur.execute(f"SELECT {aa._JOB_FAMILY_SQL} FROM monitored_jobs")
        assert cur.fetchone()[0] == _family_key(parent, jobdiva_id, job_id)


# ---------------------------------------------------------------------------
# Candidate metrics merged into the timeline + the submission split
# ---------------------------------------------------------------------------

REF, NUM = "26-29267", "31990001"


def _t(hour):
    return f"2026-09-21T{hour:02d}:00:00+00:00"


def _seed(pg):
    # In scope (lead@x.com's job): launched, attribution recorded, IST created_at.
    _add_job(
        pg, NUM, jobdiva_id=REF, title="Data Engineer", customer_name="Acme",
        posted_date="Sep 20, 2026", created_at=_ist_text(TRUTH),
        pair_launched_at="2026-09-22 10:00:00", status="OPEN",
        candidates_sourced=12, candidates_launched=4, jobdiva_total_subs=4, pair_external_subs=1,
        pair_submits=2, recruiter_emails='["lead@x.com"]',
        pair_posted_by="Poster@X.com", pair_launched_by="launcher@x.com",
    )
    # In scope (member's job, shared with an outsider): not launched, no attribution.
    _add_job(
        pg, "31990002", jobdiva_id="26-00002", title="QA Analyst", created_at="2026-09-20 08:00:00",
        status="OPEN", recruiter_emails='["member@x.com", "outsider@y.com"]',
    )
    # Out of scope.
    _add_job(
        pg, "31990003", jobdiva_id="26-00003", title="Other Team Job", created_at="2026-09-19 08:00:00",
        status="OPEN", recruiter_emails='["outsider@y.com"]',
    )

    # Feedback under BOTH keys of the first job — the case where the old
    # feedback_times join returned the job twice.
    _add_candidate(pg, REF, "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": _t(15)})
    _add_candidate(pg, NUM, "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": _t(15)},
                   source="JobDiva")
    _add_candidate(pg, REF, "c2", {"engage_status": "completed", "engage_hard_filter_status": "fail",
                                   "feedback_type": "Submit", "submission_type": "internal", "feedback_at": _t(14)})
    _add_candidate(pg, NUM, "c3", {"engage_status": "failed", "engage_score": 40,
                                   "feedback_type": "Reject", "feedback_at": _t(12)})
    _add_candidate(pg, "26-00002", "c4", {"engage_status": "passed"})
    _add_candidate(pg, "26-00003", "c5", {"engage_status": "passed", "feedback_type": "Submit",
                                          "submission_type": "internal", "feedback_at": _t(9)})


@pytest.fixture()
def analytics(pg, monkeypatch, caplog):
    """Run _compute_analytics_sync on the test connection, recording which
    jobs reach the metrics query."""
    from services import teams_db

    monkeypatch.setattr(aa, "get_db_connection", lambda: _SharedConn(pg))
    monkeypatch.setattr(teams_db, "get_team", lambda team_id: {
        "id": team_id, "name": "Team 1", "lead_emails": ["Lead@X.com"], "member_emails": ["member@x.com"],
    })
    metrics_calls = []
    real_fetch = job_candidate_metrics.fetch_job_candidate_metrics

    def spy(conn, jobs):
        jobs = list(jobs)
        metrics_calls.append(jobs)
        return real_fetch(conn, jobs)

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", spy)
    _seed(pg)

    def run(team_id=None):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=aa.logger.name):
            data = aa._compute_analytics_sync(team_id)
        # Every section ran: no "section ... unavailable" fallbacks.
        assert not [r for r in caplog.records if r.name == aa.logger.name], caplog.text
        return data

    return run, metrics_calls


def test_team_view_merges_metrics_one_row_per_job(analytics):
    run, metrics_calls = analytics
    data = run("t1")

    assert "warning" not in data
    # One metrics query, for the scoped jobs only.
    assert len(metrics_calls) == 1
    assert sorted(metrics_calls[0]) == [(NUM, REF), ("31990002", "26-00002")]

    rows = data["jobs_timeline"]
    assert [r["job_id"] for r in rows] == [NUM, "31990002"]
    assert data["jobs_timeline_total"] == 2

    r = rows[0]
    assert _parse(r["added_to_curate_at"]) == TRUTH
    assert r["posted_by"] == "poster@x.com"
    assert r["launched_by"] == "launcher@x.com"
    assert r["recruiter_emails"] == ["lead@x.com"]
    assert r["pass_candidates"] == 1          # c1, once across both keys
    assert r["fail_candidates"] == 2          # c2 (hard-filter fail), c3 (failed with a score)
    assert r["feedback_total"] == 3           # c1 c2 c3
    assert r["feedback_submits"] == 2
    assert r["feedback_rejects"] == 1
    assert r["feedback_unreachable"] == 0
    assert r["pair_internal_submits"] == 1    # c2
    assert r["pair_external_submits"] == 1    # c1: no submission_type = legacy external
    assert r["jobdiva_confirmed_subs"] == 1   # monitored_jobs.pair_external_subs
    assert r["jobdiva_submittals"] == 4       # monitored_jobs.jobdiva_total_subs
    assert _parse(r["first_pair_external_submit_at"]) == datetime.datetime(2026, 9, 21, 15, tzinfo=UTC)
    assert _parse(r["first_feedback_at"]) == datetime.datetime(2026, 9, 21, 12, tzinfo=UTC)

    r2 = rows[1]
    assert r2["posted_by"] is None and r2["launched_by"] is None
    assert r2["recruiter_emails"] == ["member@x.com"]
    assert r2["pass_candidates"] == 1
    assert r2["feedback_total"] == 0
    assert r2["first_pair_external_submit_at"] is None

    sm = data["submission_metrics"]
    # c5's internal submit is on the other team's job and must not count.
    assert sm["pair_internal_submits"] == 1
    assert sm["pair_external_submits"] == 1
    assert sm["pair_external_subs"] == 1
    assert sm["pair_submits"] == 2


def test_system_view_sums_the_split_over_every_job(analytics):
    run, metrics_calls = analytics
    data = run(None)

    assert len(metrics_calls) == 1
    assert len(metrics_calls[0]) == 3
    assert [r["job_id"] for r in data["jobs_timeline"]] == [NUM, "31990002", "31990003"]
    sm = data["submission_metrics"]
    assert sm["pair_internal_submits"] == 2   # c2 + c5
    assert sm["pair_external_submits"] == 1


# ---------------------------------------------------------------------------
# Degraded modes: missing attribution columns, metrics over their time budget
# ---------------------------------------------------------------------------


def test_timeline_renders_when_attribution_columns_are_missing(pg):
    """The startup ALTER that adds pair_posted_by / pair_launched_by can be
    cancelled behind a long lock; the timeline must still load, with None."""
    assert aa._missing_optional_columns(pg) == frozenset()
    _add_job(pg, "j1", created_at="2026-09-21 13:46:55", pair_posted_by="p@x.com")
    with pg.cursor() as cur:
        cur.execute("ALTER TABLE monitored_jobs DROP COLUMN pair_posted_by, DROP COLUMN pair_launched_by")
    assert aa._missing_optional_columns(pg) == frozenset({"pair_posted_by", "pair_launched_by"})

    rows = aa._compute_jobs_timeline(pg, None, {})["rows"]

    assert [r["job_id"] for r in rows] == ["j1"]
    assert rows[0]["posted_by"] is None and rows[0]["launched_by"] is None
    assert _parse(rows[0]["added_to_curate_at"]) == TRUTH


def _statement_timeout(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        return cur.fetchone()[0]


def test_metrics_run_under_their_own_timeout_and_restore_it(pg, monkeypatch):
    _seed(pg)
    before = _statement_timeout(pg)
    seen = {}
    real_fetch = job_candidate_metrics.fetch_job_candidate_metrics

    def spy(conn, jobs):
        seen["during"] = _statement_timeout(conn)
        return real_fetch(conn, jobs)

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", spy)
    monkeypatch.setattr(aa, "_METRICS_STATEMENT_TIMEOUT_MS", 7000)

    metrics = aa._compute_scoped_job_metrics(pg, None)

    assert seen["during"] == "7s"
    assert _statement_timeout(pg) == before
    assert metrics[NUM]["passed"] == 1


class _SavepointConn(_SharedConn):
    """Like _SharedConn, but rollback returns to a savepoint: a real rollback
    would also drop the ON COMMIT DROP tables the later sections read."""

    def __init__(self, conn):
        super().__init__(conn)
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT analytics_section")

    def rollback(self):
        with self._conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT analytics_section")


def test_metrics_timeout_degrades_to_unavailable_not_zeros(pg, monkeypatch, caplog):
    """A metrics read over its budget is cancelled by Postgres; the rest of the
    page still loads, the candidate columns are None and the page is told."""
    _seed(pg)
    before = _statement_timeout(pg)
    real_fetch = job_candidate_metrics.fetch_job_candidate_metrics

    def slow(conn, jobs):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_sleep(0.5)")
        return real_fetch(conn, jobs)

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", slow)
    monkeypatch.setattr(aa, "_METRICS_STATEMENT_TIMEOUT_MS", 50)
    monkeypatch.setattr(aa, "get_db_connection", lambda: _SavepointConn(pg))

    with caplog.at_level(logging.WARNING, logger=aa.logger.name):
        data = aa._compute_analytics_sync(None)

    assert "statement timeout" in caplog.text
    assert "warning" not in data
    assert data["jobs_timeline_metrics_available"] is False
    rows = {r["job_id"]: r for r in data["jobs_timeline"]}
    assert set(rows) == {NUM, "31990002", "31990003"}
    r = rows[NUM]
    assert r["pass_candidates"] is None and r["feedback_total"] is None
    assert r["pair_internal_submits"] is None and r["first_feedback_at"] is None
    # Job-level columns do not depend on the metrics.
    assert r["jobdiva_confirmed_subs"] == 1 and r["posted_by"] == "poster@x.com"
    assert data["submission_metrics"]["pair_internal_submits"] is None
    assert data["submission_metrics"]["pair_submits"] == 2
    assert data["launch_speed"]["launched_jobs"] == 1
    # The short budget did not leak into the sections that ran after it.
    assert _statement_timeout(pg) == before


# ---------------------------------------------------------------------------
# Step 5 time (services/job_step_time) merged into the timeline
# ---------------------------------------------------------------------------


def _utc_at(hour, minute=0):
    return datetime.datetime(2026, 9, 22, hour, minute, tzinfo=UTC)


def _time_step5(conn, job_id, email, *, minutes, entered):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_step_time (job_id, step, user_email, active_ms, first_entered_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (job_id, job_step_time.STEP_SOURCE, email, int(minutes * 60_000), entered),
        )


def _audit(conn, key, interview_id, created, candidate_id="c9"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (candidate_id, key, interview_id, created.replace(tzinfo=None)),
        )


def _seed_step_time(pg):
    # The launched job: two recruiters, 40 + 20 active minutes, first entry
    # 08:00. pair_launched_at (the first launch click, see _seed) is 10:00,
    # but that attempt reached no candidate; the first SUCCESSFUL launch is
    # 10:30, so Step 5 → Launch is 150 minutes, not 120.
    _time_step5(pg, NUM, "lead@x.com", minutes=40, entered=_utc_at(8))
    _time_step5(pg, NUM, "member@x.com", minutes=20, entered=_utc_at(9))
    _audit(pg, REF, "", _utc_at(10))
    _audit(pg, REF, "iv-1", _utc_at(10, 30))
    # 31990002: nobody timed it (worked before the tracking shipped).
    # Out of scope: timed, never launched.
    _time_step5(pg, "31990003", "outsider@y.com", minutes=5, entered=_utc_at(7))


@pytest.fixture()
def step_calls(monkeypatch):
    """Which jobs reach the Step 5 read."""
    calls = []
    real_fetch = job_step_time.fetch_step_metrics

    def spy(conn, jobs, step=job_step_time.STEP_SOURCE):
        jobs = list(jobs)
        calls.append((jobs, step))
        return real_fetch(conn, jobs, step)

    monkeypatch.setattr(aa, "fetch_step_metrics", spy)
    return calls


def test_team_view_merges_step5_time_for_scoped_jobs_only(analytics, step_calls, pg):
    run, _ = analytics
    _seed_step_time(pg)
    data = run("t1")

    assert data["jobs_timeline_step_time_available"] is True
    assert len(step_calls) == 1
    jobs, step = step_calls[0]
    assert sorted(jobs) == [(NUM, REF), ("31990002", "26-00002")]
    assert step == 5

    rows = {r["job_id"]: r for r in data["jobs_timeline"]}
    assert set(rows) == {NUM, "31990002"}
    assert rows[NUM]["step5_active_minutes"] == 60.0
    assert rows[NUM]["step5_to_launch_minutes"] == 150.0
    # The first launch CLICK is earlier than the first successful launch.
    assert _parse(rows[NUM]["curate_launched_at"]) == _utc_at(10)
    assert rows["31990002"]["step5_active_minutes"] is None
    assert rows["31990002"]["step5_to_launch_minutes"] is None


def test_system_view_step5_time_covers_every_job(analytics, step_calls, pg):
    run, _ = analytics
    _seed_step_time(pg)
    data = run(None)

    assert len(step_calls) == 1 and len(step_calls[0][0]) == 3
    rows = {r["job_id"]: r for r in data["jobs_timeline"]}
    assert rows["31990003"]["step5_active_minutes"] == 5.0
    assert rows["31990003"]["step5_to_launch_minutes"] is None  # never launched
    assert rows[NUM]["step5_to_launch_minutes"] == 150.0


def test_step_time_runs_under_its_own_timeout_and_restores_it(pg, monkeypatch):
    _seed(pg)
    _seed_step_time(pg)
    before = _statement_timeout(pg)
    seen = {}
    real_fetch = job_step_time.fetch_step_metrics

    def spy(conn, jobs, step):
        seen["during"] = _statement_timeout(conn)
        return real_fetch(conn, jobs, step)

    monkeypatch.setattr(aa, "fetch_step_metrics", spy)
    monkeypatch.setattr(aa, "_STEP_TIME_STATEMENT_TIMEOUT_MS", 6000)

    step_metrics = aa._compute_scoped_step_time(pg, None)

    assert seen["during"] == "6s"
    assert _statement_timeout(pg) == before
    assert step_metrics[NUM]["active_minutes"] == 60.0


def test_missing_step_time_table_blanks_only_the_step5_columns(pg, monkeypatch, caplog):
    """job_step_time is created by startup schema init, which can be cancelled
    behind a long lock. The rest of the page still loads, the Step 5 columns
    are None and the page is told they were unavailable (not untracked)."""
    _seed(pg)
    with pg.cursor() as cur:
        cur.execute("DROP TABLE job_step_time")
        # Dropping the temp table would unmask a permanent public.job_step_time
        # on any database the API has started against (startup schema init
        # creates it), and the read would then succeed. Every table the page
        # reads is a temp table here, so search only pg_temp (pg_catalog is
        # still searched implicitly). SET LOCAL sits before the savepoints, so
        # the section rollbacks keep it; the fixture's rollback clears it.
        cur.execute("SET LOCAL search_path = pg_temp")
        cur.execute("SELECT to_regclass('job_step_time')")
        assert cur.fetchone()[0] is None, "job_step_time must be unresolvable"
    before = _statement_timeout(pg)
    monkeypatch.setattr(aa, "get_db_connection", lambda: _SavepointConn(pg))

    with caplog.at_level(logging.WARNING, logger=aa.logger.name):
        data = aa._compute_analytics_sync(None)

    assert "_compute_scoped_step_time unavailable" in caplog.text
    assert "warning" not in data
    assert data["jobs_timeline_step_time_available"] is False
    assert data["jobs_timeline_metrics_available"] is True
    rows = {r["job_id"]: r for r in data["jobs_timeline"]}
    assert set(rows) == {NUM, "31990002", "31990003"}
    for r in rows.values():
        assert r["step5_active_minutes"] is None and r["step5_to_launch_minutes"] is None
    # The candidate columns and the sections after it are unaffected.
    assert rows[NUM]["pass_candidates"] == 1
    assert data["submission_metrics"]["pair_internal_submits"] == 2
    assert data["launch_speed"]["launched_jobs"] == 1
    assert _statement_timeout(pg) == before
