"""The Reject row-lock UPDATEs (both the sc_row_id fast path and the
candidate_id/jobdiva_id fallback) against a REAL Postgres.

Why this file has to exist: every other test for this code
(test_candidate_feedback_reject_reason_change.py) uses a fake cursor that
just records the SQL text and returns canned Python values — it never
actually asks a real planner to parse and run the query. Both UPDATEs
shipped with a real syntax defect (`SET data = data || ...` where the `FROM
(subquery) AS prev` subquery also selects a column named `data`, making the
bare `data` on the right of `=` ambiguous) that made EVERY Reject through
either path fail outright with "column reference \"data\" is ambiguous" —
100% reproducible, not an edge case — and the entire mocked-cursor suite
still passed, because it never executed real SQL. Reproduced directly
against Postgres (not guessed) before fixing it.

sourced_candidates.updated_at here is TIMESTAMP (no time zone), matching
services/sourced_candidates_storage.py's own CREATE TABLE exactly — not
TIMESTAMPTZ — since a mismatch there is what caused the *other* bug this
branch also fixes (naive-vs-aware TypeError in the fallback path's tiebreak).

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres" — the same DSN precedence and
default every other *_pg.py file in this suite uses; only TEMP tables are
created here, so the shared default database is never actually touched).
Modeled on test_feedback_candidate_resolution_ordering_pg.py's own fixture
shape.

PR #872 review: confirmed directly — CI (.github/workflows/deploy.yml) runs
`pytest -q` with no Postgres service and no LAUNCH_PLATFORM_TEST_DSN, so
this file (and every other *_pg.py file already in this suite) skips in CI
today, not just in theory. A prior attempt to add a CI Postgres service
(PR #851) was reverted because it surfaced unrelated pre-existing failures
elsewhere that needed their own investigation — reintroducing that change
here, bundled into an urgent hotfix, would repeat the same mistake. Failing
instead of skipping under REQUIRE_PG_TESTS=1 gives a future CI change (once
a Postgres service actually exists) a way to enforce this without silently
depending on infrastructure that isn't there yet, and without this PR's own
checks going red over an unrelated, already-known gap.
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from fastapi import BackgroundTasks  # noqa: E402

from core.auth import UserIdentity  # noqa: E402
from models import CandidateFeedbackRequest  # noqa: E402
from routers import candidates as cand_module  # noqa: E402
from routers.candidates import REJECT_LOCK_UPDATE_BY_CANDIDATE_JOB_SQL  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_RECRUITER = "recruiter@pyramidci.com"

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, title TEXT, enhanced_title TEXT, customer_name TEXT,
  recruiter_emails TEXT, jobdiva_id TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, name TEXT, email TEXT,
  data JSONB, source TEXT,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO monitored_jobs (job_id, title, customer_name, recruiter_emails, jobdiva_id)
  VALUES ('100', 'Data Engineer', 'Acme', '["recruiter@pyramidci.com"]', '26-100');
"""


class _Shared:
    """One connection for the whole test, so the TEMP tables stay visible
    across the several get_db_connection() calls save_candidate_feedback
    makes."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        # REQUIRE_PG_TESTS=1: opt-in for a CI (or local) setup that actually
        # provisions Postgres, so a missing DB fails loudly instead of this
        # regression guard silently turning green. Not set by CI today —
        # see this module's own docstring for why.
        if os.getenv("REQUIRE_PG_TESTS") == "1":
            pytest.fail(f"REQUIRE_PG_TESTS=1 but no Postgres reachable at {_DSN!r}: {exc}")
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_FIXTURE_SQL)
        monkeypatch.setattr(cand_module, "get_db_connection", lambda: _Shared(conn))
        monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
        monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 0})

        async def fake_create_note(**kwargs):
            return {"status": "success"}
        from services.jobdiva import jobdiva_service
        monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)

        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_row(conn, *, candidate_id="C1", source="jobdiva", data="{}"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, data, source) "
            "VALUES (%s, '26-100', 'Jane Doe', %s::jsonb, %s) RETURNING id",
            (candidate_id, data, source),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def _read_data(conn, row_id):
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM sourced_candidates WHERE id = %s", (row_id,))
        return cur.fetchone()[0]


async def _reject(user, candidate_id, reason, sourced_row_id=None):
    return await cand_module.save_candidate_feedback(
        job_id_or_ref="100", candidate_id=candidate_id,
        request=CandidateFeedbackRequest(feedback_type="Reject", reason=reason, sourced_row_id=sourced_row_id),
        background_tasks=BackgroundTasks(), user=user,
    )


@pytest.mark.anyio
async def test_reject_via_sourced_row_id_fast_path_does_not_error(pg):
    """The sc_row_id branch's UPDATE ... FROM (subquery) ... RETURNING —
    the exact shape that raised "column reference \"data\" is ambiguous"."""
    row_id = _insert_row(pg)
    user = UserIdentity(email=_RECRUITER, role="recruiter")

    res = await _reject(user, "C1", "Skills do not meet requirements", sourced_row_id=row_id)

    assert res["status"] == "success"
    assert _read_data(pg, row_id)["feedback_reason"] == "Skills do not meet requirements"


@pytest.mark.anyio
async def test_reject_via_candidate_id_fallback_path_does_not_error(pg):
    """The candidate_id/jobdiva_id fallback's UPDATE ... FROM (subquery ...
    FOR UPDATE) ... RETURNING — same ambiguous-column shape, plus the
    ORDER BY/tiebreak logic on top."""
    row_id = _insert_row(pg)
    user = UserIdentity(email=_RECRUITER, role="recruiter")

    res = await _reject(user, "C1", "Skills do not meet requirements")

    assert res["status"] == "success"
    assert _read_data(pg, row_id)["feedback_reason"] == "Skills do not meet requirements"


@pytest.mark.anyio
async def test_reject_with_a_real_duplicate_row_present_does_not_error(pg):
    """A genuinely multi-sourced candidate (two real sourced_candidates
    rows for the same candidate_id + job). Confirmed by tracing this
    directly: with a resolvable job scope, step 1's own resolution already
    picks one specific row (its usual LIMIT 1 + tiebreaker), so this lands
    on the sc_row_id fast path, not the candidate_id/jobdiva_id fallback —
    exactly one of the two duplicates ends up updated, the other untouched.
    Still real coverage: a second real row existing at all is what the
    ambiguous-column bug didn't care about — either path hit it."""
    row_a = _insert_row(pg, source="jobdiva")
    row_b = _insert_row(pg, source="linkedin")
    user = UserIdentity(email=_RECRUITER, role="recruiter")

    res = await _reject(user, "C1", "Skills do not meet requirements")

    assert res["status"] == "success"
    updated = [r for r in (row_a, row_b) if _read_data(pg, r).get("feedback_reason") == "Skills do not meet requirements"]
    assert len(updated) == 1


@pytest.mark.anyio
async def test_changing_an_already_rejected_reason_does_not_error(pg):
    """Exercises the "is this actually a change" branch end to end against
    real Postgres, including the naive-updated_at tiebreak."""
    row_id = _insert_row(
        pg,
        data='{"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": true}',
    )
    user = UserIdentity(email=_RECRUITER, role="recruiter")

    res = await _reject(user, "C1", "Communication skills", sourced_row_id=row_id)

    assert res["status"] == "success"
    assert _read_data(pg, row_id)["feedback_reason"] == "Communication skills"


def test_fallback_lock_sql_directly_against_two_duplicate_rows(pg):
    """The real candidate_id/jobdiva_id fallback UPDATE constant, run
    directly against two real duplicate rows — not through
    save_candidate_feedback, since step 1's own candidate resolution already
    finds a specific row via its own (job-unscoped) third tier whenever any
    match exists at all, so a full endpoint call practically never reaches
    this exact query with more than one row still unresolved. Imports the
    same REJECT_LOCK_UPDATE_BY_CANDIDATE_JOB_SQL the endpoint runs, so this
    can't drift from it the way a hand-copied string could."""
    row_a = _insert_row(pg, source="jobdiva")
    row_b = _insert_row(pg, source="linkedin")

    with pg.cursor() as cur:
        cur.execute(
            REJECT_LOCK_UPDATE_BY_CANDIDATE_JOB_SQL,
            ('{"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements"}',
             "C1", None, None, "26-100", "26-100", "26-100"),
        )
        returned = cur.fetchall()
    pg.commit()

    # Both duplicates matched and locked (this UPDATE's own WHERE has always
    # written every matching duplicate — unchanged by the row-lock fix).
    assert {r[0] for r in returned} == {row_a, row_b}
    assert _read_data(pg, row_a)["feedback_reason"] == "Skills do not meet requirements"
    assert _read_data(pg, row_b)["feedback_reason"] == "Skills do not meet requirements"
