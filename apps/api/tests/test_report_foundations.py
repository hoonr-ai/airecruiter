"""Shared building blocks of the report columns (2026-09-23).

  * engage_display_sql — the SQL twin of the rank list's format_engage_status;
    drift-tested row by row against the Python rules on a real Postgres.
  * feedback_metrics split predicates — Python mirrors vs SQL fragments.
  * job_candidate_metrics — per-job pass/feedback/submit counts in one query.
  * _helpers._ts_utc — the "Added on PAIR is +5:30" read-side fix.
  * job_attribution — who posted / launched a job, and the routers/jobs.py
    writers of those columns (external create, version clone).

The Postgres tests skip when no server is reachable (set LAUNCH_REPORT_TEST_DSN,
e.g. to a pgserver instance). The session is pinned to UTC like managed PROD.
"""

import datetime
import itertools
import json
import os

import pytest

from routers._helpers import _ts, _ts_utc, optional_monitored_jobs_columns
from services import feedback_metrics as fm
from services import job_attribution
from services.engage_status import (
    COMPLETED_STATUSES,
    FAIL_STATUSES,
    HF_PASS_VALUES,
    IN_PROGRESS_STATUSES,
    PASS_STATUSES,
    engage_display_sql,
    format_engage_status,
    parse_engage_score,
)
from services.job_candidate_metrics import (
    JOB_CANDIDATE_METRICS_SQL,
    METRIC_COLUMNS,
    fetch_job_candidate_metrics,
)

UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Static checks (no database)
# ---------------------------------------------------------------------------


def test_sql_fragments_are_safe_inside_parameterised_statements():
    # A literal '%' in a fragment breaks psycopg2 formatting of every statement
    # it is spliced into.
    fragments = [
        engage_display_sql("sc.data"),
        _ts_utc("mj.created_at"),
        fm.IS_INTERNAL_SUBMIT_SQL.format(alias="sc."),
        fm.IS_EXTERNAL_SUBMIT_SQL.format(alias="sc."),
        fm.IS_REJECT_SQL.format(alias="sc."),
        fm.IS_UNREACHABLE_SQL.format(alias="sc."),
    ]
    for fragment in fragments:
        assert "%" not in fragment, fragment
    assert JOB_CANDIDATE_METRICS_SQL.count("%s") == 2
    assert "%" not in JOB_CANDIDATE_METRICS_SQL.replace("%s", "")


def test_engage_display_sql_lists_every_status_token():
    sql = engage_display_sql("d")
    for token in PASS_STATUSES + FAIL_STATUSES + IN_PROGRESS_STATUSES + COMPLETED_STATUSES:
        assert f"'{token}'" in sql
    for token in HF_PASS_VALUES:
        assert f"'{token}'" in sql


@pytest.mark.parametrize(
    "data,kind",
    [
        ({"feedback_type": "Submit"}, "external"),  # pre-09-11 submit: no type
        ({"feedback_type": "Submit", "submission_type": "external"}, "external"),
        ({"feedback_type": "submit", "submission_type": "Internal"}, "internal"),
        # a later Reject leaves submission_type behind — it is not a submit
        ({"feedback_type": "Reject", "submission_type": "internal"}, None),
        ({"feedback_type": "Unreachable", "submission_type": "external"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_submission_kind(data, kind):
    assert fm.submission_kind(data) == kind


@pytest.mark.parametrize(
    "data,label",
    [
        ({"feedback_type": "Submit"}, "Submit"),
        ({"feedback_type": " reject "}, "Reject"),
        ({"feedback_type": "Rejected"}, "Reject"),
        ({"feedback_type": "Unreachable"}, "Unreachable"),
        ({"feedback_type": ""}, None),
        ({"feedback_type": "Maybe"}, "Maybe"),
        (None, None),
    ],
)
def test_feedback_label(data, label):
    assert fm.feedback_label(data) == label


def test_normalize_actor_email():
    assert job_attribution.normalize_actor_email(" Jane.Doe@Pyramid.com ") == "jane.doe@pyramid.com"
    assert job_attribution.normalize_actor_email("") is None
    assert job_attribution.normalize_actor_email(None) is None
    assert job_attribution.normalize_actor_email("unauthenticated@hoonr.ai") is None
    assert job_attribution.attribution_fields("A@x.com", None) == {"posted_by": "a@x.com", "launched_by": None}


def test_stamp_job_posted_by_never_raises(monkeypatch):
    import core.db

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "get_db_connection", boom)
    job_attribution.stamp_job_posted_by("26-1", "a@x.com")  # logged, not raised
    job_attribution.stamp_job_posted_by("", "a@x.com")
    job_attribution.stamp_job_posted_by("26-1", None)


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_SCHEMA_SQL = """
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT, email TEXT, phone TEXT, data JSONB, status TEXT DEFAULT 'sourced',
    resume_match_percentage INT DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY, jobdiva_id TEXT, created_at TEXT, created_ts TIMESTAMP,
    pair_launched_at TIMESTAMP, pair_launched_by TEXT, pair_posted_by TEXT, updated_at TIMESTAMP
) ON COMMIT DROP;
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
            cur.execute(_SCHEMA_SQL)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _add(conn, key, cid, data, source="LinkedIn"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES (%s, %s, %s, %s)",
            (key, cid, source, json.dumps(data)),
        )


_STATUS_SAMPLES = (
    list(PASS_STATUSES + FAIL_STATUSES + IN_PROGRESS_STATUSES + COMPLETED_STATUSES)
    + ["", "sent", "Initiated", "pending", "phase2", "active", " PASSED ", "Completed", None]
)
_SCORE_SAMPLES = [None, "", "85", 85, 72.5, "abc", " 90 ", "1e2", 0]
_HF_SAMPLES = [None, "", "pass", "Passed", "fail", "not_hard_filter", " FAIL "]


def test_engage_display_sql_matches_python_rules(pg):
    """Every (status, score, hard-filter) combination classifies identically."""
    cases = list(itertools.product(_STATUS_SAMPLES, _SCORE_SAMPLES, _HF_SAMPLES))
    blobs = []
    for i, (status, score, hf) in enumerate(cases):
        blob = {}
        if status is not None:
            blob["engage_status"] = status
        if score is not None:
            blob["engage_score"] = score
        if hf is not None:
            blob["engage_hard_filter_status"] = hf
        blobs.append(blob)
    with pg.cursor() as cur:
        cur.execute(
            f"SELECT i, {engage_display_sql('d.data')} FROM unnest(%s::int[], %s::jsonb[]) AS d(i, data)",
            (list(range(len(blobs))), [json.dumps(b) for b in blobs]),
        )
        got = dict(cur.fetchall())
    mismatches = []
    for i, (status, score, hf) in enumerate(cases):
        expected = format_engage_status(
            (status or "").strip().lower(),
            parse_engage_score(score),
            (hf or "").strip().lower(),
        )
        if got[i] != expected:
            mismatches.append((status, score, hf, got[i], expected))
    assert not mismatches, mismatches[:10]


def test_legacy_hard_filter_key_is_honoured(pg):
    with pg.cursor() as cur:
        cur.execute(
            f"WITH d(data) AS (SELECT %s::jsonb) SELECT {engage_display_sql('d.data')} FROM d",
            (json.dumps({"engage_status": "completed", "hard_filter_status": "fail"}),),
        )
        assert cur.fetchone()[0] == "Fail"


def test_job_candidate_metrics_counts(pg):
    ref, num = "26-29267", "31990001"
    other_ref, other_num = "26-00002", "31990002"
    t = lambda h: f"2026-09-21T{h:02d}:00:00+00:00"  # noqa: E731
    # Stored under the ref …
    _add(pg, ref, "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": t(15)})
    # … and the same person again under the numeric key: counted once.
    _add(pg, num, "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": t(15)}, source="JobDiva")
    _add(pg, ref, "c2", {"engage_status": "completed", "engage_hard_filter_status": "fail",
                         "feedback_type": "Submit", "submission_type": "internal", "feedback_at": t(14)})
    _add(pg, ref, "c3", {"engage_status": "failed", "engage_score": 40,
                         "feedback_type": "Reject", "submission_type": "external", "feedback_at": t(13)})
    _add(pg, num, "c4", {"engage_status": "in_progress", "feedback_type": "Unreachable", "feedback_at": t(12)})
    _add(pg, ref, "c5", {"engage_status": "failed"})  # no score: outreach miss → Pending
    _add(pg, ref, "c6", {"feedback_type": "Submit", "submission_type": "external", "feedback_at": "garbage"})
    _add(pg, other_ref, "c9", {"engage_status": "passed"})
    _add(pg, other_ref, "c10", {"engage_status": "failed", "engage_score": "12"})
    _add(pg, other_ref, "c11", {"engage_status": "passed", "feedback_type": "Unreachable"})

    got = fetch_job_candidate_metrics(pg, [(num, ref), (other_num, other_ref), ("31990003", None)])

    m = got[num]
    assert m["passed"] == 1           # c1 once, across both keys
    assert m["failed"] == 2           # c2 (completed + failed HF), c3 (failed with score)
    assert m["in_progress"] == 1      # c4
    assert m["feedback_total"] == 5   # c1 c2 c3 c4 c6
    assert m["pair_submits"] == 3     # c1 c2 c6
    assert m["pair_internal_submits"] == 1  # c2
    assert m["pair_external_submits"] == 2  # c1 (no type = external), c6
    assert m["rejects"] == 1
    assert m["unreachable"] == 1
    # decided interviews with no recruiter decision: c5 is Pending, so none —
    # every Pass/Fail above already has feedback.
    assert m["awaiting_feedback"] == 0
    # c6's unparseable feedback_at is ignored rather than failing the query.
    assert m["first_external_submit_at"] == datetime.datetime(2026, 9, 21, 15, tzinfo=UTC)
    assert m["first_internal_submit_at"] == datetime.datetime(2026, 9, 21, 14, tzinfo=UTC)
    assert m["first_feedback_at"] == datetime.datetime(2026, 9, 21, 12, tzinfo=UTC)

    assert got[other_num]["passed"] == 2
    assert got[other_num]["failed"] == 1
    assert got[other_num]["feedback_total"] == 1
    # c9 and c10 are decided with no feedback; c11's Unreachable is feedback.
    assert got[other_num]["awaiting_feedback"] == 2
    assert got["31990003"] == {c: (None if c.endswith("_at") else 0) for c in METRIC_COLUMNS}


def test_job_candidate_metrics_count_people_not_stored_rows(pg):
    """A person stored under both keys of a job is one person: their decision
    on one row is not undone by the other row having none."""
    ref, num = "26-29288", "31990003"
    t = lambda h: f"2026-09-22T{h:02d}:00:00+00:00"  # noqa: E731
    # Every pair is inserted so that row-id order DISAGREES with the rule under
    # test — ordering by id alone would give the wrong answer.
    # Submitted on the ref row; the numeric-key copy carries no decision.
    _add(pg, num, "g1", {"engage_status": "passed"}, source="JobDiva")
    _add(pg, ref, "g1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": t(15)})
    # A newer Reject (inserted first) beats an older Submit: newest by feedback_at.
    _add(pg, num, "g2", {"engage_status": "failed", "engage_score": 30,
                         "feedback_type": "Reject", "feedback_at": t(12)}, source="JobDiva")
    _add(pg, ref, "g2", {"engage_status": "failed", "engage_score": 30,
                         "feedback_type": "Submit", "feedback_at": t(10)})
    # Pass on the first row, a stale Pending copy after it: still one Pass.
    _add(pg, num, "g3", {"engage_status": "passed"}, source="JobDiva")
    _add(pg, ref, "g3", {"engage_status": "sent"})
    # Fail then Pass: the furthest-along outcome wins, whichever row is newer.
    _add(pg, ref, "g4", {"engage_status": "passed"})
    _add(pg, num, "g4", {"engage_status": "failed", "engage_score": 20}, source="JobDiva")

    m = fetch_job_candidate_metrics(pg, [(num, ref)])[num]
    assert m["passed"] == 3 and m["failed"] == 1  # g1 g3 g4 pass; g2 fails
    assert m["feedback_total"] == 2
    assert m["pair_submits"] == 1 and m["pair_external_submits"] == 1
    assert m["rejects"] == 1
    assert m["awaiting_feedback"] == 2  # g3 and g4 — g1 was submitted
    assert m["first_external_submit_at"] == datetime.datetime(2026, 9, 22, 15, tzinfo=UTC)
    # First feedback is the earliest decision ever recorded (g2's 10:00 Submit).
    assert m["first_feedback_at"] == datetime.datetime(2026, 9, 22, 10, tzinfo=UTC)


def test_job_candidate_metrics_empty_input_skips_the_query(pg):
    assert fetch_job_candidate_metrics(pg, []) == {}


def test_ts_utc_honours_the_ist_suffix(pg):
    """The +5:30 bug: 09:46:55 EDT stored as India wall-clock text."""
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO monitored_jobs (job_id, created_at, created_ts) VALUES "
            "('ist', '2026-09-21 19:16:55 IST', NULL), "
            "('utc_text', '2026-09-21 13:46:55', NULL), "
            "('utc_frac', '2026-09-21 13:46:55.123456', NULL), "
            "('blank', '', NULL), "
            "('ts', NULL, '2026-09-21 13:46:55')"
        )
        cur.execute(
            f"SELECT job_id, {_ts_utc('created_at')}, {_ts_utc('created_ts')} FROM monitored_jobs ORDER BY job_id"
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute(f"SELECT ({_ts('created_at')}) AT TIME ZONE 'UTC' FROM monitored_jobs WHERE job_id = 'ist'")
        old_reading = cur.fetchone()[0]
    truth = datetime.datetime(2026, 9, 21, 13, 46, 55, tzinfo=UTC)
    assert rows["ist"][0] == truth
    assert rows["utc_text"][0] == truth
    assert rows["utc_frac"][0] == truth
    assert rows["blank"][0] is None
    assert rows["ts"][1] == truth
    # The reading Admin Analytics used before the fix was exactly 5h30m late.
    assert old_reading - truth == datetime.timedelta(hours=5, minutes=30)


class _PoolConn:
    """Stands in for a pool connection on the test's single session (the temp
    tables live there). commit() is a no-op so they survive the assertions;
    close() undoes only this borrower's work when it failed, the way a pool
    discards an aborted connection, so the session stays usable."""

    def __init__(self, conn):
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT pool_conn")

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass

    def close(self):
        failed = self._conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR
        with self._conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT pool_conn" if failed else "RELEASE SAVEPOINT pool_conn")


@pytest.fixture()
def pool(pg, monkeypatch):
    import core.db

    monkeypatch.setattr(core.db, "get_db_connection", lambda: _PoolConn(pg))
    return pg


def test_stamp_job_posted_by_first_writer_wins(pool):
    with pool.cursor() as cur:
        cur.execute(
            "INSERT INTO monitored_jobs (job_id, jobdiva_id, created_at) VALUES "
            "('31990001', '26-29267', '2026-09-24 10:00:00 IST'), ('-4', 'EXT-4', '2026-09-24 04:30:00')"
        )
    job_attribution.stamp_job_posted_by("26-29267", "Poster@X.com")
    job_attribution.stamp_job_posted_by("31990001", "someone.else@x.com")
    job_attribution.stamp_job_posted_by("-4", "ext@x.com")
    with pool.cursor() as cur:
        cur.execute("SELECT job_id, pair_posted_by FROM monitored_jobs ORDER BY job_id")
        assert cur.fetchall() == [("-4", "ext@x.com"), ("31990001", "poster@x.com")]


def test_optional_monitored_jobs_columns_selects_null_for_a_missing_column(pg):
    with pg.cursor() as cur:
        assert optional_monitored_jobs_columns(cur) == {
            "pair_posted_by": "mj.pair_posted_by",
            "pair_launched_by": "mj.pair_launched_by",
        }
        cur.execute("ALTER TABLE monitored_jobs DROP COLUMN pair_launched_by")
        # A dropped column is still in pg_attribute (attisdropped) — excluded.
        assert optional_monitored_jobs_columns(cur, "") == {
            "pair_posted_by": "pair_posted_by",
            "pair_launched_by": "NULL::text",
        }


def test_schema_marks_existing_jobs_not_recorded_and_new_jobs_eligible(pg):
    """Jobs that exist when pair_posted_by arrives read '' (never stamped);
    jobs created later start NULL. pair_launched_by needs no marker — its
    stamp freezes on a successful launch instead — so it is plain NULL."""
    with pg.cursor() as cur:
        cur.execute("ALTER TABLE monitored_jobs DROP COLUMN pair_posted_by, DROP COLUMN pair_launched_by")
        cur.execute("INSERT INTO monitored_jobs (job_id) VALUES ('old')")
        for stmt in job_attribution.SCHEMA_STATEMENTS * 2:  # re-running is a no-op
            cur.execute(stmt)
        cur.execute("INSERT INTO monitored_jobs (job_id) VALUES ('new')")
        cur.execute("SELECT job_id, pair_posted_by, pair_launched_by FROM monitored_jobs ORDER BY job_id")
        assert cur.fetchall() == [("new", None, None), ("old", "", None)]
        # The default is gone, so no later insert can be born "not recorded".
        cur.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'monitored_jobs' AND column_name = 'pair_posted_by'"
        )
        assert cur.fetchone()[0] is None


def test_posted_by_backfill_and_default_drop_are_one_statement():
    """If the backfill (ADD ... DEFAULT '') committed but the DROP DEFAULT then
    lost a lock race, every new job would be born '' and never stamped. The
    pair must travel as one statement, i.e. one transaction."""
    posted = [s for s in job_attribution.SCHEMA_STATEMENTS if "pair_posted_by" in s]
    assert len(posted) == 1
    assert "DEFAULT ''" in posted[0] and "DROP DEFAULT" in posted[0]


def test_stamp_job_posted_by_never_credits_a_job_that_predates_the_column(pool):
    """An old job's first PAIR save happened before anyone recorded it: whoever
    saves it next is not its poster."""
    with pool.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs (job_id, pair_posted_by) VALUES ('old', ''), ('new', NULL)")
    job_attribution.stamp_job_posted_by("old", "bob@x.com")
    job_attribution.stamp_job_posted_by("new", "bob@x.com")
    with pool.cursor() as cur:
        cur.execute("SELECT job_id, pair_posted_by FROM monitored_jobs ORDER BY job_id")
        assert cur.fetchall() == [("new", "bob@x.com"), ("old", "")]
    # The reports render the '' marker as "not recorded".
    assert job_attribution.attribution_fields("", "") == {"posted_by": None, "launched_by": None}


def _audit(conn, key, interview_id, candidate_id="c1"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status) VALUES (%s, %s, %s, %s)",
            (candidate_id, key, interview_id, "sent" if interview_id else "failed"),
        )


def _launch(conn, key, email, *, succeeds=True, audit_key=None):
    """The /candidates/save order: attribution first, then the pre-existing
    launch-time stamp — then pair-bot's outcome lands in the audit table."""
    job_attribution.stamp_job_launched_by(key, email)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET pair_launched_at = COALESCE(pair_launched_at, NOW()), updated_at = NOW() "
            "WHERE job_id = %s OR jobdiva_id = %s",
            (key, key),
        )
    _audit(conn, audit_key or key, f"iv-{email}" if succeeds else "")


def test_stamp_job_launched_by_credits_the_first_successful_launch(pool):
    with pool.cursor() as cur:
        cur.execute(
            "INSERT INTO monitored_jobs (job_id, jobdiva_id, pair_launched_by) VALUES "
            "('1', '26-1', NULL), ('2', '26-2', NULL), ('3', '26-3', NULL), ('4', '26-4', NULL), "
            "('5', '26-5', NULL), ('6', '', NULL)"
        )
    # Job 5 was launched before attribution existed.
    _audit(pool, "26-5", "iv-historical")
    # An audit row written with an empty job key must not count as job 6's launch.
    _audit(pool, "", "iv-unkeyed")
    # Job 1: first attempt succeeds; a later re-launch is not the launcher.
    _launch(pool, "26-1", "First@X.com")
    _launch(pool, "26-1", "second@x.com")
    # Job 2: A's attempt fails, B's retry succeeds, C re-launches later → B.
    _launch(pool, "26-2", "a@x.com", succeeds=False)
    _launch(pool, "26-2", "b@x.com", audit_key="2")  # audit keyed on the numeric id
    _launch(pool, "26-2", "c@x.com")
    # Job 3 existed before the column but had never launched: its first launch
    # after the deploy is credited.
    _launch(pool, "26-3", "late@x.com")
    # Job 4: no identity leaves the column alone.
    _launch(pool, "26-4", None)
    _launch(pool, "26-4", "unauthenticated@hoonr.ai")
    # Job 5 was already launched: a re-launcher is not its launcher.
    _launch(pool, "26-5", "relauncher@x.com")
    # Job 6 has no JobDiva ref; its launch audits under the numeric id.
    _launch(pool, "6", "noref@x.com")
    with pool.cursor() as cur:
        cur.execute("SELECT job_id, pair_launched_by, pair_launched_at IS NOT NULL FROM monitored_jobs ORDER BY job_id")
        assert cur.fetchall() == [
            ("1", "first@x.com", True),
            ("2", "b@x.com", True),
            ("3", "late@x.com", True),
            ("4", None, True),
            ("5", None, True),
            ("6", "noref@x.com", True),
        ]


def test_launch_time_is_stamped_when_the_attribution_column_is_missing(pool):
    """A boot whose ALTER never added pair_launched_by costs the attribution
    only — never the pair_launched_at baseline (Time to First Pass etc.)."""
    with pool.cursor() as cur:
        cur.execute("ALTER TABLE monitored_jobs DROP COLUMN pair_launched_by")
        cur.execute("INSERT INTO monitored_jobs (job_id, jobdiva_id) VALUES ('1', '26-1')")
    _launch(pool, "26-1", "first@x.com")  # logged, not raised
    with pool.cursor() as cur:
        cur.execute("SELECT pair_launched_at IS NOT NULL FROM monitored_jobs WHERE job_id = '1'")
        assert cur.fetchone()[0] is True


def test_stamp_job_launched_by_never_raises(monkeypatch):
    import core.db

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "get_db_connection", boom)
    job_attribution.stamp_job_launched_by("26-1", "a@x.com")  # logged, not raised
    job_attribution.stamp_job_launched_by("", "a@x.com")
    job_attribution.stamp_job_launched_by("26-1", None)


# ---------------------------------------------------------------------------
# routers/jobs.py writers
# ---------------------------------------------------------------------------


def _external_job_table(conn, *, with_posted_by):
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE monitored_jobs ADD COLUMN title TEXT, ADD COLUMN enhanced_title TEXT, "
            "ADD COLUMN ai_description TEXT, ADD COLUMN recruiter_notes TEXT, ADD COLUMN customer_name TEXT, "
            "ADD COLUMN processing_status TEXT, ADD COLUMN current_step INT, ADD COLUMN recruiter_emails TEXT"
        )
        if not with_posted_by:
            cur.execute("ALTER TABLE monitored_jobs DROP COLUMN pair_posted_by")


def _create_external_job(pool, monkeypatch, email="Maker@X.com"):
    import asyncio

    from core.auth import UserIdentity
    from models import ExternalJobCreateRequest
    from routers import jobs

    monkeypatch.setattr(jobs, "get_db_connection", lambda: _PoolConn(pool))
    return asyncio.run(jobs.create_external_job(
        ExternalJobCreateRequest(title="Data Engineer"), UserIdentity(email=email, role="recruiter"),
    ))


def test_external_job_create_records_the_poster(pool, monkeypatch):
    _external_job_table(pool, with_posted_by=True)
    out = _create_external_job(pool, monkeypatch)
    assert out["status"] == "success" and out["job_id"] == "-1"
    with pool.cursor() as cur:
        cur.execute("SELECT jobdiva_id, pair_posted_by FROM monitored_jobs WHERE job_id = '-1'")
        assert cur.fetchone() == ("EXT-1", "maker@x.com")


def test_external_job_create_survives_a_missing_attribution_column(pool, monkeypatch):
    """Was a 500 ('column "pair_posted_by" ... does not exist') on a boot whose
    startup ALTER was cancelled."""
    _external_job_table(pool, with_posted_by=False)
    out = _create_external_job(pool, monkeypatch)
    assert out["status"] == "success"
    with pool.cursor() as cur:
        cur.execute("SELECT jobdiva_id, title FROM monitored_jobs WHERE job_id = '-1'")
        assert cur.fetchone() == ("EXT-1", "Data Engineer")


def test_job_version_clone_resets_attribution(monkeypatch):
    """A -vN clone is a fresh, unlaunched draft: v1's launcher must not show as
    its 'Launched By', and its own first save records its poster."""
    from unittest.mock import MagicMock

    from routers import jobs

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = [("31990001", "26-1", None), (1,)]
    cur.fetchall.return_value = [
        (name, None)
        for name in ("job_id", "jobdiva_id", "title", "pair_launched_at", "pair_launched_by",
                     "pair_posted_by", "recruiter_emails", "created_at")
    ]
    monkeypatch.setattr(jobs, "get_db_connection", lambda: conn)
    monkeypatch.setattr(jobs, "JobRubricDB", MagicMock())
    out = jobs._create_job_version_sync("26-1")
    assert out["new_job_id"] == "26-1-v2"

    sql, params = cur.execute.call_args_list[-1].args
    cols = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    exprs = sql.split(" SELECT ", 1)[1].split(" FROM ", 1)[0].split(", ")
    values = iter(params)
    got = {col: (next(values) if expr == "%s" else expr) for col, expr in zip(cols, exprs)}
    assert got["pair_launched_by"] is None
    assert got["pair_posted_by"] is None
    assert got["pair_launched_at"] is None
    assert got["title"] == "title"  # everything else is copied from v1
    assert got["recruiter_emails"] == "recruiter_emails"
