"""services/task_outbox.py against a real Postgres: rows added on the caller's
transaction commit or roll back with it, the relay runs due rows once, retries
retry-safe tasks with backoff, and parks the rest as dead.

Skipped when no Postgres is reachable (LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import asyncio
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

import core.queue as queue  # noqa: E402
import core.queue_tasks  # noqa: E402,F401  (register real tasks before any test swaps the registry)
from services import task_outbox  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


class _Shared:
    """One connection for the whole test, so TEMP tables are visible to the relay."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            for stmt in task_outbox.SCHEMA_STATEMENTS:
                cur.execute(stmt.replace("CREATE TABLE IF NOT EXISTS task_outbox", "CREATE TEMP TABLE task_outbox")
                            .replace("ON task_outbox", "ON pg_temp.task_outbox"))
        monkeypatch.setattr(task_outbox, "_connect", lambda: _Shared(conn))
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def tasks(monkeypatch):
    calls = []
    behaviour = {}

    def make(name):
        async def fn(*args):
            calls.append((name, args))
            err = behaviour.get(name)
            if err:
                raise err

        return fn

    registry = {
        "safe": queue._Task("safe", make("safe"), max_tries=3, timeout_s=5),
        "once": queue._Task("once", make("once"), max_tries=1, timeout_s=5),
    }
    monkeypatch.setattr(queue, "_REGISTRY", registry)
    return calls, behaviour


def _rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT task, status, attempts, last_error IS NOT NULL FROM task_outbox ORDER BY id")
        return cur.fetchall()


def test_add_then_relay_runs_each_row_once(pg, tasks):
    calls, _ = tasks
    with pg.cursor() as cur:
        assert task_outbox.add(cur, "safe", "J1", 2) is not None
        assert task_outbox.add(cur, "once", ["a"], dedupe_key="k1") is not None
        assert task_outbox.add(cur, "once", ["a"], dedupe_key="k1") is None  # deduped

    out = asyncio.run(task_outbox.relay_once())
    assert out == {"claimed": 2, "done": 2, "failed": 0}
    assert calls == [("safe", ("J1", 2)), ("once", (["a"],))]
    assert _rows(pg) == [("safe", "done", 1, False), ("once", "done", 1, False)]

    assert asyncio.run(task_outbox.relay_once())["claimed"] == 0


def test_rows_roll_back_with_the_callers_transaction(pg, tasks):
    with pg.cursor() as cur:
        cur.execute("SAVEPOINT s")
        task_outbox.add(cur, "safe", "J1")
        cur.execute("ROLLBACK TO SAVEPOINT s")
    assert _rows(pg) == []


def test_retry_safe_task_backs_off_then_dies_after_max_attempts(pg, tasks):
    calls, behaviour = tasks
    behaviour["safe"] = RuntimeError("jobdiva 503")
    with pg.cursor() as cur:
        task_outbox.add(cur, "safe", "J1")

    asyncio.run(task_outbox.relay_once())
    assert _rows(pg) == [("safe", "pending", 1, True)]
    # Backoff: not due yet.
    assert asyncio.run(task_outbox.relay_once())["claimed"] == 0

    for _ in range(2):
        with pg.cursor() as cur:
            cur.execute("UPDATE task_outbox SET available_at = now()")
        asyncio.run(task_outbox.relay_once())
    assert _rows(pg) == [("safe", "dead", 3, True)]
    assert len(calls) == 3


def test_non_retryable_task_fails_straight_to_dead(pg, tasks):
    _, behaviour = tasks
    behaviour["once"] = RuntimeError("boom")
    with pg.cursor() as cur:
        task_outbox.add(cur, "once", "x")
    asyncio.run(task_outbox.relay_once())
    assert _rows(pg) == [("once", "dead", 1, True)]


def test_stale_running_rows_are_reaped_by_retry_policy(pg, tasks):
    calls, _ = tasks
    with pg.cursor() as cur:
        task_outbox.add(cur, "safe", "s")
        task_outbox.add(cur, "once", "o")
        cur.execute(
            "UPDATE task_outbox SET status = 'running', attempts = 1, locked_at = now() - interval '1 hour'"
        )
    out = asyncio.run(task_outbox.relay_once())
    # The retry-safe row runs again; the run-once row is parked, not re-run.
    assert calls == [("safe", ("s",))]
    assert out["done"] == 1
    assert _rows(pg) == [("safe", "done", 2, False), ("once", "dead", 1, True)]


def test_claimed_rows_run_concurrently(pg, monkeypatch):
    # Each task waits for the other; run one at a time this would time out.
    seen = []

    async def fn(x):
        seen.append(x)
        while len(seen) < 2:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(queue, "_REGISTRY", {"w": queue._Task("w", fn, max_tries=1, timeout_s=2)})
    with pg.cursor() as cur:
        task_outbox.add(cur, "w", 1)
        task_outbox.add(cur, "w", 2)
    out = asyncio.run(task_outbox.relay_once())
    assert out == {"claimed": 2, "done": 2, "failed": 0}


def test_heartbeat_keeps_a_long_running_row_from_being_reaped(pg, tasks):
    with pg.cursor() as cur:
        task_outbox.add(cur, "once", "o")
        cur.execute(
            "UPDATE task_outbox SET status = 'running', attempts = 1, locked_at = now() - interval '1 hour'"
        )
        cur.execute("SELECT id FROM task_outbox")
        (row_id,) = cur.fetchone()
    task_outbox._heartbeat([row_id])
    with pg.cursor() as cur:
        cur.execute(task_outbox._REAP_SQL)
    assert _rows(pg) == [("once", "running", 1, False)]


def test_purge_and_lag(pg, tasks):
    with pg.cursor() as cur:
        task_outbox.add(cur, "safe", "old")
        task_outbox.add(cur, "safe", "waiting")
        cur.execute(
            "UPDATE task_outbox SET status = 'done', updated_at = now() - interval '30 days' WHERE args = '[\"old\"]'"
        )
        cur.execute("UPDATE task_outbox SET available_at = now() - interval '20 minutes' WHERE status = 'pending'")
    assert task_outbox.purge_old() == 1
    assert task_outbox.oldest_pending_age_seconds() > 1000


# --- passed-candidate flow: flag and JobDiva side effects commit together ----

_PASS_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, title TEXT, enhanced_title TEXT, city TEXT, state TEXT,
  pay_rate TEXT, recruiter_emails TEXT, jobdiva_id TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, candidate_id TEXT, jobdiva_id TEXT, name TEXT, email TEXT, phone TEXT,
  resume_text TEXT, data JSONB, source TEXT, updated_at TIMESTAMPTZ);
INSERT INTO monitored_jobs (job_id, title, recruiter_emails, jobdiva_id)
  VALUES ('100', 'Java Developer', '[]', '26-100');
INSERT INTO sourced_candidates (candidate_id, jobdiva_id, name, email, data, source)
  VALUES ('c1', '26-100', 'Ann Lee', 'ann@x.com', '{"jobdiva_candidate_id": "555"}', 'JobDiva-Applicants');
"""


@pytest.fixture()
def pass_flow(pg, monkeypatch):
    from routers.engagement import common, provisioning

    with pg.cursor() as cur:
        cur.execute(_PASS_FIXTURE_SQL)
    monkeypatch.setattr(common, "_get_db_connection", lambda: _Shared(pg))
    monkeypatch.setattr(provisioning, "get_db_connection", lambda: _Shared(pg))

    class _NoResume:
        async def get_candidate_resume(self, **_k):
            return {}

    monkeypatch.setattr(provisioning, "jobdiva_service", _NoResume())
    emails = []
    monkeypatch.setattr(provisioning, "notify_candidate_passed", lambda **k: emails.append(k) or True)

    async def no_enqueue(*_a, **_k):
        return "spawned"

    monkeypatch.setattr(provisioning, "enqueue", no_enqueue)
    return provisioning, emails


def _passed_payload():
    return {"interview": {"hard_filter_status": "pass", "candidate_score": 8, "total_score": 10,
                          "completed_at": "2026-10-20T10:00:00+00:00"}}


def test_pass_writes_jobdiva_side_effects_to_the_outbox_once(pass_flow):
    provisioning, emails = pass_flow
    conn = provisioning.common._get_db_connection()._conn

    asyncio.run(provisioning._check_and_fire_candidate_passed_notification(1, _passed_payload(), "100", "c1"))
    asyncio.run(provisioning._check_and_fire_candidate_passed_notification(1, _passed_payload(), "100", "c1"))

    with conn.cursor() as cur:
        cur.execute("SELECT task, args, dedupe_key, max_attempts FROM task_outbox ORDER BY id")
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["update_jobdiva_qualification", "create_jobdiva_pass_note"]
    assert rows[0][1][0] == "555" and rows[0][1][4] == "2026-10-20T10:00:00+00:00"
    assert rows[1][1][:3] == ["555", "26-100", provisioning.JOBDIVA_PASS_ACTION_NAME]
    assert "Java Developer" in rows[1][1][3]
    assert rows[1][2] == "jobdiva_pass_note:555:26-100"
    assert [r[3] for r in rows] == [3, 1]
    assert len(emails) == 1  # the second webhook hit the flag and stopped
