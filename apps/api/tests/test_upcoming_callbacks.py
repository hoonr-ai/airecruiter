"""Coverage for the Upcoming Callbacks list and its "mark done" action:

- GET /candidates/callbacks/upcoming: scoped to the requesting recruiter
  (same scoping as the reminder notification itself), excludes callbacks
  already marked done, soonest-first.
- POST .../callback/done: a lightweight write (callback_done_at only) that
  never touches feedback_type, JobDiva, or any notification — the callback
  isn't an outcome by itself, so this deliberately doesn't ask the recruiter
  to pick Submit/Reject/Unreachable the way Rankings does.
"""
import json

import pytest
from fastapi import HTTPException

from core.auth import UserIdentity


class _FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self._fetchone_results = list(fetchone_results or [])
        self._fetchall_results = list(fetchall_results or [])

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_results.pop(0) if self._fetchone_results else None

    def fetchall(self):
        return self._fetchall_results.pop(0) if self._fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _setup(monkeypatch):
    from routers import candidates as cand_module
    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
    return cand_module


# --- POST /jobs/{job}/candidates/{id}/callback/done -------------------------

@pytest.mark.anyio
async def test_mark_callback_done_sets_callback_done_at_only(monkeypatch):
    cand_module = _setup(monkeypatch)
    cur = _FakeCursor(fetchone_results=[(55148489,)])
    conn = _FakeConn(cur)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: conn)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await cand_module.mark_candidate_callback_done(
        job_id_or_ref="26-12137", candidate_id="55148489", user=user,
    )

    assert res == {"status": "success"}
    update_calls = [c for c in cur.executed if "UPDATE sourced_candidates" in c[0]]
    assert len(update_calls) == 1
    _sql, params = update_calls[0]
    payload = json.loads(params[0])
    # Only callback_done_at — no feedback_type, no JobDiva action string,
    # nothing that would read as an actual outcome on Rankings.
    assert set(payload.keys()) == {"callback_done_at"}
    assert params[1] == 55148489
    assert conn.committed is True


@pytest.mark.anyio
async def test_mark_callback_done_404s_when_candidate_not_resolved(monkeypatch):
    cand_module = _setup(monkeypatch)
    cur = _FakeCursor(fetchone_results=[None])
    conn = _FakeConn(cur)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: conn)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    with pytest.raises(HTTPException) as exc_info:
        await cand_module.mark_candidate_callback_done(
            job_id_or_ref="26-12137", candidate_id="999999", user=user,
        )
    assert exc_info.value.status_code == 404


# --- GET /candidates/callbacks/upcoming -------------------------------------

def _row(**overrides):
    row = {
        "candidate_id": "c1", "name": "Jane Doe",
        "data": {
            "feedback_type": "Callback Scheduled",
            "callback_scheduled_at": "2026-01-01T15:00:00+00:00",
            "submitted_by": "recruiter@pyramidci.com",
        },
        "job_id": "J1", "jobdiva_id": "26-1", "title": "Data Engineer",
        "enhanced_title": None, "customer_name": "Acme",
    }
    row.update(overrides)
    return row


@pytest.mark.anyio
async def test_list_upcoming_callbacks_returns_scoped_rows(monkeypatch):
    cand_module = _setup(monkeypatch)
    cur = _FakeCursor(fetchall_results=[[_row()]])
    conn = _FakeConn(cur)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: conn)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await cand_module.list_upcoming_callbacks(user=user)

    assert res["status"] == "success"
    assert len(res["callbacks"]) == 1
    cb = res["callbacks"][0]
    assert cb["candidate_id"] == "c1"
    assert cb["candidate_name"] == "Jane Doe"
    assert cb["job_title"] == "Data Engineer"
    assert cb["customer_name"] == "Acme"
    assert cb["callback_at"] == "2026-01-01T15:00:00+00:00"

    sql, params = cur.executed[0]
    assert "callback_done_at' IS NULL" in sql
    assert "LOWER(TRIM(sc.data->>'submitted_by')) = %s" in sql
    assert params == ("recruiter@pyramidci.com",)


@pytest.mark.anyio
async def test_list_upcoming_callbacks_falls_back_to_the_plain_title(monkeypatch):
    cand_module = _setup(monkeypatch)
    cur = _FakeCursor(fetchall_results=[[_row(title="Data Engineer", enhanced_title="Senior Data Engineer")]])
    conn = _FakeConn(cur)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: conn)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await cand_module.list_upcoming_callbacks(user=user)
    assert res["callbacks"][0]["job_title"] == "Senior Data Engineer"


@pytest.mark.anyio
async def test_list_upcoming_callbacks_skips_a_row_with_no_callback_at(monkeypatch):
    """Defensive: the SQL filters on feedback_type/submitted_by/done, not on
    callback_scheduled_at's shape — a row that somehow lacks it must not
    surface with a None time."""
    cand_module = _setup(monkeypatch)
    bad_row = _row(data={"feedback_type": "Callback Scheduled", "submitted_by": "recruiter@pyramidci.com"})
    cur = _FakeCursor(fetchall_results=[[bad_row]])
    conn = _FakeConn(cur)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: conn)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    res = await cand_module.list_upcoming_callbacks(user=user)
    assert res["callbacks"] == []


@pytest.mark.anyio
async def test_list_upcoming_callbacks_noop_without_a_recipient_email(monkeypatch):
    cand_module = _setup(monkeypatch)

    def _fail():
        raise AssertionError("must not touch the DB without a recipient email")

    monkeypatch.setattr(cand_module, "get_db_connection", _fail)

    user = UserIdentity(email="", role="recruiter")
    res = await cand_module.list_upcoming_callbacks(user=user)
    assert res == {"status": "success", "callbacks": []}
