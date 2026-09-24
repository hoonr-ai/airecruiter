"""routers/job_step_time: the endpoint the job wizard reports Step 5 active
time to (POST /api/v1/jobs/{job_id}/step-time).

It is telemetry running underneath the wizard, so beyond the auth guard these
tests pin the "never break the wizard" contract: unknown jobs and non-person
identities are ignored, and a DB failure, in the access check or in the write,
answers 200 {"status": "error"}, not a 500 or a 403. The client retries
"error" and drops a 403, so only a real denial may be a 403. Most tests mock
the connection. The Postgres test (skips without LAUNCH_REPORT_TEST_DSN) runs
the real resolve + upsert against temp tables.
"""

import ast
import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.auth import UserIdentity, get_current_user
from routers import job_step_time as router_mod
from services import job_step_time as jst

API_ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = API_ROOT / "routers" / "job_step_time.py"

RECRUITER = UserIdentity(email="Priya.Recruiter@Example.com ", role="recruiter")
ADMIN = UserIdentity(email="admin@example.com", role="admin")
# verify_job_access compares user.email as given (auth hands it over
# normalised), so the access tests use the normalised form.
ASSIGNED = UserIdentity(email="priya.recruiter@example.com", role="recruiter")


def _post(job_id, step=5, active_ms=30_000, user=RECRUITER):
    return asyncio.run(
        router_mod.record_job_step_time(
            job_id=job_id,
            report=router_mod.StepTimeReport(step=step, active_ms=active_ms),
            user=user,
        )
    )


def _conn_returning(row):
    """A mocked pooled connection whose resolve SELECT returns ``row``."""
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _draft(recruiter_emails):
    """What routers.jobs._get_job_draft_sync returns for a saved job."""
    return {"status": "success", "data": {"job_id": "26-29267", "recruiter_emails": recruiter_emails}}


NOT_SAVED = {"status": "error", "message": "No data found for job 26-29267"}


@pytest.fixture()
def allow_access():
    with patch.object(router_mod, "_check_access_sync") as check:
        yield check


# ---------------------------------------------------------------------------
# Route shape and guards
# ---------------------------------------------------------------------------


def _route():
    matches = [r for r in router_mod.router.routes if getattr(r, "path", "") == "/api/v1/jobs/{job_id}/step-time"]
    assert len(matches) == 1, "POST /api/v1/jobs/{job_id}/step-time is not registered"
    return matches[0]


def test_route_requires_authentication():
    route = _route()
    assert route.methods == {"POST"}
    calls = [d.call for d in route.dependant.dependencies]
    assert get_current_user in calls, (
        "record_job_step_time must take `user: UserIdentity = Depends(get_current_user)`; "
        "there is no global auth middleware, so without it the endpoint is open to anyone"
    )


def _names_in(func_name):
    tree = ast.parse(ROUTER_PATH.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name
    )
    return {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}


def test_route_verifies_job_access():
    """Authentication alone is not enough: the job must be one the user can
    open. The handler checks it through _check_access_sync, which applies the
    shared rule (core.auth.verify_job_access) to the job the shared loader
    returns, the same pair routers.jobs._verify_job_access_by_id uses."""
    assert "_check_access_sync" in _names_in("record_job_step_time")
    assert {"verify_job_access", "_get_job_draft_sync"} <= _names_in("_check_access_sync")


def test_route_lives_under_the_api_passthrough():
    """nginx forwards /jobs/{id}/<subpath> only for an allowlist of subpaths
    (nginx-app-locations.conf); /api/ is a passthrough. Moving this route out
    from under /api/ would 404 it as a Next.js page in QA/PROD, and the client
    swallows errors, so the reports would silently get no data."""
    assert _route().path.startswith("/api/")


def test_main_mounts_the_router():
    src = (API_ROOT / "main.py").read_text(encoding="utf-8")
    assert '_safe_import("job_step_time")' in src
    assert "_mount(job_step_time_router" in src


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", [0, 6, -1])
def test_step_outside_the_wizard_is_rejected(step):
    with pytest.raises(ValidationError):
        router_mod.StepTimeReport(step=step, active_ms=1000)


def test_negative_active_ms_is_rejected_but_oversized_is_accepted():
    with pytest.raises(ValidationError):
        router_mod.StepTimeReport(step=5, active_ms=-1)
    # Oversized reports are clamped by record_step_time, not rejected (a
    # rejection would make the client retry the same chunk forever).
    assert router_mod.StepTimeReport(step=5, active_ms=10**12).active_ms == 10**12


def _client(user=RECRUITER):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_invalid_bodies_answer_422_over_http(allow_access):
    client = _client()
    url = "/api/v1/jobs/26-29267/step-time"
    assert client.post(url, json={"step": 9, "active_ms": 1000}).status_code == 422
    assert client.post(url, json={"step": 5}).status_code == 422
    assert client.post(url, json={"step": 5, "active_ms": "lots"}).status_code == 422
    allow_access.assert_not_called()


def test_access_denied_propagates_as_403():
    client = _client(ASSIGNED)
    with patch.object(
        router_mod, "_get_job_draft_sync", return_value=_draft(["someone.else@example.com"])
    ), patch.object(router_mod, "get_db_connection") as get_conn:
        res = client.post("/api/v1/jobs/26-29267/step-time", json={"step": 5, "active_ms": 1000})
    assert res.status_code == 403
    get_conn.assert_not_called()


def test_an_unrecognised_lookup_result_is_denied():
    """The shape _verify_job_access_by_id also refuses: deny, don't retry."""
    client = _client(ASSIGNED)
    with patch.object(
        router_mod, "_get_job_draft_sync", return_value={"status": "success", "data": None}
    ), patch.object(router_mod, "get_db_connection") as get_conn:
        res = client.post("/api/v1/jobs/26-29267/step-time", json={"step": 5, "active_ms": 1000})
    assert res.status_code == 403
    get_conn.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("connection pool exhausted"), TimeoutError("connect timeout"), OSError("db down")],
)
def test_access_lookup_failure_answers_error_not_403(failure):
    """_verify_job_access_by_id would turn this into a 403, which the client
    treats as final and drops. A DB blip has to come back as "error" so the
    pending time is retried, and nothing may be written."""
    client = _client(ASSIGNED)
    with patch.object(router_mod, "_get_job_draft_sync", side_effect=failure), patch.object(
        router_mod, "get_db_connection"
    ) as get_conn:
        res = client.post("/api/v1/jobs/26-29267/step-time", json={"step": 5, "active_ms": 1000})
    assert res.status_code == 200
    assert res.json() == {"status": "error"}
    get_conn.assert_not_called()


def test_an_assigned_recruiter_is_recorded():
    conn, _ = _conn_returning(("31920032",))
    with patch.object(
        router_mod, "_get_job_draft_sync", return_value=_draft(["Priya.Recruiter@example.com"])
    ) as load, patch.object(router_mod, "get_db_connection", return_value=conn), patch.object(
        router_mod, "record_step_time"
    ) as record:
        assert _post("26-29267", user=ASSIGNED) == {"status": "success"}
    load.assert_called_once_with("26-29267")
    record.assert_called_once_with(conn, "31920032", 5, "priya.recruiter@example.com", 30_000)


def test_an_unassigned_job_is_open_to_any_recruiter():
    """verify_job_access's legacy rule: no recruiters assigned, anyone may work it."""
    conn, _ = _conn_returning(("31920032",))
    with patch.object(router_mod, "_get_job_draft_sync", return_value=_draft([])), patch.object(
        router_mod, "get_db_connection", return_value=conn
    ), patch.object(router_mod, "record_step_time") as record:
        assert _post("26-29267", user=ASSIGNED) == {"status": "success"}
    record.assert_called_once()


def test_a_job_not_saved_yet_passes_the_check_and_is_ignored():
    """allow_not_found: a job not saved yet must not 403 the wizard. The
    resolve finds no row, so nothing is recorded."""
    conn, _ = _conn_returning(None)
    with patch.object(router_mod, "_get_job_draft_sync", return_value=NOT_SAVED), patch.object(
        router_mod, "get_db_connection", return_value=conn
    ), patch.object(router_mod, "record_step_time") as record:
        assert _post("26-29267", user=ASSIGNED) == {"status": "ignored"}
    record.assert_not_called()


def test_admins_skip_the_lookup():
    conn, _ = _conn_returning(("31920032",))
    with patch.object(router_mod, "_get_job_draft_sync") as load, patch.object(
        router_mod, "get_db_connection", return_value=conn
    ), patch.object(router_mod, "record_step_time"):
        assert _post("26-29267", user=ADMIN) == {"status": "success"}
    load.assert_not_called()


# ---------------------------------------------------------------------------
# Behaviour (mocked connection)
# ---------------------------------------------------------------------------


def test_happy_path_records_under_the_canonical_job_id(allow_access):
    conn, cur = _conn_returning((31920032,))
    with patch.object(router_mod, "get_db_connection", return_value=conn), patch.object(
        router_mod, "record_step_time"
    ) as record:
        assert _post("26-29267", step=5, active_ms=45_000) == {"status": "success"}

    # Resolved by either identifier the page may hold.
    sql, params = cur.execute.call_args_list[-1].args
    assert "monitored_jobs" in sql and params == ("26-29267", "26-29267")
    # Keyed by the canonical monitored_jobs.job_id (as text), the email
    # normalised the way the reports' by_user keys are.
    record.assert_called_once_with(conn, "31920032", 5, "priya.recruiter@example.com", 45_000)
    allow_access.assert_called_once_with("26-29267", RECRUITER)
    conn.close.assert_called_once()


def test_entry_report_with_zero_ms_is_recorded(allow_access):
    conn, _ = _conn_returning(("31920032",))
    with patch.object(router_mod, "get_db_connection", return_value=conn), patch.object(
        router_mod, "record_step_time"
    ) as record:
        assert _post("31920032", active_ms=0) == {"status": "success"}
    record.assert_called_once_with(conn, "31920032", 5, "priya.recruiter@example.com", 0)


def test_unknown_job_is_ignored(allow_access):
    conn, _ = _conn_returning(None)
    with patch.object(router_mod, "get_db_connection", return_value=conn), patch.object(
        router_mod, "record_step_time"
    ) as record:
        assert _post("26-00000") == {"status": "ignored"}
    record.assert_not_called()
    conn.close.assert_called_once()


@pytest.mark.parametrize("email", ["unauthenticated@hoonr.ai", "", "   "])
def test_identity_that_is_not_a_person_is_ignored(allow_access, email):
    with patch.object(router_mod, "get_db_connection") as get_conn:
        assert _post("26-29267", user=UserIdentity(email=email, role="admin")) == {"status": "ignored"}
    get_conn.assert_not_called()


def test_blank_job_id_is_ignored(allow_access):
    with patch.object(router_mod, "get_db_connection") as get_conn:
        assert _post("   ") == {"status": "ignored"}
    get_conn.assert_not_called()


def test_db_failure_on_connect_answers_error_not_500(allow_access):
    with patch.object(router_mod, "get_db_connection", side_effect=RuntimeError("pool exhausted")):
        assert _post("26-29267") == {"status": "error"}


def test_db_failure_on_write_answers_error_and_returns_the_connection(allow_access):
    conn, _ = _conn_returning(("31920032",))
    with patch.object(router_mod, "get_db_connection", return_value=conn), patch.object(
        router_mod, "record_step_time", side_effect=RuntimeError("lock timeout")
    ):
        assert _post("26-29267") == {"status": "error"}
    conn.close.assert_called_once()


def test_db_failure_over_http_is_200(allow_access):
    client = _client()
    with patch.object(router_mod, "get_db_connection", side_effect=RuntimeError("db down")):
        res = client.post("/api/v1/jobs/26-29267/step-time", json={"step": 5, "active_ms": 1000})
    assert res.status_code == 200
    assert res.json() == {"status": "error"}


# ---------------------------------------------------------------------------
# Real Postgres: resolve + upsert end to end
# ---------------------------------------------------------------------------

psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(jst.SCHEMA_STATEMENTS[0].replace("CREATE TABLE", "CREATE TEMP TABLE") + " ON COMMIT DROP")
            cur.execute("CREATE TEMP TABLE monitored_jobs (job_id TEXT PRIMARY KEY, jobdiva_id TEXT) ON COMMIT DROP")
            cur.execute("INSERT INTO monitored_jobs VALUES ('31920032', '26-29267'), ('-4', NULL)")
            # fetch_step_metrics also reads launches; none here.
            cur.execute(
                "CREATE TEMP TABLE engage_interview_audit (candidate_id TEXT, jobdiva_id TEXT, "
                "interview_id TEXT, created_at TIMESTAMP) ON COMMIT DROP"
            )
        yield conn
    finally:
        conn.rollback()
        conn.close()


class _Borrowed:
    """What get_db_connection hands out, minus the commit and close: the
    temp tables must outlive record_step_time's commit and the endpoint's
    close, and the fixture's rollback cleans up."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        pass

    def close(self):
        pass


def test_reports_accumulate_on_the_canonical_row(pg):
    with patch.object(router_mod, "get_db_connection", side_effect=lambda: _Borrowed(pg)):
        # The wizard may hold the ref or the numeric id; both land on one row.
        assert _post("26-29267", active_ms=0, user=ADMIN) == {"status": "success"}
        assert _post("31920032", active_ms=60_000, user=ADMIN) == {"status": "success"}
        assert _post("26-29267", active_ms=15_000, user=ADMIN) == {"status": "success"}
        # A job without a JobDiva ref (external) resolves by its own id.
        assert _post("-4", active_ms=5_000, user=ADMIN) == {"status": "success"}
        assert _post("26-99999", active_ms=5_000, user=ADMIN) == {"status": "ignored"}

    with pg.cursor() as cur:
        cur.execute("SELECT job_id, step, user_email, active_ms FROM job_step_time ORDER BY job_id")
        rows = cur.fetchall()
    assert rows == [
        ("-4", 5, "admin@example.com", 5_000),
        ("31920032", 5, "admin@example.com", 75_000),
    ]

    got = jst.fetch_step_metrics(pg, [("31920032", "26-29267")])
    assert got["31920032"]["active_ms"] == 75_000
    assert got["31920032"]["by_user"] == {"admin@example.com": 75_000}
