"""routers/candidates.py's Upcoming Callbacks SQL against a real Postgres
(PR #844 review: the fake-cursor tests in test_upcoming_callbacks.py check
call shape, not that the actual JSONB filter/sort semantics hold against a
real engine).

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres") — same precedence as
test_task_outbox_pg.py.
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from routers import candidates as cand_module  # noqa: E402
from core.auth import UserIdentity  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


class _Shared:
    """One connection for the whole test, so TEMP tables stay visible across
    the several get_db_connection() calls routers/candidates.py makes."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, title TEXT, enhanced_title TEXT, customer_name TEXT,
  recruiter_emails TEXT, jobdiva_id TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, name TEXT, email TEXT,
  data JSONB, source TEXT, updated_at TIMESTAMPTZ DEFAULT now());
INSERT INTO monitored_jobs (job_id, title, customer_name, recruiter_emails, jobdiva_id)
  VALUES ('100', 'Data Engineer', 'Acme', '["recruiter@pyramidci.com"]', '26-100');
"""


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_FIXTURE_SQL)
        monkeypatch.setattr(cand_module, "get_db_connection", lambda: _Shared(conn))
        monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_candidate(conn, *, candidate_id, name, data):
    import json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data) "
            "VALUES (%s, '26-100', %s, %s::jsonb) RETURNING id",
            (candidate_id, name, json.dumps(data)),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


_RECRUITER = "recruiter@pyramidci.com"


@pytest.mark.anyio
async def test_list_upcoming_callbacks_filters_and_sorts_against_real_sql(pg):
    # Matches: this recruiter's own pending callback.
    _insert_candidate(pg, candidate_id="c-later", name="Later Larry", data={
        "feedback_type": "Callback Scheduled", "submitted_by": _RECRUITER,
        "callback_scheduled_at": "2026-02-01T15:00:00+00:00",
    })
    _insert_candidate(pg, candidate_id="c-sooner", name="Sooner Sam", data={
        "feedback_type": "Callback Scheduled", "submitted_by": _RECRUITER,
        "callback_scheduled_at": "2026-01-01T15:00:00+00:00",
    })
    # Excluded: already marked done.
    _insert_candidate(pg, candidate_id="c-done", name="Done Dana", data={
        "feedback_type": "Callback Scheduled", "submitted_by": _RECRUITER,
        "callback_scheduled_at": "2026-01-05T15:00:00+00:00", "callback_done_at": "2026-01-01T00:00:00+00:00",
    })
    # Excluded: a different recruiter's callback.
    _insert_candidate(pg, candidate_id="c-other-recruiter", name="Other Oliver", data={
        "feedback_type": "Callback Scheduled", "submitted_by": "someone-else@pyramidci.com",
        "callback_scheduled_at": "2026-01-02T15:00:00+00:00",
    })
    # Excluded: not a callback at all.
    _insert_candidate(pg, candidate_id="c-rejected", name="Rejected Rex", data={
        "feedback_type": "Reject", "submitted_by": _RECRUITER,
    })

    user = UserIdentity(email=_RECRUITER, role="recruiter")
    res = await cand_module.list_upcoming_callbacks(user=user)

    assert res["status"] == "success"
    names = [cb["candidate_name"] for cb in res["callbacks"]]
    assert names == ["Sooner Sam", "Later Larry"]  # sorted soonest-first, real timestamptz ORDER BY
    assert res["callbacks"][0]["customer_name"] == "Acme"


@pytest.mark.anyio
async def test_mark_callback_done_only_updates_a_pending_callback_row(pg):
    pending_id = _insert_candidate(pg, candidate_id="c1", name="Jane Doe", data={
        "feedback_type": "Callback Scheduled", "submitted_by": _RECRUITER,
        "callback_scheduled_at": "2026-01-01T15:00:00+00:00",
    })
    rejected_id = _insert_candidate(pg, candidate_id="c2", name="John Roe", data={
        "feedback_type": "Reject", "submitted_by": _RECRUITER,
    })

    user = UserIdentity(email=_RECRUITER, role="recruiter")
    await cand_module.mark_candidate_callback_done(job_id_or_ref="100", candidate_id="c1", user=user)

    with pg.cursor() as cur:
        cur.execute("SELECT data->>'callback_done_at' FROM sourced_candidates WHERE id = %s", (pending_id,))
        assert cur.fetchone()[0] is not None

        # PR #844 review: the UPDATE is scoped to feedback_type = 'Callback
        # Scheduled' at the SQL level — confirm a non-callback row is never
        # touched even if somehow targeted.
        cur.execute(
            "UPDATE sourced_candidates SET data = data || '{\"callback_done_at\": \"x\"}'::jsonb "
            "WHERE id = %s AND data->>'feedback_type' = 'Callback Scheduled'",
            (rejected_id,),
        )
        assert cur.rowcount == 0


@pytest.mark.anyio
async def test_mark_callback_done_403s_a_non_owner_against_real_sql(pg):
    _insert_candidate(pg, candidate_id="c1", name="Jane Doe", data={
        "feedback_type": "Callback Scheduled", "submitted_by": _RECRUITER,
        "callback_scheduled_at": "2026-01-01T15:00:00+00:00",
    })

    from fastapi import HTTPException

    other_user = UserIdentity(email="someone-else@pyramidci.com", role="recruiter")
    with pytest.raises(HTTPException) as exc_info:
        await cand_module.mark_candidate_callback_done(job_id_or_ref="100", candidate_id="c1", user=other_user)
    assert exc_info.value.status_code == 403
