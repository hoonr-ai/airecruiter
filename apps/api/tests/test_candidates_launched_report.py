"""GET /candidates/launched — the admin Candidates report ("Master Candidate Pool").

Pinned here (2026-09-23):

  * The audit row is JOB-SCOPED. It used to be the newest audit row per
    candidate across every job, so a person launched on jobs A and B showed
    B's Pass/Fail, interview and launch date on A's row, and was listed under
    A even when never launched there. It is the newest SUCCESSFUL one (with an
    interview id), the Launch Report / Rankings rule, so a failed re-launch
    no longer hides an earlier launch.
  * One row per (job, person), also for a person stored under both of the
    job's keys, and that row is the one carrying the recruiter's decision;
    No Feedback looks at both keys' rows.
  * The status filter classifies with the rank list's Pass rule
    (engage_display_sql), not the raw audit status — "pass" used to return a
    completed interview that failed its hard filter, which displays Fail.
  * Candidate Feedback fields and the Submittal Status label, joined to
    JobDiva's own submittal feed after pagination in one statement.
  * The Rejected feedback filter no longer 500s (a bare LIKE 'reject%'
    inside a parameterised statement made psycopg2 raise IndexError).

The Postgres tests drive the real endpoint against TEMP tables and skip when
no server is reachable (set LAUNCH_REPORT_TEST_DSN, e.g. to a pgserver
instance). The session is pinned to UTC like managed PROD.
"""

import asyncio
import json
import os

import pytest

from core.auth import UserIdentity
from routers import candidates as cr

ADMIN = UserIdentity(email="admin@x.com", role="admin")

# ---------------------------------------------------------------------------
# Pure helpers (no database)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,count,label",
    [
        ({"feedback_type": "Submit", "submission_type": "external"}, 1, "Submitted – External (JobDiva confirmed)"),
        ({"feedback_type": "Submit", "submission_type": "external"}, 0, "Submitted – External"),
        # Pre-2026-09-11 submit: no submission_type = the endpoint's default, external.
        ({"feedback_type": "Submit"}, 0, "Submitted – External"),
        ({"feedback_type": "submit", "submission_type": "Internal"}, 0, "Submitted – Internal"),
        ({"feedback_type": "Submit", "submission_type": "internal"}, 2, "Submitted – Internal (JobDiva confirmed)"),
        ({}, 1, "Submitted in JobDiva"),
        ({}, 0, "Not Submitted"),
        # A later Reject / Unreachable leaves submission_type behind: not a submit.
        ({"feedback_type": "Reject", "submission_type": "internal"}, 0, "Not Submitted"),
        ({"feedback_type": "Reject - Skills", "submission_type": "external"}, 1, "Submitted in JobDiva"),
        ({"feedback_type": "Unreachable", "submission_type": "external"}, 0, "Not Submitted"),
        # JobDiva lookup failed (count unknown): PAIR data alone.
        ({"feedback_type": "Submit", "submission_type": "external"}, None, "Submitted – External"),
        ({}, None, "Not Submitted"),
        (None, None, "Not Submitted"),
    ],
)
def test_submittal_status_labels(data, count, label):
    assert cr._submittal_status(data, count) == label


def test_feedback_fields_only_report_actor_and_time_with_a_current_decision():
    # Inherited from a prior job by cross-submissions: no feedback_* keys, but
    # submitted_by / submission_type survive the copy.
    stale = {"submitted_by": "old@x.com", "submission_type": "internal"}
    assert cr._launched_feedback_fields(stale) == {
        "feedback": None,
        "feedback_reason": None,
        "feedback_at": None,
        "feedback_by": None,
        "submission_type": None,
    }
    rejected = {
        "feedback_type": "Reject",
        "feedback_reason": "Skills do not meet requirements",
        "feedback_at": "2026-09-21T13:46:55+00:00",
        "submitted_by": " rec@x.com ",
        "submission_type": "internal",
    }
    assert cr._launched_feedback_fields(rejected) == {
        "feedback": "Reject",
        "feedback_reason": "Skills do not meet requirements",
        "feedback_at": "2026-09-21T13:46:55+00:00",
        "feedback_by": "rec@x.com",
        "submission_type": None,
    }
    submitted = {"feedback_type": "Submit", "feedback_reason": None, "submission_type": "internal"}
    fields = cr._launched_feedback_fields(submitted)
    assert fields["feedback"] == "Submit"
    assert fields["feedback_reason"] is None
    assert fields["submission_type"] == "internal"
    assert cr._launched_feedback_fields(None)["feedback"] is None


@pytest.mark.parametrize(
    "value,label",
    [
        ("pass", "Pass"),
        ("Pass", "Pass"),
        ("fail", "Fail"),
        ("in progress", "In Progress"),
        ("In Progress", "In Progress"),
        ("pending", "Pending"),
        ("waiting", "Pending"),
        ("Initiated", "Pending"),
    ],
)
def test_status_filter_selects_the_display_label(value, label):
    clause, params = cr._launched_status_filter(value)
    assert cr.LAUNCHED_PASS_STATUS_SQL in clause
    assert params == [label]


def test_status_filter_edge_values():
    assert cr._launched_status_filter(None) == ("", [])
    assert cr._launched_status_filter("  ") == ("", [])
    # An unrecognised value keeps its old meaning: the raw audit status.
    assert cr._launched_status_filter("sent") == (" AND la.status = %s", ["sent"])


def test_statements_take_exactly_the_params_the_endpoint_passes():
    search_condition, params, fb_cond, fb_order = cr._launched_filter_conditions(
        "jane", "pass", "Reject", "LinkedIn", 60, "2026-09-01", "2026-09-30",
    )
    rows_sql, count_sql = cr._launched_candidates_sql(search_condition, fb_cond, fb_order)
    # '%%' is psycopg2's escaped literal percent, not a placeholder.
    assert rows_sql.replace("%%", "").count("%s") == len(params) + 2
    assert count_sql.replace("%%", "").count("%s") == len(params)
    assert "%" not in cr.LAUNCHED_PASS_STATUS_SQL
    assert "%" not in cr._PAGE_SUBMITTALS_SQL.replace("%s", "")


# Shape of the SHIPPED statements, checked without a database so CI (which has
# none, and so skips every Postgres test below) still catches a regression.
# tests/test_candidates_feedback_filter.py pins the same properties against its
# own hand-copied CTE, which still has the old cross-job latest_audit.


def _squash(sql):
    return " ".join(sql.split())


def _shipped(feedback=None):
    search_condition, _, fb_cond, fb_order = cr._launched_filter_conditions(
        None, None, feedback, None, None, None, None,
    )
    rows_sql, count_sql = cr._launched_candidates_sql(search_condition, fb_cond, fb_order)
    return _squash(rows_sql), _squash(count_sql), _squash(fb_cond)


_LAUNCHED_ORDER_BY = "ORDER BY COALESCE(mj.job_key, sc.jobdiva_id), sc.candidate_id, "
_TWIN_DECISION_ORDER = (
    "CASE WHEN COUNT(*) OVER job_person > 1 THEN NULLIF(TRIM(sc.data->>'feedback_type'), '') IS NOT NULL END DESC NULLS LAST, "
    "CASE WHEN COUNT(*) OVER job_person > 1 THEN sc.data->>'feedback_at' END DESC NULLS LAST, "
    "sc.created_at DESC"
)


def _launched_order_tiebreak(rows_sql):
    # The feedback filter's tiebreak: whatever sits between the DISTINCT ON
    # keys and the always-present twin-row decision preference.
    start = rows_sql.index(_LAUNCHED_ORDER_BY) + len(_LAUNCHED_ORDER_BY)
    return rows_sql[start:rows_sql.index(_TWIN_DECISION_ORDER, start)]


def test_shipped_page_and_count_share_one_cte():
    for feedback in (None, "Submit", "Reject", "No Feedback"):
        rows_sql, count_sql, _ = _shipped(feedback)
        rows_cte = rows_sql.split(" SELECT * FROM launched_candidates ")[0]
        count_cte = count_sql.split(" SELECT COUNT(*) AS total FROM launched_candidates")[0]
        assert rows_cte == count_cte


def test_shipped_audit_row_is_job_scoped():
    rows_sql, _, _ = _shipped()
    assert "SELECT DISTINCT ON (a.job_key, a.candidate_id)" in rows_sql
    assert "ORDER BY a.job_key, a.candidate_id, a.id DESC" in rows_sql
    assert "AND la.job_key = COALESCE(mj.job_key, sc.jobdiva_id)" in rows_sql
    # The pre-2026-09-23 cross-job pick must not come back.
    assert "DISTINCT ON (candidate_id)" not in rows_sql


def test_shipped_audit_pick_skips_failed_attempts_before_the_newest_row():
    rows_sql, _, _ = _shipped()
    audit = rows_sql[rows_sql.index("latest_audit AS ("):rows_sql.index("launched_candidates AS (")]
    # Inside the sub-select, i.e. before DISTINCT ON picks the newest row.
    assert audit.index("AND COALESCE(NULLIF(eia.interview_id, ''), '') <> ''") < audit.index(") a ORDER BY")


def test_shipped_rows_are_one_per_job_and_person_across_both_keys():
    for feedback in (None, "Submit", "No Feedback"):
        rows_sql, _, _ = _shipped(feedback)
        assert "SELECT DISTINCT ON (COALESCE(mj.job_key, sc.jobdiva_id), sc.candidate_id)" in rows_sql
        assert "WINDOW job_person AS (PARTITION BY COALESCE(mj.job_key, sc.jobdiva_id), sc.candidate_id)" in rows_sql
        order_at = rows_sql.index(_LAUNCHED_ORDER_BY)
        # The twin-row decision preference is last before the created_at fallback.
        assert rows_sql.index(_TWIN_DECISION_ORDER, order_at) > order_at


def test_shipped_tiebreak_only_prefers_feedback_when_a_filter_is_active():
    rows_sql, _, _ = _shipped()
    assert _launched_order_tiebreak(rows_sql) == ""
    for feedback in ("Submit", "Reject", "Unreachable", "No Feedback"):
        _, matching_pred = cr._build_feedback_filter_condition(feedback)
        tiebreak = _launched_order_tiebreak(_shipped(feedback)[0])
        assert tiebreak.startswith(_squash(matching_pred.replace("%", "%%")) + " DESC, ")
        assert "(sc.data->>'feedback_at') DESC NULLS LAST" in tiebreak


def test_shipped_feedback_condition_sits_in_where_not_order_by():
    for feedback in ("Submit", "Reject", "Unreachable", "No Feedback"):
        rows_sql, _, fb_cond = _shipped(feedback)
        assert fb_cond
        where_at = rows_sql.index("WHERE (la.interview_id IS NOT NULL")
        order_at = rows_sql.index(_LAUNCHED_ORDER_BY)
        assert where_at < rows_sql.index(fb_cond) < order_at
        assert "NOT EXISTS" not in _launched_order_tiebreak(rows_sql)


def test_malformed_date_is_a_400_not_a_500():
    with pytest.raises(cr.HTTPException) as exc:
        cr._launched_filter_conditions(None, None, None, None, None, "09/21/2026", None)
    assert exc.value.status_code == 400


def test_non_admin_is_refused():
    with pytest.raises(cr.HTTPException) as exc:
        asyncio.run(cr.get_launched_candidates(
            user=UserIdentity(email="r@x.com", role="team_lead"),
            limit=50, offset=0, search=None, status=None, feedback=None,
            source=None, min_score=None, start_date=None, end_date=None,
        ))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Real Postgres — the endpoint end to end
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

# engage_interview_audit / sourced_candidates: column-for-column the CREATE
# TABLE statements in routers/engagement.py and
# services/sourced_candidates_storage.py; jobdiva_submittals: routers/jobs.py.
# monitored_jobs has no CREATE TABLE in the repo — only the columns read here.
_SCHEMA_SQL = """
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), candidate_name VARCHAR(255), candidate_email VARCHAR(255),
    payload JSONB, response JSONB, status VARCHAR(50) DEFAULT 'sent',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT, email TEXT, phone TEXT, headline TEXT, location TEXT, resume_id TEXT, resume_text TEXT,
    profile_url TEXT, image_url TEXT, data JSONB, status TEXT DEFAULT 'sourced',
    resume_match_percentage INT DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY, jobdiva_id TEXT, title TEXT, screening_level TEXT,
    recruiter_emails TEXT, customer_name TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE jobdiva_submittals (
    id SERIAL PRIMARY KEY, job_id TEXT NOT NULL, jobdiva_ref TEXT,
    candidate_id TEXT NOT NULL DEFAULT '', recipient_name TEXT,
    submit_date TIMESTAMP NULL, data JSONB, synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
"""

A_REF, A_NUM = "26-A0001", "101"
B_REF, B_NUM = "26-B0002", "102"


class _Borrowed:
    """The endpoint closes the connection it borrows; keep the test's session
    (and its TEMP tables) alive."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_SCHEMA_SQL)
            cur.execute(
                "INSERT INTO monitored_jobs VALUES (%s, %s, 'Job A', 'L1', %s, 'Acme'), (%s, %s, 'Job B', 'L2', NULL, 'Beta')",
                (A_NUM, A_REF, json.dumps(["ra@x.com"]), B_NUM, B_REF),
            )
        monkeypatch.setattr(cr, "get_db_connection", lambda: _Borrowed(conn))

        async def _no_live(_ids):
            return {}

        monkeypatch.setattr(cr, "_fetch_all_outreach", _no_live)
        yield conn
    finally:
        conn.rollback()  # ON COMMIT DROP never fires; rollback discards everything
        conn.close()


def _sourced(conn, key, cid, data=None, source="LinkedIn", created="2026-09-01 10:00:00"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, name, data, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (key, cid, source, cid.upper(), json.dumps(data or {}), created),
        )


def _audit(conn, key, cid, iid, status="sent", created="2026-09-20 10:00:00"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, created_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (cid, key, iid, status, created),
        )


def _submittal(conn, job_id, candidate_id, submit_date):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobdiva_submittals (job_id, jobdiva_ref, candidate_id, submit_date) VALUES (%s, %s, %s, %s)",
            (job_id, None, candidate_id, submit_date),
        )


def _call(**kw):
    args = dict(limit=50, offset=0, search=None, status=None, feedback=None, source=None,
                min_score=None, start_date=None, end_date=None)
    args.update(kw)
    return asyncio.run(cr.get_launched_candidates(user=ADMIN, **args))


def _rows(result):
    return {(r["jobdiva_id"], r["candidate_id"]): r for r in result["candidates"]}


def _seed_two_jobs(conn):
    # c1 is launched on both jobs. Job A's audit row sits under A's NUMERIC
    # key while the sourced row uses the ref — the same job, so they meet.
    _sourced(conn, A_REF, "c1", {})
    _sourced(conn, B_REF, "c1", {"engage_score": 30})
    _audit(conn, A_NUM, "c1", "iA1", "completed", "2026-09-20 10:00:00")
    _audit(conn, B_REF, "c1", "iB1", "failed", "2026-09-21 10:00:00")  # newer, other job
    # c2 is sourced on both but only launched on B: it must not be listed under A.
    _sourced(conn, A_REF, "c2", {})
    _sourced(conn, B_REF, "c2", {})
    _audit(conn, B_REF, "c2", "iB2", "passed", "2026-09-21 11:00:00")
    # c9's sourced row uses A's numeric key, its audit row the ref.
    _sourced(conn, A_NUM, "c9", {"engage_status": "passed"})
    _audit(conn, A_REF, "c9", "iA9", "passed", "2026-09-20 12:00:00")


def test_pg_audit_row_is_job_scoped(pg):
    _seed_two_jobs(pg)
    rows = _rows(_call())

    a1, b1 = rows[(A_REF, "c1")], rows[(B_REF, "c1")]
    # Job A's own launch: completed with no failed hard filter → Pass.
    assert (a1["engage_interview_id"], a1["pass_status"], a1["engage_status"]) == ("iA1", "Pass", "Pass")
    assert a1["engage_created_at"] == "2026-09-20T10:00:00Z"
    # Job B's own launch: failed with a score → Fail.
    assert (b1["engage_interview_id"], b1["pass_status"]) == ("iB1", "Fail")
    assert b1["engage_created_at"] == "2026-09-21T10:00:00Z"

    assert (A_REF, "c2") not in rows, "never launched on job A"
    assert rows[(B_REF, "c2")]["pass_status"] == "Pass"

    c9 = rows[(A_NUM, "c9")]
    assert c9["engage_interview_id"] == "iA9"
    # Monitored-job fields resolve from either key variant.
    assert (c9["job_id"], c9["job_title"], c9["customer_name"]) == (A_NUM, "Job A", "Acme")
    assert (a1["job_id"], a1["customer_name"]) == (A_NUM, "Acme")
    assert (b1["job_id"], b1["customer_name"]) == (B_NUM, "Beta")
    assert "audit_response" not in a1


def test_pg_launch_date_filter_uses_the_rows_own_launch(pg):
    _seed_two_jobs(pg)
    # 2026-09-20 in New York covers c1's job-A launch (06:00 ET) only; the old
    # cross-job audit row carried job B's 09-21 date onto job A's row.
    rows = _rows(_call(start_date="2026-09-20", end_date="2026-09-20"))
    assert (A_REF, "c1") in rows
    assert (B_REF, "c1") not in rows


def test_pg_failed_relaunch_does_not_hide_an_earlier_launch(pg):
    """A Fail is retryable (engage_status 'failed'), and a re-launch pair-bot
    rejects writes a newer audit row with no interview id. The person is still
    launched on the job — as the Launch Report and Rankings count them."""
    _sourced(pg, A_REF, "c3", {"engage_status": "failed", "engage_score": 20})
    _audit(pg, A_REF, "c3", "iA3", "failed", "2026-09-20 10:00:00")
    _audit(pg, A_NUM, "c3", "", "failed", "2026-09-20 11:00:00")  # newest: no interview id
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, created_at)"
            " VALUES ('c3', %s, NULL, 'failed', '2026-09-20 12:00:00')",
            (A_REF,),
        )
    result = _call()
    assert result["total"] == 1
    row = result["candidates"][0]
    assert (row["engage_interview_id"], row["pass_status"]) == ("iA3", "Fail")
    assert row["engage_created_at"] == "2026-09-20T10:00:00Z"
    # Only failed attempts ever: not launched.
    _sourced(pg, A_REF, "c4", {"engage_status": "failed"})
    _audit(pg, A_REF, "c4", "", "failed", "2026-09-20 13:00:00")
    assert _call()["total"] == 1


def _seed_dual_key(conn):
    """d1 has a row under each of job A's keys and the Submit on the older
    one (the feedback endpoint writes to exactly one row); d2 has two rows and
    no decision; d3 decided twice, the newer decision on the newer row."""
    _sourced(conn, A_REF, "d1", {"feedback_type": "Submit", "submission_type": "external",
                                 "feedback_at": "2026-09-21T10:00:00+00:00"}, created="2026-09-01 10:00:00")
    _sourced(conn, A_NUM, "d1", {}, created="2026-09-02 10:00:00")
    _sourced(conn, A_REF, "d2", {}, created="2026-09-01 10:00:00")
    _sourced(conn, A_NUM, "d2", {}, created="2026-09-02 10:00:00")
    _sourced(conn, A_NUM, "d3", {"feedback_type": "Reject", "feedback_at": "2026-09-21T09:00:00+00:00"},
             created="2026-09-01 10:00:00")
    _sourced(conn, A_REF, "d3", {"feedback_type": "Unreachable", "feedback_at": "2026-09-22T09:00:00+00:00"},
             created="2026-09-02 10:00:00")
    for n, cid in enumerate(("d1", "d2", "d3")):
        _audit(conn, A_NUM, cid, f"i-{cid}", "completed", f"2026-09-20 1{n}:00:00")


def test_pg_person_under_both_job_keys_is_one_row_with_the_decision(pg):
    _seed_dual_key(pg)
    result = _call()
    assert result["total"] == 3
    rows = {r["candidate_id"]: r for r in result["candidates"]}
    assert len(rows) == 3
    d1 = rows["d1"]
    assert (d1["jobdiva_id"], d1["feedback"], d1["submittal_status"]) == (A_REF, "Submit", "Submitted – External")
    assert rows["d2"]["jobdiva_id"] == A_NUM  # no decision anywhere: the newest row
    assert rows["d3"]["feedback"] == "Unreachable"  # the newest decision


@pytest.mark.parametrize(
    "value,expected",
    [
        # d1's Submit and d3's decisions sit on the other key's row.
        ("No Feedback", {"d2"}),
        ("Submit", {"d1"}),
        ("Unreachable", {"d3"}),
    ],
)
def test_pg_feedback_filters_see_both_job_keys(pg, value, expected):
    _seed_dual_key(pg)
    result = _call(feedback=value)
    assert [r["candidate_id"] for r in result["candidates"]] == sorted(expected)
    assert result["total"] == len(expected)


def _seed_statuses(conn):
    cases = {
        "p_completed": ({}, "completed"),                                         # Pass
        "p_hired": ({"engage_status": "hired"}, "sent"),                          # Pass (blob wins)
        "f_hf": ({"engage_status": "completed", "engage_hard_filter_status": "fail"}, "completed"),  # Fail
        "f_scored": ({"engage_status": "failed", "engage_score": 12}, "failed"),  # Fail
        "q_incomplete": ({"engage_status": "incomplete"}, "completed"),           # Pending: exact tokens only
        "q_noscore": ({"engage_status": "failed"}, "failed"),                     # Pending: outreach miss
        "q_sent": ({}, "sent"),                                                   # Pending
        "i_call": ({}, "call_in_progress"),                                       # In Progress
    }
    for n, (cid, (data, audit_status)) in enumerate(cases.items()):
        _sourced(conn, A_REF, cid, data)
        _audit(conn, A_REF, cid, f"i-{cid}", audit_status, f"2026-09-20 1{n}:00:00")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("pass", {"p_completed", "p_hired"}),
        ("fail", {"f_hf", "f_scored"}),
        ("pending", {"q_incomplete", "q_noscore", "q_sent"}),
        ("in progress", {"i_call"}),
    ],
)
def test_pg_status_filter_matches_the_displayed_label(pg, value, expected):
    _seed_statuses(pg)
    result = _call(status=value)
    got = {r["candidate_id"] for r in result["candidates"]}
    assert got == expected
    assert result["total"] == len(expected)
    # With no live read in play, every returned row displays the filtered label.
    label = cr._LAUNCHED_STATUS_FILTER_LABELS[value]
    assert {r["pass_status"] for r in result["candidates"]} == {label}


def test_pg_unfiltered_labels_agree_with_the_sql_classification(pg):
    _seed_statuses(pg)
    by_filter = {}
    for value in ("pass", "fail", "pending", "in progress"):
        for r in _call(status=value)["candidates"]:
            by_filter[r["candidate_id"]] = cr._LAUNCHED_STATUS_FILTER_LABELS[value]
    shown = {r["candidate_id"]: r["pass_status"] for r in _call()["candidates"]}
    assert shown == by_filter


def _seed_submittals(conn):
    fb_at = "2026-09-21T13:46:55+00:00"
    _sourced(conn, A_REF, "s1", {"feedback_type": "Submit", "submission_type": "external", "feedback_at": fb_at,
                                 "submitted_by": "rec@x.com", "jobdiva_candidate_id": "JD-S1"})
    _sourced(conn, A_REF, "s2", {"feedback_type": "Submit", "feedback_at": fb_at})  # legacy: no type
    _sourced(conn, A_REF, "s3", {"feedback_type": "Submit", "submission_type": "internal",
                                 "manager_email": "m@pyramidci.com"}, source="JobDiva-TalentSearch")
    _sourced(conn, A_REF, "s4", {"feedback_type": "Reject", "feedback_reason": "Communication skills",
                                 "submission_type": "internal", "submitted_by": "rec@x.com"})
    _sourced(conn, A_REF, "s5", {"jobdiva_candidate_id": "JD-S5"})
    _sourced(conn, A_REF, "s6", {"feedback_type": "Unreachable", "submission_type": "external"})
    for n, cid in enumerate(("s1", "s2", "s3", "s4", "s5", "s6")):
        _audit(conn, A_REF, cid, f"i-{cid}", "completed", f"2026-09-20 1{n}:00:00")
    # JobDiva keys submittals by the numeric job_id and its own CANDIDATEID.
    _submittal(conn, A_NUM, "JD-S1", "2026-09-22 08:00:00")
    _submittal(conn, A_NUM, "JD-S1", "2026-09-21 23:30:00")
    _submittal(conn, B_NUM, "JD-S1", "2026-09-01 09:00:00")  # another job: never counted on A
    _submittal(conn, A_NUM, "s3", None)                       # JobDiva-sourced: candidate_id is the CANDIDATEID
    _submittal(conn, A_NUM, "JD-S5", "2026-09-23 01:00:00")


def test_pg_submittal_status_and_feedback_fields(pg):
    _seed_submittals(pg)
    rows = {r["candidate_id"]: r for r in _call()["candidates"]}

    s1 = rows["s1"]
    assert s1["submittal_status"] == "Submitted – External (JobDiva confirmed)"
    assert (s1["jobdiva_submittal_count"], s1["jobdiva_submittal_date"]) == (2, "2026-09-21")
    assert (s1["feedback"], s1["feedback_by"], s1["submission_type"]) == ("Submit", "rec@x.com", "external")
    assert s1["feedback_at"] == "2026-09-21T13:46:55+00:00"

    assert rows["s2"]["submittal_status"] == "Submitted – External"
    assert (rows["s2"]["jobdiva_submittal_count"], rows["s2"]["jobdiva_submittal_date"]) == (0, None)

    s3 = rows["s3"]
    assert s3["submittal_status"] == "Submitted – Internal (JobDiva confirmed)"
    assert (s3["jobdiva_submittal_count"], s3["jobdiva_submittal_date"]) == (1, None)

    s4 = rows["s4"]
    assert s4["submittal_status"] == "Not Submitted"
    assert (s4["feedback"], s4["feedback_reason"], s4["submission_type"]) == ("Reject", "Communication skills", None)

    assert rows["s5"]["submittal_status"] == "Submitted in JobDiva"
    assert rows["s5"]["feedback"] is None
    assert rows["s6"]["submittal_status"] == "Not Submitted"
    assert rows["s6"]["feedback"] == "Unreachable"


def test_pg_submittal_lookup_failure_degrades_to_pair_data(pg, monkeypatch):
    _seed_submittals(pg)
    # Same placeholders, so the failure comes from the server, mid-transaction.
    monkeypatch.setattr(
        cr,
        "_PAGE_SUBMITTALS_SQL",
        "SELECT 1, 1, NULL FROM no_such_submittals_table WHERE %s::bigint[] IS NOT NULL"
        " AND %s::text[] IS NOT NULL AND %s::text[] IS NOT NULL AND %s::text[] IS NOT NULL",
    )
    rows = {r["candidate_id"]: r for r in _call()["candidates"]}
    assert len(rows) == 6
    assert rows["s1"]["submittal_status"] == "Submitted – External"
    assert rows["s3"]["submittal_status"] == "Submitted – Internal"
    assert rows["s5"]["submittal_status"] == "Not Submitted"
    assert all(r["jobdiva_submittal_count"] is None for r in rows.values())


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Submit", {"s1", "s2", "s3"}),
        ("Reject", {"s4"}),
        ("Unreachable", {"s6"}),
        ("No Feedback", {"s5"}),
    ],
)
def test_pg_feedback_filters_run(pg, value, expected):
    _seed_submittals(pg)
    result = _call(feedback=value)
    assert {r["candidate_id"] for r in result["candidates"]} == expected
    assert result["total"] == len(expected)


def test_pg_pages_are_stable_and_match_the_total(pg):
    _seed_statuses(pg)
    _seed_submittals(pg)
    # Two rows share a launch time so the id tiebreak is exercised.
    _sourced(pg, A_REF, "t1", {})
    _audit(pg, A_REF, "t1", "i-t1", "sent", "2026-09-20 10:00:00")
    total = _call()["total"]
    seen = []
    for offset in range(0, total, 3):
        seen += [r["id"] for r in _call(limit=3, offset=offset)["candidates"]]
    assert len(seen) == total == len(set(seen)) == 15
