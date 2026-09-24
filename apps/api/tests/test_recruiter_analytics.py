"""Recruiter Analytics (routers/recruiter_analytics.py, 2026-09-23).

  * RBAC — admin / team lead pinned to own team / recruiter 403 / unknown
    team 404 / a team lead's recruiter filter must stay inside the team.
  * Request validation and the 60s cache.
  * Aggregation — credit by assignment, totals over DISTINCT jobs, JobDiva
    counters MAX-ed within a v1/v2 family, unassigned jobs, email case.
  * The "launched in range" bounds are Eastern midnights, and a job's launch
    is its first SUCCESSFUL audit row — the Launch Report's, pinned by a
    real-Postgres agreement test — not the pre-launch pair_launched_at stamp.
  * The cache key never lets a literal filter value alias "no filter".
  * One real-Postgres run over temp tables (skips when no server is reachable;
    set LAUNCH_REPORT_TEST_DSN, e.g. to a pgserver instance).
"""

import asyncio
import datetime
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from core.auth import UserIdentity
from routers import recruiter_analytics as ra
from services import job_step_time as jst
from services.job_candidate_metrics import empty_metrics

UTC = datetime.timezone.utc
TODAY_ET = lambda: datetime.datetime.now(ra.REPORT_TIMEZONE).date()  # noqa: E731


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    ra.clear_recruiter_analytics_cache()
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    yield
    ra.clear_recruiter_analytics_cache()


def _admin() -> UserIdentity:
    return UserIdentity(email="admin@pyramid.com", role="admin")


def _lead(team_id="team-lead-own") -> UserIdentity:
    return UserIdentity(email="lead@pyramid.com", role="team_lead", team_id=team_id, team_name="Own")


def _recruiter() -> UserIdentity:
    return UserIdentity(email="rec@pyramid.com", role="recruiter", team_id="team-x", team_name="X")


def _call(user, **overrides):
    kwargs = dict(start_date=None, end_date=None, team_id=None, recruiter=None, refresh=False)
    kwargs.update(overrides)
    return asyncio.run(ra.get_recruiter_analytics(user=user, response=None, **kwargs))


def _stub_payload(**extra):
    return {"warnings": [], "recruiters": [], "totals": {}, **extra}


@pytest.fixture()
def compute_calls(monkeypatch):
    calls = []

    def _fake(scope_team_id, recruiter, date_range):
        calls.append((scope_team_id, recruiter, date_range))
        return _stub_payload()

    monkeypatch.setattr(ra, "_compute_recruiter_analytics_sync", _fake)
    return calls


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_every_route_requires_a_signed_in_user():
    # There is no global auth middleware: an endpoint without its own
    # Depends(get_current_user) is open to the internet.
    from core.auth import get_current_user

    assert ra.router.routes
    for route in ra.router.routes:
        assert get_current_user in [d.call for d in route.dependant.dependencies], route.path


def test_admin_sees_everything_by_default(compute_calls):
    res = _call(_admin())
    assert res["status"] == "success"
    assert compute_calls == [(None, None, None)]


def test_admin_can_scope_to_a_team(compute_calls):
    _call(_admin(), team_id="  t-1  ")
    assert compute_calls[0][0] == "t-1"


def test_team_lead_is_pinned_to_own_team_and_team_id_is_ignored(compute_calls):
    _call(_lead(), team_id="someone-elses-team")
    assert compute_calls[0][0] == "team-lead-own"


@pytest.mark.parametrize(
    "user",
    [_recruiter(), UserIdentity(email="lead@pyramid.com", role="team_lead", team_id=None)],
    ids=["recruiter", "team-lead-without-team"],
)
def test_everyone_else_gets_403_before_any_read(compute_calls, user):
    with pytest.raises(HTTPException) as exc:
        _call(user)
    assert exc.value.status_code == 403
    assert compute_calls == []


def test_unknown_team_is_404(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(ra, "get_db_connection", lambda: conn)

    def _missing(_conn, team_id):
        raise LookupError(f"Team '{team_id}' not found.")

    monkeypatch.setattr(ra, "_load_team_scope", _missing)
    with pytest.raises(HTTPException) as exc:
        _call(_admin(), team_id="nope")
    assert exc.value.status_code == 404
    conn.close.assert_called_once()


def test_team_lead_recruiter_filter_outside_team_is_403(monkeypatch, compute_calls):
    monkeypatch.setattr(ra, "get_user_scope_emails", lambda user: {"lead@pyramid.com", "Mate@Pyramid.com"})
    with pytest.raises(HTTPException) as exc:
        _call(_lead(), recruiter="outsider@pyramid.com")
    assert exc.value.status_code == 403
    assert compute_calls == []

    _call(_lead(), recruiter=" MATE@pyramid.com ")
    assert compute_calls == [("team-lead-own", "mate@pyramid.com", None)]


def test_admin_recruiter_filter_is_not_scope_checked(monkeypatch, compute_calls):
    def _boom(_user):
        raise AssertionError("admins bypass the scope check")

    monkeypatch.setattr(ra, "get_user_scope_emails", _boom)
    _call(_admin(), team_id="t-1", recruiter="Anyone@Else.com")
    assert compute_calls == [("t-1", "anyone@else.com", None)]


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-09-01", None),
        (None, "2026-09-01"),
        ("2026-09-02", "2026-09-01"),
        ("2026/09/01", "2026-09-02"),
        ("2026-02-30", "2026-03-01"),
        ("2025-01-01", "2026-01-02"),  # 367 days
    ],
)
def test_bad_ranges_are_400(compute_calls, start, end):
    with pytest.raises(HTTPException) as exc:
        _call(_admin(), start_date=start, end_date=end)
    assert exc.value.status_code == 400
    assert compute_calls == []


def test_max_range_and_today_are_accepted(compute_calls):
    _call(_admin(), start_date="2025-01-01", end_date="2026-01-01")  # 366 days
    today = TODAY_ET()
    _call(_admin(), start_date=today.isoformat(), end_date=today.isoformat())
    assert compute_calls[0][2] == (datetime.date(2025, 1, 1), datetime.date(2026, 1, 1))
    assert compute_calls[1][2] == (today, today)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_hit_refresh_bypass_and_per_scope_keys(compute_calls):
    first = _call(_admin())
    second = _call(_admin())
    assert len(compute_calls) == 1
    assert first["data"]["cached"] is False and second["data"]["cached"] is True

    _call(_admin(), refresh=True)
    assert len(compute_calls) == 2

    _call(_admin(), team_id="t-1")
    _call(_admin(), start_date="2026-09-01", end_date="2026-09-02")
    _call(_admin(), recruiter="a@x.com")
    assert len(compute_calls) == 5


def test_a_literal_star_filter_never_shares_the_unfiltered_entry(compute_calls):
    # The key once spelled "no filter" as "*", so an admin's ?recruiter=* (a
    # payload with every job filtered out) was stored under — and served as —
    # the unfiltered dashboard for the whole TTL, and vice versa.
    assert _call(_admin(), recruiter="*")["data"]["cached"] is False
    assert compute_calls[-1] == (None, "*", None)
    assert _call(_admin())["data"]["cached"] is False
    assert compute_calls[-1] == (None, None, None)
    assert _call(_admin(), recruiter="*")["data"]["cached"] is True
    assert len(compute_calls) == 2
    assert ra._cache_key(None, None, None) != ra._cache_key("*", "*", None)


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _freeze_cache_clock(monkeypatch, clock):
    # Swap only the router's reference: patching time.monotonic globally would
    # also freeze the asyncio loop clock that _call runs on.
    monkeypatch.setattr(ra, "time", SimpleNamespace(monotonic=clock))


def test_healthy_results_expire_after_the_full_ttl(monkeypatch, compute_calls):
    clock = _Clock()
    _freeze_cache_clock(monkeypatch, clock)
    _call(_admin())
    clock.now += ra._CACHE_TTL_SECONDS - 1
    assert _call(_admin())["data"]["cached"] is True
    clock.now += 2
    assert _call(_admin())["data"]["cached"] is False
    assert len(compute_calls) == 2


def test_degraded_results_are_cached_briefly_and_refresh_retries(monkeypatch):
    # The likeliest degradation is the metrics statement timing out on a big
    # population; re-running it on every open would spend its full timeout
    # each time only to return the same fallback.
    assert ra._DEGRADED_CACHE_TTL_SECONDS < ra._CACHE_TTL_SECONDS
    clock = _Clock()
    _freeze_cache_clock(monkeypatch, clock)
    calls = []
    healthy = {"value": False}

    def _fake(*args):
        calls.append(args)
        if healthy["value"]:
            return _stub_payload()
        return _stub_payload(warnings=["Candidate outcome counts are temporarily unavailable"])

    monkeypatch.setattr(ra, "_compute_recruiter_analytics_sync", _fake)
    _call(_admin())
    second = _call(_admin())
    assert len(calls) == 1 and second["data"]["cached"] is True
    assert second["data"]["warnings"]

    # An explicit Refresh always retries the failed section.
    _call(_admin(), refresh=True)
    assert len(calls) == 2

    # The fallback is not served for the healthy result's full minute.
    clock.now += ra._DEGRADED_CACHE_TTL_SECONDS + 1
    healthy["value"] = True
    recovered = _call(_admin())
    assert len(calls) == 3 and recovered["data"]["warnings"] == []

    # The healthy result replaces the degraded entry and keeps the full TTL.
    clock.now += ra._DEGRADED_CACHE_TTL_SECONDS + 1
    assert _call(_admin())["data"]["cached"] is True
    assert len(calls) == 3


def test_compute_failure_is_a_generic_500(monkeypatch):
    def _boom(*_args):
        raise RuntimeError('relation "monitored_jobs" does not exist')

    monkeypatch.setattr(ra, "_compute_recruiter_analytics_sync", _boom)
    with pytest.raises(HTTPException) as exc:
        _call(_admin())
    assert exc.value.status_code == 500
    assert "monitored_jobs" not in exc.value.detail


# ---------------------------------------------------------------------------
# Date range bounds (Eastern midnights)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,lo,hi",
    [
        # EDT (UTC-4)
        ((2026, 9, 21), (2026, 9, 21), (2026, 9, 21, 4), (2026, 9, 22, 4)),
        # EST (UTC-5)
        ((2026, 12, 1), (2026, 12, 31), (2026, 12, 1, 5), (2027, 1, 1, 5)),
        # DST ends on 2026-11-01: starts in EDT, ends in EST
        ((2026, 10, 31), (2026, 11, 1), (2026, 10, 31, 4), (2026, 11, 2, 5)),
    ],
)
def test_range_bounds_are_eastern_midnights(start, end, lo, hi):
    got_lo, got_hi = ra._range_bounds_utc(datetime.date(*start), datetime.date(*end))
    assert got_lo == datetime.datetime(*lo, tzinfo=UTC)
    assert got_hi == datetime.datetime(*hi, tzinfo=UTC)


def test_fetch_job_rows_passes_the_eastern_bounds():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    scope = {"job_ids": ["j1"], "sc_keys": ["j1"], "emails": ["a@x.com"]}

    ra._fetch_job_rows(conn, scope, (datetime.date(2026, 9, 21), datetime.date(2026, 9, 21)))

    main = [c for c in cur.execute.call_args_list if "FROM monitored_jobs" in c.args[0]]
    assert len(main) == 1
    sql, params = main[0].args
    # The Launch Report's first SUCCESSFUL launch, not the pre-launch stamp.
    assert "engage_interview_audit" in sql and "pair_launched_at" not in sql
    assert "l.first_launch_at >= %s AND l.first_launch_at < %s" in sql
    assert params == [
        ra.REPORT_DB_TIMEZONE,  # the CTE's AT TIME ZONE
        ["j1"],  # the launches CTE's scope
        ["j1"],  # the job read's scope
        datetime.datetime(2026, 9, 21, 4, tzinfo=UTC),
        datetime.datetime(2026, 9, 22, 4, tzinfo=UTC),
    ]
    # A pair_posted_by / pair_launched_by column missing from pg_attribute is
    # selected as NULL rather than failing the statement.
    assert "NULL::text" in sql
    assert any("statement_timeout" in c.args[0] for c in cur.execute.call_args_list)


def test_fetch_job_rows_without_range_has_no_launch_filter():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    ra._fetch_job_rows(conn, None, None)
    sql, params = [c for c in cur.execute.call_args_list if "FROM monitored_jobs" in c.args[0]][0].args
    assert "WHERE TRUE\n" in sql or "WHERE TRUE " in sql
    assert "first_launch_at >=" not in sql
    assert "LEFT JOIN launches" in sql  # unlaunched jobs stay in the all-time view
    assert params == [ra.REPORT_DB_TIMEZONE]


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------

T0 = datetime.datetime(2026, 9, 21, 13, 46, 55, tzinfo=UTC)


def _row(job_id, emails, **kw):
    rec = {
        "job_id": job_id,
        "jobdiva_id": kw.pop("jobdiva_id", None),
        "title": kw.pop("title", f"Job {job_id}"),
        "customer_name": kw.pop("customer_name", "Acme"),
        "recruiter_emails": json.dumps(emails) if isinstance(emails, list) else emails,
        "is_archived": kw.pop("is_archived", False),
        "created_at": kw.pop("created_at", None),
        "launched_at": kw.pop("launched_at", T0),
        "candidates_sourced": kw.pop("candidates_sourced", 0),
        "candidates_launched": kw.pop("candidates_launched", 0),
        "pair_external_subs": kw.pop("pair_external_subs", 0),
        "jobdiva_total_subs": kw.pop("jobdiva_total_subs", 0),
        "time_to_first_pass": kw.pop("time_to_first_pass", None),
        "pair_posted_by": kw.pop("pair_posted_by", None),
        "pair_launched_by": kw.pop("pair_launched_by", None),
        "parent_job_id": kw.pop("parent_job_id", None),
        "version": kw.pop("version", 1),
    }
    assert not kw, kw
    return tuple(rec[c] for c in ra._JOB_COLUMNS)


def _metrics(**kw):
    return {**empty_metrics(), **kw}


def _payload(rows, metrics=None, directory=None, scope=None, recruiter=None):
    scope_emails = set(scope["emails"]) if scope else None
    jobs = ra._rows_to_jobs(rows, scope_emails, recruiter)
    return ra._build_payload(
        jobs,
        metrics or {},
        directory or {},
        scope=scope,
        team_scope_out=None,
        date_range=None,
        recruiter=recruiter,
        warnings=[],
    )


def _by_email(payload):
    return {r["email"]: r for r in payload["recruiters"]}


def test_shared_job_is_credited_to_each_recruiter_but_counted_once_in_totals():
    rows = [
        _row("j1", ["Alice@Pyramid.com", "bob@pyramid.com"], candidates_sourced=10, candidates_launched=4),
        _row("j2", ["bob@pyramid.com"], candidates_sourced=5, candidates_launched=2, launched_at=None),
    ]
    metrics = {
        "j1": _metrics(passed=3, failed=1, in_progress=2, feedback_total=3, pair_submits=2,
                       pair_internal_submits=1, pair_external_submits=1, rejects=1, awaiting_feedback=1),
        "j2": _metrics(passed=1, failed=1),
    }
    p = _payload(rows, metrics)
    rec = _by_email(p)

    assert set(rec) == {"alice@pyramid.com", "bob@pyramid.com"}
    assert rec["alice@pyramid.com"]["jobs"] == {"assigned": 1, "active": 1, "archived": 0, "launched": 1}
    assert rec["alice@pyramid.com"]["outcomes"] == {"passed": 3, "failed": 1, "in_progress": 2}
    assert rec["alice@pyramid.com"]["pass_rate"] == 0.75
    assert rec["bob@pyramid.com"]["jobs"]["assigned"] == 2
    assert rec["bob@pyramid.com"]["candidates"] == {"sourced": 15, "launched": 6}
    assert rec["bob@pyramid.com"]["job_ids"] == ["j1", "j2"]

    t = p["totals"]
    # j1 once, not once per recruiter: sum of rows would say 3 jobs / 25 sourced.
    assert t["jobs"]["assigned"] == 2
    assert t["jobs"]["launched"] == 1
    assert t["candidates"]["sourced"] == 15
    assert t["outcomes"]["passed"] == 4
    assert t["feedback"] == {"total": 3, "submits": 2, "internal": 1, "external": 1,
                             "rejects": 1, "unreachable": 0, "awaiting": 1}
    assert t["submittals"]["pair_internal"] == 1 and t["submittals"]["pair_external"] == 1
    assert t["recruiters"] == 2
    # sorted by jobs launched desc, then email
    assert [r["email"] for r in p["recruiters"]] == ["alice@pyramid.com", "bob@pyramid.com"]


def test_jobdiva_counters_take_the_max_within_a_version_family():
    rows = [
        # v1 and its v2 clone re-read the SAME JobDiva job's submittals
        _row("31990001", ["a@x.com"], jobdiva_id="26-29267", pair_external_subs=2, jobdiva_total_subs=5),
        _row("26-29267-v2", ["a@x.com"], jobdiva_id="26-29267-v2", parent_job_id="26-29267",
             version=2, pair_external_subs=1, jobdiva_total_subs=5),
        # v3 without parent_job_id still folds in via the stripped suffix
        _row("26-29267-v3", ["a@x.com"], jobdiva_id="26-29267-V3", version=3, jobdiva_total_subs=6),
        _row("31990002", ["a@x.com"], jobdiva_id="26-00002", pair_external_subs=1, jobdiva_total_subs=1),
    ]
    metrics = {
        "31990001": _metrics(pair_external_submits=1),
        "26-29267-v2": _metrics(pair_external_submits=2),  # candidate metrics are per version: summed
    }
    p = _payload(rows, metrics)
    subs = p["recruiters"][0]["submittals"]
    assert subs["jobdiva_confirmed"] == 2 + 1
    assert subs["jobdiva_total"] == 6 + 1
    assert subs["pair_external"] == 3
    assert p["totals"]["submittals"]["jobdiva_total"] == 7
    assert [j["version"] for j in p["jobs"] if j["job_id"].startswith("26-29267")] == [2, 3]


def test_unassigned_jobs_are_counted_in_totals_but_credited_to_nobody():
    rows = [
        _row("j1", ["a@x.com"]),
        _row("j2", []),
        _row("j3", ""),
        _row("j4", None),
    ]
    p = _payload(rows, {"j2": _metrics(passed=1)})
    assert p["unassigned_jobs"] == 3
    assert p["totals"]["jobs"]["unassigned"] == 3
    assert p["totals"]["jobs"]["assigned"] == 4
    assert p["totals"]["outcomes"]["passed"] == 1
    assert [r["email"] for r in p["recruiters"]] == ["a@x.com"]


def test_email_case_and_whitespace_collapse_to_one_recruiter():
    rows = [
        _row("j1", [" Alice@Pyramid.COM "]),
        _row("j2", ["alice@pyramid.com", "ALICE@pyramid.com"]),
    ]
    p = _payload(rows)
    assert [r["email"] for r in p["recruiters"]] == ["alice@pyramid.com"]
    assert p["recruiters"][0]["jobs"]["assigned"] == 2
    assert p["jobs"][0]["recruiter_emails"] == ["alice@pyramid.com"]


def test_team_scope_hides_outside_recruiters():
    scope = {"team_id": "t1", "team_name": "East", "emails": ["a@x.com"], "job_ids": ["j1"], "sc_keys": ["j1"]}
    rows = [_row("j1", ["a@x.com", "outsider@x.com"]), _row("j9", ["outsider@x.com"])]
    p = _payload(rows, scope=scope)
    assert [r["email"] for r in p["recruiters"]] == ["a@x.com"]
    assert p["recruiters"][0]["team_name"] == "East"
    assert [j["job_id"] for j in p["jobs"]] == ["j1"]
    assert p["jobs"][0]["recruiter_emails"] == ["a@x.com"]


def test_recruiter_filter_gives_one_row_and_only_their_jobs():
    rows = [_row("j1", ["a@x.com", "b@x.com"]), _row("j2", ["b@x.com"])]
    p = _payload(rows, recruiter="a@x.com")
    assert [r["email"] for r in p["recruiters"]] == ["a@x.com"]
    assert [j["job_id"] for j in p["jobs"]] == ["j1"]
    assert p["totals"]["jobs"]["assigned"] == 1


def test_has_pair_account_team_name_and_attribution(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Pyramid.com")
    rows = [
        _row("j1", ["member@x.com", "boss@pyramid.com", "poster@x.com", "am@client.com"],
             pair_posted_by="Poster@X.com", pair_launched_by="unauthenticated@hoonr.ai"),
        _row("j2", ["role@x.com"]),
    ]
    directory = {
        "team_by_email": {"member@x.com": {"team_name": "East", "is_team_lead": True}},
        "accounts": {"member@x.com", "role@x.com"},
    }
    p = _payload(rows, directory=directory)
    rec = _by_email(p)
    assert rec["member@x.com"]["has_pair_account"] is True
    assert rec["member@x.com"]["team_name"] == "East"
    assert rec["member@x.com"]["is_team_lead"] is True
    assert rec["boss@pyramid.com"]["has_pair_account"] is True   # ADMIN_EMAILS
    assert rec["poster@x.com"]["has_pair_account"] is True       # posted a job in PAIR
    assert rec["role@x.com"]["has_pair_account"] is True         # user_roles
    assert rec["am@client.com"]["has_pair_account"] is False     # JobDiva contact field
    job = [j for j in p["jobs"] if j["job_id"] == "j1"][0]
    assert job["posted_by"] == "poster@x.com"
    assert job["launched_by"] is None  # the dev fallback identity is not a person


def test_times_first_external_and_time_to_first_pass():
    rows = [
        _row("j1", ["a@x.com"], time_to_first_pass=30.0, created_at=T0),
        _row("j2", ["a@x.com"], time_to_first_pass=90.0, launched_at=None),
        _row("j3", ["a@x.com"], time_to_first_pass=-5.0),  # clock disagreement: ignored
    ]
    metrics = {
        "j1": _metrics(first_external_submit_at=datetime.datetime(2026, 9, 21, 16, tzinfo=UTC)),
        "j2": _metrics(first_external_submit_at=datetime.datetime(2026, 9, 21, 15, tzinfo=UTC)),
    }
    p = _payload(rows, metrics)
    rec = p["recruiters"][0]
    assert rec["first_external_submit_at"] == "2026-09-21T11:00:00-04:00"
    assert rec["avg_time_to_first_pass_minutes"] == 60.0
    assert rec["pass_rate"] is None  # nothing decided yet
    j1 = [j for j in p["jobs"] if j["job_id"] == "j1"][0]
    assert j1["launched_at"] == "2026-09-21T09:46:55-04:00"
    assert j1["created_at"] == "2026-09-21T09:46:55-04:00"
    assert j1["time_to_first_pass_minutes"] == 30.0
    # launched jobs first (newest first), unlaunched last
    assert p["jobs"][-1]["job_id"] == "j2"


def test_duplicate_job_rows_are_read_once():
    rows = [_row("j1", ["a@x.com"], candidates_sourced=3), _row("j1", ["a@x.com"], candidates_sourced=99)]
    p = _payload(rows)
    assert p["totals"]["candidates"]["sourced"] == 3


# ---------------------------------------------------------------------------
# Compute with a mocked connection and a mocked metrics call
# ---------------------------------------------------------------------------


def test_compute_makes_one_metrics_call_for_the_whole_population(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(ra, "get_db_connection", lambda: conn)
    monkeypatch.setattr(ra, "_fetch_job_rows", lambda _c, _s, _r: [
        _row("31990001", ["a@x.com"], jobdiva_id="26-29267"),
        _row("31990002", ["b@x.com"], jobdiva_id="26-00002"),
        _row("EXT-1", []),
    ])
    monkeypatch.setattr(ra, "_load_directory", lambda _c: {"team_by_email": {}, "accounts": {"a@x.com"}})
    calls = []

    def _metrics_call(c, pairs):
        calls.append(list(pairs))
        return {"31990001": _metrics(passed=2, failed=2), "31990002": _metrics(passed=1)}

    monkeypatch.setattr(ra, "fetch_job_candidate_metrics", _metrics_call)
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    assert calls == [[("31990001", "26-29267"), ("31990002", "26-00002"), ("EXT-1", None)]]
    assert p["totals"]["outcomes"]["passed"] == 3
    assert p["totals"]["pass_rate"] == 0.6
    assert p["metrics_available"] is True and p["warnings"] == []
    assert p["range"]["mode"] == "all_time"
    conn.close.assert_called_once()


def test_metrics_failure_degrades_with_a_warning(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(ra, "get_db_connection", lambda: conn)
    monkeypatch.setattr(ra, "_fetch_job_rows", lambda _c, _s, _r: [_row("j1", ["a@x.com"], candidates_sourced=4)])
    monkeypatch.setattr(ra, "_load_directory", lambda _c: {"team_by_email": {}, "accounts": set()})

    def _timeout(_c, _pairs):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(ra, "fetch_job_candidate_metrics", _timeout)
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    conn.rollback.assert_called()
    assert p["metrics_available"] is False
    assert len(p["warnings"]) == 1 and "statement timeout" not in p["warnings"][0]
    assert p["totals"]["candidates"]["sourced"] == 4  # the job counters still come through


def test_directory_failure_makes_the_login_flag_unknown_not_false(monkeypatch):
    conn = MagicMock()
    monkeypatch.setattr(ra, "get_db_connection", lambda: conn)
    monkeypatch.setattr(ra, "_fetch_job_rows", lambda _c, _s, _r: [
        _row("j1", ["a@x.com", "b@x.com"], pair_launched_by="b@x.com"),
    ])
    monkeypatch.setattr(ra, "fetch_job_candidate_metrics", lambda _c, _p: {})

    def _no_tables(_c):
        raise RuntimeError('relation "team_members" does not exist')

    monkeypatch.setattr(ra, "_load_directory", _no_tables)
    p = ra._compute_recruiter_analytics_sync(None, None, None)

    rec = _by_email(p)
    assert rec["a@x.com"]["has_pair_account"] is None  # unknown, so the UI does not hide them
    assert rec["b@x.com"]["has_pair_account"] is True  # launched a job in PAIR
    assert p["metrics_available"] is True and len(p["warnings"]) == 1


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_SCHEMA_SQL = """
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT PRIMARY KEY, jobdiva_id TEXT, title TEXT, enhanced_title TEXT, customer_name TEXT,
    recruiter_emails TEXT, is_archived BOOLEAN DEFAULT FALSE, created_at TEXT,
    pair_launched_at TIMESTAMP, candidates_sourced INTEGER DEFAULT 0, candidates_launched INTEGER DEFAULT 0,
    pair_external_subs INTEGER DEFAULT 0, jobdiva_total_subs INTEGER DEFAULT 0,
    time_to_first_pass DOUBLE PRECISION, pair_posted_by TEXT, pair_launched_by TEXT,
    parent_job_id TEXT, version INTEGER, posted_date TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    data JSONB, status TEXT DEFAULT 'sourced', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
CREATE TEMP TABLE teams (id TEXT PRIMARY KEY, name TEXT NOT NULL) ON COMMIT DROP;
CREATE TEMP TABLE team_members (
    id SERIAL PRIMARY KEY, team_id TEXT NOT NULL, email TEXT NOT NULL, member_role TEXT NOT NULL DEFAULT 'member'
) ON COMMIT DROP;
CREATE TEMP TABLE user_roles (email TEXT PRIMARY KEY, role TEXT) ON COMMIT DROP;
"""


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
            # The router reads Step 5 time too; a missing table would fail
            # that section and its rollback would drop every temp table above.
            # The shipped DDL, as a temp table (tests/test_job_step_time.py).
            cur.execute(jst.SCHEMA_STATEMENTS[0].replace("CREATE TABLE", "CREATE TEMP TABLE") + " ON COMMIT DROP")
        _seed(conn)

        class _Borrowed:
            """The router closes its connection; the fixture owns this one."""

            def __getattr__(self, name):
                return getattr(conn, name)

            def close(self):
                pass

        monkeypatch.setattr(ra, "get_db_connection", lambda: _Borrowed())
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _job(cur, job_id, jobdiva_id, emails, launched, **kw):
    cols = {
        "job_id": job_id, "jobdiva_id": jobdiva_id, "title": kw.get("title", f"Job {job_id}"),
        "customer_name": "Acme", "recruiter_emails": json.dumps(emails),
        "created_at": kw.get("created_at"), "pair_launched_at": launched,
        "candidates_sourced": kw.get("sourced", 0), "candidates_launched": kw.get("launched_count", 0),
        "pair_external_subs": kw.get("confirmed", 0), "jobdiva_total_subs": kw.get("jd_total", 0),
        "time_to_first_pass": kw.get("ttfp"), "pair_posted_by": kw.get("posted_by"),
        "pair_launched_by": kw.get("launched_by"), "parent_job_id": kw.get("parent"),
        "version": kw.get("version", 1),
    }
    cur.execute(
        f"INSERT INTO monitored_jobs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        list(cols.values()),
    )


def _audit(cur, key, at, interview_id="iv", candidate_id="c"):
    """One /engage/launch audit row; interview_id '' or None = a failed attempt."""
    cur.execute(
        "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (candidate_id, key, interview_id, at),
    )


def _cand(cur, key, cid, data, source="LinkedIn"):
    cur.execute(
        "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES (%s, %s, %s, %s)",
        (key, cid, source, json.dumps(data)),
    )


def _seed(conn):
    with conn.cursor() as cur:
        # A job's launch is its first SUCCESSFUL engage_interview_audit row
        # (the Launch Report's rule); pair_launched_at is what /candidates/save
        # stamped before pair-bot answered, and must not decide anything.
        #
        # 09:46:55 EDT on 09/21, the report example. created_at is the
        # readable_ist_now() text for the same instant (19:16:55 IST). The
        # first attempt, on 09/20, failed; that is the day pair_launched_at
        # carries, and the job still belongs to 09/21.
        _job(cur, "31990001", "26-29267", ["Alice@Pyramid.com", "bob@pyramid.com"], "2026-09-20 15:00:00",
             created_at="2026-09-21 19:16:55 IST", sourced=12, launched_count=5, confirmed=1, jd_total=3,
             ttfp=40.0, posted_by="alice@pyramid.com", launched_by="bob@pyramid.com")
        _audit(cur, "26-29267", "2026-09-20 15:00:00", interview_id="")
        _audit(cur, "26-29267", "2026-09-21 13:46:55")
        _audit(cur, "31990001", "2026-09-21 18:00:00")  # the job_id key, later
        _job(cur, "26-29267-v2", "26-29267-v2", ["alice@pyramid.com", "bob@pyramid.com"], "2026-09-21 20:00:00",
             parent="26-29267", version=2, sourced=4, launched_count=2, confirmed=1, jd_total=3, ttfp=20.0)
        _audit(cur, "26-29267-v2", "2026-09-21 20:00:00")
        # 23:59:59 EDT on 09/21: still in range
        _job(cur, "31990002", "26-00002", ["bob@pyramid.com", "am@client.com"], "2026-09-22 03:59:59",
             sourced=6, launched_count=3, jd_total=2)
        _audit(cur, "26-00002", "2026-09-22 03:59:59")
        _audit(cur, "26-00002", "2026-09-25 10:00:00")  # a later re-launch changes nothing
        # 00:00:00 EDT on 09/22 and 23:59:59 EDT on 09/20: out of range
        _job(cur, "31990003", "26-00003", ["alice@pyramid.com"], "2026-09-22 04:00:00", sourced=100)
        _audit(cur, "26-00003", "2026-09-22 04:00:00")
        _job(cur, "31990004", "26-00004", ["alice@pyramid.com"], "2026-09-21 03:59:59", sourced=100)
        _audit(cur, "26-00004", "2026-09-21 03:59:59")
        _job(cur, "EXT-5", None, [], "2026-09-21 12:00:00", sourced=1)  # unassigned, in range
        _audit(cur, "EXT-5", "2026-09-21 12:00:00")  # no JobDiva ref: keyed on job_id
        _job(cur, "31990006", "26-00006", ["alice@pyramid.com"], None, sourced=7)  # never launched
        # Every attempt failed (pair-bot 5xx): stamped, never launched.
        _job(cur, "31990007", "26-00007", ["bob@pyramid.com"], "2026-09-21 14:00:00", sourced=2,
             launched_by="bob@pyramid.com")
        _audit(cur, "26-00007", "2026-09-21 14:00:00", interview_id="")
        _audit(cur, "26-00007", "2026-09-21 14:05:00", interview_id=None)
        _audit(cur, "26-00007", "2026-09-21 14:10:00", candidate_id="")
        # A '' JobDiva ref must not pick up an audit row that also has ''.
        _job(cur, "31990008", "", ["bob@pyramid.com"], None, sourced=3)
        _audit(cur, "", "2026-09-21 15:00:00")

        t = lambda h: f"2026-09-21T{h:02d}:00:00+00:00"  # noqa: E731
        _cand(cur, "26-29267", "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": t(15)})
        _cand(cur, "31990001", "c1", {"engage_status": "passed", "feedback_type": "Submit", "feedback_at": t(15)},
              source="JobDiva")  # same person under the numeric key: counted once
        _cand(cur, "26-29267", "c2", {"engage_status": "completed", "engage_hard_filter_status": "fail"})
        _cand(cur, "26-29267", "c3", {"engage_status": "passed"})  # awaiting feedback
        _cand(cur, "26-29267-v2", "c4", {"engage_status": "passed", "feedback_type": "Submit",
                                         "submission_type": "internal", "feedback_at": t(18)})
        _cand(cur, "26-00002", "c5", {"engage_status": "failed", "engage_score": 40, "feedback_type": "Reject",
                                      "feedback_at": t(19)})
        _cand(cur, "EXT-5", "c6", {"engage_status": "in_progress"})

        cur.execute("INSERT INTO teams VALUES ('t-east', 'East')")
        cur.execute("INSERT INTO team_members (team_id, email, member_role) VALUES ('t-east', 'ALICE@pyramid.com', 'lead')")


def test_postgres_happy_path_launched_in_range(pg):
    p = ra._compute_recruiter_analytics_sync(None, None, (datetime.date(2026, 9, 21), datetime.date(2026, 9, 21)))

    assert p["warnings"] == [] and p["metrics_available"] is True
    assert p["range"]["mode"] == "launched_in_range"
    assert sorted(j["job_id"] for j in p["jobs"]) == ["26-29267-v2", "31990001", "31990002", "EXT-5"]
    assert p["unassigned_jobs"] == 1

    rec = _by_email(p)
    assert [r["email"] for r in p["recruiters"]] == ["bob@pyramid.com", "alice@pyramid.com", "am@client.com"]

    alice = rec["alice@pyramid.com"]
    assert alice["jobs"] == {"assigned": 2, "active": 2, "archived": 0, "launched": 2}
    assert alice["outcomes"] == {"passed": 3, "failed": 1, "in_progress": 0}  # c1 c3 + c4; c2
    assert alice["pass_rate"] == 0.75
    assert alice["feedback"] == {"total": 2, "submits": 2, "internal": 1, "external": 1,
                                 "rejects": 0, "unreachable": 0, "awaiting": 2}  # c2, c3
    assert alice["submittals"] == {"pair_internal": 1, "pair_external": 1,
                                   "jobdiva_confirmed": 1, "jobdiva_total": 3}  # v1/v2: one family
    assert alice["first_external_submit_at"] == "2026-09-21T11:00:00-04:00"
    assert alice["avg_time_to_first_pass_minutes"] == 30.0
    assert alice["team_name"] == "East" and alice["is_team_lead"] is True
    assert alice["has_pair_account"] is True

    bob = rec["bob@pyramid.com"]
    assert bob["jobs"]["launched"] == 3
    assert bob["outcomes"]["failed"] == 2
    assert bob["feedback"]["rejects"] == 1
    assert bob["submittals"]["jobdiva_total"] == 3 + 2
    assert bob["has_pair_account"] is True  # launched job 31990001 in PAIR
    assert rec["am@client.com"]["has_pair_account"] is False

    t = p["totals"]
    assert t["jobs"] == {"assigned": 4, "active": 4, "archived": 0, "launched": 4, "unassigned": 1}
    assert t["candidates"] == {"sourced": 12 + 4 + 6 + 1, "launched": 5 + 2 + 3}
    assert t["outcomes"] == {"passed": 3, "failed": 2, "in_progress": 1}
    assert t["submittals"]["jobdiva_total"] == 5  # not 3 + 5 + 2 from the recruiter rows
    assert t["recruiters"] == 3

    j1 = [j for j in p["jobs"] if j["job_id"] == "31990001"][0]
    assert j1["launched_at"] == "2026-09-21T09:46:55-04:00"
    assert j1["created_at"] == "2026-09-21T09:46:55-04:00"  # the IST text, not +5:30
    assert j1["posted_by"] == "alice@pyramid.com" and j1["launched_by"] == "bob@pyramid.com"
    assert j1["recruiter_emails"] == ["alice@pyramid.com", "bob@pyramid.com"]
    v2 = [j for j in p["jobs"] if j["job_id"] == "26-29267-v2"][0]
    assert v2["posted_by"] is None and v2["launched_by"] is None  # not recorded → "—" in the UI


def test_postgres_all_time_includes_unlaunched_jobs(pg):
    p = ra._compute_recruiter_analytics_sync(None, None, None)
    assert p["totals"]["jobs"]["assigned"] == 9
    assert p["totals"]["jobs"]["launched"] == 6  # not 31990006/7/8
    alice = _by_email(p)["alice@pyramid.com"]
    assert alice["jobs"]["assigned"] == 5 and alice["jobs"]["launched"] == 4
    bob = _by_email(p)["bob@pyramid.com"]
    assert bob["jobs"]["assigned"] == 5 and bob["jobs"]["launched"] == 3
    jobs = {j["job_id"]: j for j in p["jobs"]}
    # Failed-only: pair_launched_at and pair_launched_by were stamped, but it
    # never launched, so it is not "Launched" here nor on the Launch Report.
    assert jobs["31990007"]["launched_at"] is None
    assert jobs["31990008"]["launched_at"] is None


@pytest.mark.parametrize(
    "start,end",
    [((2026, 9, 21), (2026, 9, 21)), ((2026, 9, 20), (2026, 9, 22)), ((2026, 9, 1), (2026, 9, 30))],
)
def test_postgres_launch_matches_the_launch_report(pg, start, end):
    # The same jobs, launched at the same instant, as the Launch Report's
    # first-successful-launch CTE — the definition this router restates.
    from routers import launch_report as lr

    lo, hi = datetime.date(*start), datetime.date(*end)
    ours = ra._compute_recruiter_analytics_sync(None, None, (lo, hi))
    theirs = lr._fetch_jobs_launched_on(pg, lo, None, hi)
    db_tz = lr._DB_TZ
    assert {j["job_id"]: datetime.datetime.fromisoformat(j["launched_at"]) for j in ours["jobs"]} == {
        r["job_id"]: r["first_launch_at"].replace(tzinfo=db_tz) for r in theirs
    }
    assert ours["totals"]["jobs"]["launched"] == len(theirs)


def test_postgres_range_is_independent_of_session_timezone(pg):
    with pg.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Kolkata'")
    p = ra._compute_recruiter_analytics_sync(None, None, (datetime.date(2026, 9, 21), datetime.date(2026, 9, 21)))
    assert sorted(j["job_id"] for j in p["jobs"]) == ["26-29267-v2", "31990001", "31990002", "EXT-5"]
    j1 = [j for j in p["jobs"] if j["job_id"] == "31990001"][0]
    assert j1["created_at"] == "2026-09-21T09:46:55-04:00"


def test_postgres_team_scope_through_the_endpoint(pg, monkeypatch):
    from services import teams_db

    monkeypatch.setattr(teams_db, "get_team", lambda team_id: {
        "id": team_id, "name": "East", "lead_emails": ["alice@pyramid.com"], "member_emails": [],
    } if team_id == "t-east" else None)

    res = _call(_lead("t-east"), team_id="ignored", start_date="2026-09-21", end_date="2026-09-21")
    data = res["data"]
    assert data["team_scope"] == {"team_id": "t-east", "team_name": "East", "member_count": 1}
    assert [r["email"] for r in data["recruiters"]] == ["alice@pyramid.com"]
    assert sorted(j["job_id"] for j in data["jobs"]) == ["26-29267-v2", "31990001"]
    assert all(j["recruiter_emails"] == ["alice@pyramid.com"] for j in data["jobs"])  # bob hidden
    assert data["totals"]["jobs"]["unassigned"] == 0

    with pytest.raises(HTTPException) as exc:
        _call(_admin(), team_id="missing")
    assert exc.value.status_code == 404


def test_postgres_team_with_no_jobs_is_empty_not_an_error(pg, monkeypatch):
    from services import teams_db

    monkeypatch.setattr(teams_db, "get_team", lambda team_id: {
        "id": team_id, "name": "New", "lead_emails": ["nobody@pyramid.com"], "member_emails": [],
    })
    p = ra._compute_recruiter_analytics_sync("t-new", None, None)
    assert p["warnings"] == []
    assert p["recruiters"] == [] and p["jobs"] == []
    assert p["totals"]["jobs"]["assigned"] == 0 and p["totals"]["pass_rate"] is None
