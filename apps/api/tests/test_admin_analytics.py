import datetime
from unittest.mock import MagicMock

import pytest

import routers.admin_analytics as aa
from routers._helpers import _ts, _ts_utc
from routers.admin_analytics import (
    _TIMELINE_COLUMNS,
    _compute_jobs_timeline,
    _compute_scoped_job_metrics,
    _compute_submission_metrics,
)
from services.job_candidate_metrics import empty_metrics

UTC = datetime.timezone.utc
_REAL_MISSING_OPTIONAL_COLUMNS = aa._missing_optional_columns


@pytest.fixture(autouse=True)
def _all_optional_columns_present(monkeypatch):
    """The timeline probes the catalog for the optional attribution columns
    through its own cursor; on the shared mock cursor that probe would eat the
    first fetchall. Tests of the probe itself override this."""
    monkeypatch.setattr(aa, "_missing_optional_columns", lambda conn: frozenset())


def _mock_conn():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn, mock_cursor


def _timeline_row(**overrides):
    """One row of the timeline SELECT, built by column name so adding a
    column only means adding a default here."""
    now = datetime.datetime.now(UTC)
    values = {
        "job_id": "job1",
        "jobdiva_id": "jd1",
        "title": "Title",
        "customer_name": "Cust",
        "posted_date": "01/01/2026",
        "created_at": now,
        "pair_launched_at": now,
        "outreach_stopped_at": now,
        "is_archived": False,
        "archive_reason": None,
        "status": "OPEN",
        "candidates_sourced": 5,
        "candidates_launched": 5,
        "jobdiva_total_subs": 5,
        "pair_external_subs": 0,
        "campaign_id": "camp1",
        "recruiter_emails": '["test@example.com", "other@example.com"]',
        "pair_posted_by": None,
        "pair_launched_by": None,
    }
    unknown = set(overrides) - set(values)
    assert not unknown, unknown
    values.update(overrides)
    assert set(values) == set(_TIMELINE_COLUMNS)
    return tuple(values[c] for c in _TIMELINE_COLUMNS)


def test_compute_jobs_timeline_removes_200_limit():
    """
    Test that _compute_jobs_timeline does not artificially truncate to 200 records.
    It should now use LIMIT 2000 to prevent unbounded query risks but allow large result sets.
    """
    mock_conn, mock_cursor = _mock_conn()

    # Mock total count return
    mock_cursor.fetchone.return_value = (5000,)

    # Mock data rows
    mock_cursor.fetchall.return_value = []

    _compute_jobs_timeline(mock_conn, scope=None)

    # The second execute call is for the main query
    assert mock_cursor.execute.call_count == 2
    query = mock_cursor.execute.call_args_list[1][0][0]

    import re
    assert not re.search(r"LIMIT\s+200\b", query), "Query should not be hard-limited to 200 records"
    assert re.search(r"LIMIT\s+2000\b", query), "Query should be bounded to 2000 records to prevent scalability issues"

def test_compute_jobs_timeline_recruiter_emails():
    """
    Test that recruiter_emails are properly parsed and included in the timeline rows.
    """
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [_timeline_row(archive_reason="Reason")]

    result = _compute_jobs_timeline(mock_conn, scope=None)

    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["recruiter_emails"] == ["test@example.com", "other@example.com"]
    assert row["archive_reason"] == "Reason"


def test_compute_jobs_timeline_dedup_key_is_job_id_not_jobdiva_id():
    """A job edited after launch clones into a new job_id row that keeps the
    same jobdiva_id (see launch_report.py); deduping on jobdiva_id would drop
    the earlier version's own candidates/campaign/timeline data. The
    ROW_NUMBER() partition (and the total_jobs count) must key on job_id,
    not COALESCE(jobdiva_id, job_id::text).
    """
    mock_conn, mock_cursor = _mock_conn()

    mock_cursor.fetchone.return_value = (2,)
    mock_cursor.fetchall.return_value = []

    _compute_jobs_timeline(mock_conn, scope=None)

    count_query = mock_cursor.execute.call_args_list[0][0][0]
    main_query = mock_cursor.execute.call_args_list[1][0][0]
    assert "COUNT(DISTINCT job_id::text)" in count_query
    assert "PARTITION BY job_id::text" in main_query
    assert "COALESCE(jobdiva_id" not in main_query


def test_timeline_query_has_no_candidate_join_that_can_duplicate_rows():
    """The old feedback_times step joined sourced_candidates on either job key
    (`= jobdiva_id OR = job_id::text`), so a job with feedback stored under
    both keys came back as two timeline rows. Candidate numbers are merged in
    Python by job_id now; the main query must not touch the candidate table."""
    main_query = aa._jobs_timeline_sql("TRUE")
    assert "feedback_times" not in main_query
    assert "sourced_candidates" not in main_query
    assert "JOIN" not in main_query.upper()


def test_timeline_reads_created_at_in_its_own_zone():
    """The +5:30 bug: created_at text written by readable_ist_now() ends in
    " IST". Truncating it with _ts() and declaring UTC read it 5h30m late."""
    main_query = aa._jobs_timeline_sql("TRUE")
    assert f"{_ts_utc('d.created_at')} AS created_at" in main_query
    # (the old reading; _ts_utc keeps it only as its non-IST ELSE branch)
    assert f"({_ts('d.created_at')}) AT TIME ZONE 'UTC' AS created_at" not in main_query
    # ...and the dedup / sort key uses the same reading.
    assert f"{_ts_utc('created_at')})" in main_query
    # pair_launched_at is a real TIMESTAMP written with NOW(): still UTC.
    assert f"({_ts('d.pair_launched_at')}) AT TIME ZONE 'UTC' AS pair_launched_at" in main_query


def test_other_created_at_readers_are_zone_aware():
    """Aged-unlaunched and the weekly jobs_added buckets read created_at too."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "routers" / "admin_analytics.py").read_text()
    assert "_ts('created_at')" not in text
    assert "_ts('d.created_at')" not in text
    assert "{_ts_utc('created_at')} < NOW() - INTERVAL '7 days'" in text


def test_funnel_sql_maps_completed_like_rankings():
    from pathlib import Path
    from services.engage_status import effective_funnel_status

    src = Path(__file__).resolve().parents[1] / "routers" / "admin_analytics.py"
    text = src.read_text()
    assert "IN ('completed', 'complete')" in text
    assert "AND NULLIF(TRIM(COALESCE(sc.data->>'engage_score', '')), '') IS NOT NULL THEN 'failed'" in text

    assert effective_funnel_status("completed", None, "passed", has_interview=True) == "passed"
    assert effective_funnel_status("completed", None, "failed", has_interview=True) == "failed"
    assert effective_funnel_status("failed", None, "", has_interview=True) == "launched"
    assert effective_funnel_status("failed", "65", "", has_interview=True) == "failed"
    assert effective_funnel_status("qualified", None, "", has_interview=False) == "passed"
    assert effective_funnel_status("in_progress", None, "", has_interview=True) == "in_progress"
    assert effective_funnel_status("", None, "", has_interview=True) == "launched"
    assert effective_funnel_status("", None, "", has_interview=False, sc_status="pending") == "pending"


def test_compute_jobs_timeline_recruiter_emails_scoped_to_team():
    """A job shared across teams must not leak recruiters outside the
    requesting team's scope through recruiter_emails — mirrors the guard
    already applied to the Top Recruiters section.
    """
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [
        _timeline_row(recruiter_emails='["team@example.com", "outsider@example.com"]')
    ]

    scope = {"job_ids": ["job1"], "sc_keys": [], "emails": ["team@example.com"]}
    result = _compute_jobs_timeline(mock_conn, scope=scope)

    row = result["rows"][0]
    assert row["recruiter_emails"] == ["team@example.com"]
    assert "outsider@example.com" not in row["recruiter_emails"]


def test_compute_jobs_timeline_recruiter_emails_unscoped_keeps_all():
    """No team scope (admin view) must still see every recruiter."""
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [
        _timeline_row(recruiter_emails='["team@example.com", "outsider@example.com"]')
    ]

    result = _compute_jobs_timeline(mock_conn, scope=None)

    row = result["rows"][0]
    assert row["recruiter_emails"] == ["team@example.com", "outsider@example.com"]


# ---------------------------------------------------------------------------
# Per-job candidate metrics merged into the timeline (2026-09-23 columns)
# ---------------------------------------------------------------------------


def _metrics(**overrides):
    m = empty_metrics()
    m.update(overrides)
    return m


def test_timeline_merges_candidate_metrics_by_job_id():
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (2,)
    mock_cursor.fetchall.return_value = [
        _timeline_row(job_id="101", jobdiva_id="26-101", pair_external_subs=3,
                      pair_posted_by=" Poster@Pyramid.com ", pair_launched_by="launcher@pyramid.com"),
        _timeline_row(job_id="102", jobdiva_id="26-102"),
    ]
    first_ext = datetime.datetime(2026, 9, 21, 13, 46, 55, tzinfo=UTC)
    first_fb = datetime.datetime(2026, 9, 20, 9, 0, 0, tzinfo=UTC)
    job_metrics = {
        "101": _metrics(passed=4, failed=2, feedback_total=5, pair_submits=3, rejects=1,
                        unreachable=1, pair_internal_submits=1, pair_external_submits=2,
                        first_feedback_at=first_fb, first_external_submit_at=first_ext),
        "102": _metrics(),
    }

    rows = _compute_jobs_timeline(mock_conn, scope=None, job_metrics=job_metrics)["rows"]

    by_id = {r["job_id"]: r for r in rows}
    r = by_id["101"]
    assert r["pass_candidates"] == 4
    assert r["fail_candidates"] == 2
    assert r["feedback_total"] == 5
    assert r["feedback_submits"] == 3
    assert r["feedback_rejects"] == 1
    assert r["feedback_unreachable"] == 1
    assert r["pair_internal_submits"] == 1
    assert r["pair_external_submits"] == 2
    # PAIR-recorded external vs the JobDiva-verified counter are separate fields.
    assert r["jobdiva_confirmed_subs"] == 3
    assert r["jobdiva_submittals"] == 5
    assert r["first_pair_external_submit_at"] == "2026-09-21T13:46:55+00:00"
    assert r["first_feedback_at"] == "2026-09-20T09:00:00+00:00"
    assert r["posted_by"] == "poster@pyramid.com"
    assert r["launched_by"] == "launcher@pyramid.com"

    r2 = by_id["102"]
    assert r2["pass_candidates"] == 0
    assert r2["first_pair_external_submit_at"] is None
    # Jobs created before attribution existed: None, rendered as "—".
    assert r2["posted_by"] is None and r2["launched_by"] is None


_METRIC_ROW_KEYS = (
    "pass_candidates", "fail_candidates", "feedback_total", "feedback_submits",
    "feedback_rejects", "feedback_unreachable", "pair_internal_submits",
    "pair_external_submits", "first_feedback_at", "first_pair_external_submit_at",
)


def test_timeline_without_metrics_reports_none_not_zeros():
    """job_metrics=None (the metrics section failed): rows still come back,
    but every candidate column is None ("—"), never a 0 that reads as real.
    The job's own counters are unaffected."""
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [_timeline_row(job_id="101", pair_external_subs=2)]

    rows = _compute_jobs_timeline(mock_conn, scope=None, job_metrics=None)["rows"]

    assert len(rows) == 1
    r = rows[0]
    for key in _METRIC_ROW_KEYS:
        assert r[key] is None, key
    assert r["jobdiva_confirmed_subs"] == 2
    assert r["candidates_launched"] == 5


def test_timeline_job_missing_from_available_metrics_gets_zeros():
    """A job created between the metrics read and the timeline read has no
    candidates yet: zeros are the truth for it, unlike the unavailable case."""
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [_timeline_row(job_id="999")]

    r = _compute_jobs_timeline(mock_conn, scope=None, job_metrics={})["rows"][0]

    assert r["pass_candidates"] == 0 and r["feedback_total"] == 0
    assert r["first_feedback_at"] is None


# ---------------------------------------------------------------------------
# Optional attribution columns (schema init may not have added them yet)
# ---------------------------------------------------------------------------


def test_timeline_sql_selects_null_for_missing_attribution_columns():
    present = aa._jobs_timeline_sql("TRUE")
    assert "NULL::text AS pair_posted_by" not in present

    missing = aa._jobs_timeline_sql("TRUE", frozenset({"pair_posted_by", "pair_launched_by"}))
    assert "NULL::text AS pair_posted_by" in missing
    assert "NULL::text AS pair_launched_by" in missing
    # The outer SELECT (and so the unpacking) is unchanged.
    assert "d.pair_posted_by AS pair_posted_by" in missing


def test_timeline_passes_the_probe_result_to_the_sql(monkeypatch):
    monkeypatch.setattr(aa, "_missing_optional_columns", lambda conn: frozenset({"pair_launched_by"}))
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = (1,)
    mock_cursor.fetchall.return_value = [_timeline_row(job_id="101", pair_posted_by="p@x.com")]

    rows = _compute_jobs_timeline(mock_conn, scope=None, job_metrics={})["rows"]

    main_query = mock_cursor.execute.call_args_list[1][0][0]
    assert "NULL::text AS pair_launched_by" in main_query
    assert "NULL::text AS pair_posted_by" not in main_query
    assert rows[0]["posted_by"] == "p@x.com"
    assert rows[0]["launched_by"] is None


def test_missing_optional_columns_probe():
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchall.return_value = [("job_id",), ("pair_posted_by",)]
    # The autouse fixture replaced the module attribute; call the real one.
    missing = _REAL_MISSING_OPTIONAL_COLUMNS(mock_conn)
    assert missing == frozenset({"pair_launched_by"})
    sql = mock_cursor.execute.call_args[0][0]
    assert "to_regclass('monitored_jobs')" in sql


def test_scoped_job_metrics_only_receives_scoped_jobs(monkeypatch):
    mock_conn, mock_cursor = _mock_conn()
    scoped_rows = [("101", "26-101"), ("102", None)]
    mock_cursor.fetchall.return_value = scoped_rows
    seen = {}

    def fake_fetch(conn, jobs):
        seen["jobs"] = list(jobs)
        return {"101": _metrics(passed=1)}

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", fake_fetch)
    scope = {"job_ids": ["101", "102"], "sc_keys": ["101", "102", "26-101"], "emails": ["a@x.com"]}

    result = _compute_scoped_job_metrics(mock_conn, scope)

    sql, params = _job_read_call(mock_cursor)
    assert "job_id::text = ANY(%s)" in sql
    assert params == [["101", "102"]]
    assert seen["jobs"] == scoped_rows
    assert result == {"101": _metrics(passed=1)}


def _job_read_call(mock_cursor):
    calls = [c[0] for c in mock_cursor.execute.call_args_list
             if "FROM monitored_jobs" in c[0][0]]
    assert len(calls) == 1
    return calls[0]


def test_scoped_job_metrics_unscoped_reads_every_job(monkeypatch):
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchall.return_value = []
    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", lambda conn, jobs: {})

    _compute_scoped_job_metrics(mock_conn, None)

    sql, params = _job_read_call(mock_cursor)
    assert "WHERE TRUE" in sql
    assert params == []


def test_scoped_job_metrics_runs_under_its_own_timeout_then_restores(monkeypatch):
    """The metrics get a dedicated statement_timeout, set before the reads
    and put back afterwards so the later sections (same transaction) keep
    the pool default."""
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = ("30s",)
    mock_cursor.fetchall.return_value = [("101", "26-101")]
    order = []
    mock_cursor.execute.side_effect = lambda sql, *a: order.append(sql)

    def fake_fetch(conn, jobs):
        order.append("<metrics>")
        return {}

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", fake_fetch)
    monkeypatch.setattr(aa, "_METRICS_STATEMENT_TIMEOUT_MS", 12345)

    _compute_scoped_job_metrics(mock_conn, None)

    set_idx = order.index("SET LOCAL statement_timeout = '12345ms'")
    read_idx = next(i for i, s in enumerate(order) if "FROM monitored_jobs" in s)
    metrics_idx = order.index("<metrics>")
    assert set_idx < read_idx < metrics_idx
    assert order[metrics_idx + 1:] == ["SET LOCAL statement_timeout = %s"]
    assert mock_cursor.execute.call_args[0][1] == ("30s",)


def test_scoped_job_metrics_failure_propagates_without_restoring(monkeypatch):
    """On a timeout the transaction is aborted, so a restore would only raise
    a second error; _section's rollback discards the SET LOCAL instead."""
    mock_conn, mock_cursor = _mock_conn()
    mock_cursor.fetchone.return_value = ("30s",)
    mock_cursor.fetchall.return_value = []

    def boom(conn, jobs):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(aa, "fetch_job_candidate_metrics", boom)

    with pytest.raises(RuntimeError):
        _compute_scoped_job_metrics(mock_conn, None)
    assert all("= %s" not in c[0][0] for c in mock_cursor.execute.call_args_list)


def _submission_conn():
    mock_conn, mock_cursor = _mock_conn()
    # counters: complete, pass, pair_external_subs, pair_submits, jobdiva_total
    # then jobdiva_submittals: total, distinct candidates, last 30 days
    mock_cursor.fetchone.side_effect = [(10, 4, 2, 7, 20), (20, 15, 3)]
    mock_cursor.fetchall.return_value = []
    return mock_conn


def test_submission_split_sums_every_scoped_job():
    """The split is summed over all scoped jobs' metrics — the same dict the
    orchestrator computes for every job — not over the timeline's rows."""
    job_metrics = {
        "1": _metrics(pair_internal_submits=1, pair_external_submits=2),
        "2": _metrics(pair_internal_submits=0, pair_external_submits=3),
        "3": _metrics(pair_internal_submits=2, pair_external_submits=0),
    }
    out = _compute_submission_metrics(_submission_conn(), None, job_metrics)
    assert out["pair_internal_submits"] == 3
    assert out["pair_external_submits"] == 5
    # The JobDiva-confirmed counter and the PAIR Submits counter are unchanged.
    assert out["pair_external_subs"] == 2
    assert out["pair_submits"] == 7
    assert out["complete_submissions"] == 10 and out["pass_submissions"] == 4


def test_submission_split_unavailable_is_none_not_zero():
    out = _compute_submission_metrics(_submission_conn(), None, None)
    assert out["pair_internal_submits"] is None
    assert out["pair_external_submits"] is None
    assert out["pair_submits"] == 7


def test_submission_split_with_no_jobs_is_zero():
    out = _compute_submission_metrics(_submission_conn(), None, {})
    assert out["pair_internal_submits"] == 0
    assert out["pair_external_submits"] == 0


# ---------------------------------------------------------------------------
# Orchestration: one metrics call, shared, with a _section fallback
# ---------------------------------------------------------------------------


@pytest.fixture()
def orchestrated(monkeypatch):
    """_compute_analytics_sync over a mock connection, with each section
    replaced by a recorder so the wiring between them is what's under test."""
    mock_conn, _ = _mock_conn()  # the overview block iterates empty results
    monkeypatch.setattr(aa, "get_db_connection", lambda: mock_conn)
    calls = {"metrics": 0}

    def fake_metrics(conn, scope):
        calls["metrics"] += 1
        calls["metrics_scope"] = scope
        if calls.get("metrics_raises"):
            raise RuntimeError("statement timeout")
        return {"101": _metrics(passed=2)}

    def fake_timeline(conn, scope, job_metrics):
        calls["timeline_metrics"] = job_metrics
        return {"rows": [{"job_id": "101"}], "total": 1}

    def fake_submissions(conn, scope, job_metrics):
        calls["submission_metrics"] = job_metrics
        return {"pair_submits": 0}

    monkeypatch.setattr(aa, "_compute_scoped_job_metrics", fake_metrics)
    monkeypatch.setattr(aa, "_compute_jobs_timeline", fake_timeline)
    monkeypatch.setattr(aa, "_compute_submission_metrics", fake_submissions)
    monkeypatch.setattr(aa, "_compute_launch_speed", lambda conn, scope: {"launched_jobs": 1})
    monkeypatch.setattr(aa, "_compute_weekly_trends", lambda conn, scope: {"weeks": []})
    monkeypatch.setattr(aa, "_compute_linkedin_accounts", lambda conn: [])
    return mock_conn, calls


def test_metrics_computed_once_and_shared(orchestrated):
    mock_conn, calls = orchestrated
    data = aa._compute_analytics_sync(None)
    assert calls["metrics"] == 1
    assert calls["timeline_metrics"] == {"101": _metrics(passed=2)}
    assert calls["submission_metrics"] is calls["timeline_metrics"]
    assert data["jobs_timeline"] == [{"job_id": "101"}]
    assert data["jobs_timeline_metrics_available"] is True
    assert "warning" not in data


def test_metrics_failure_falls_back_without_breaking_the_page(orchestrated):
    mock_conn, calls = orchestrated
    calls["metrics_raises"] = True
    data = aa._compute_analytics_sync(None)
    # _section rolled the aborted transaction back and handed None on.
    assert mock_conn.rollback.called
    assert calls["timeline_metrics"] is None
    assert calls["submission_metrics"] is None
    assert data["jobs_timeline"] == [{"job_id": "101"}]
    assert data["launch_speed"] == {"launched_jobs": 1}
    # The page shows a notice and "—" cells rather than zeros.
    assert data["jobs_timeline_metrics_available"] is False
    assert "warning" not in data


def test_metrics_receive_the_team_scope(orchestrated, monkeypatch):
    _, calls = orchestrated
    scope = {"team_id": "t1", "team_name": "Team 1", "emails": ["a@x.com"],
             "job_ids": ["101"], "sc_keys": ["101", "26-101"]}
    monkeypatch.setattr(aa, "_load_team_scope", lambda conn, team_id: scope)
    aa._compute_analytics_sync("t1")
    assert calls["metrics_scope"] is scope
