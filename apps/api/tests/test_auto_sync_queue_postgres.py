"""AutoSync job selection against a real Postgres (pgserver; skipped without it)."""
import shutil

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg2 = pytest.importorskip("psycopg2")

from services import auto_sync_queue as q  # noqa: E402


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    datadir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(datadir))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture
def conn(pg_uri, monkeypatch):
    monkeypatch.setattr(q, "_column_ready", False)
    c = psycopg2.connect(pg_uri)
    with c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS monitored_jobs")
        cur.execute(
            "CREATE TABLE monitored_jobs (job_id TEXT PRIMARY KEY, title TEXT, status TEXT,"
            " processing_status TEXT, is_archived BOOLEAN DEFAULT FALSE)"
        )
    c.commit()
    yield c
    c.close()


def _add(conn, job_id, status="OPEN", processing="step_5_complete", archived=False, synced=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO monitored_jobs (job_id, title, status, processing_status, is_archived) VALUES (%s,%s,%s,%s,%s)",
            (job_id, job_id, status, processing, archived),
        )
    conn.commit()
    if synced is not None:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE monitored_jobs SET last_auto_sync_at = NOW() - interval '{synced}' WHERE job_id = %s",
                (job_id,),
            )
        conn.commit()


def test_open_jobs_oldest_first_then_due_closed_jobs(conn):
    q.ensure_column(conn)
    _add(conn, "open-recent", synced="1 minute")
    _add(conn, "open-old", synced="3 hours")
    _add(conn, "open-never")
    _add(conn, "closed-recent", status="Closed", synced="2 hours")
    _add(conn, "closed-stale", status="Filled", synced="30 hours")
    _add(conn, "closed-never", status="cancelled")
    _add(conn, "archived", archived=True)
    _add(conn, "not-ready", processing="step_2_in_progress")

    ids = [r["job_id"] for r in q.pick_jobs(conn)]
    assert ids == ["open-never", "open-old", "open-recent", "closed-never", "closed-stale"]


def test_mark_synced_moves_job_to_the_back(conn):
    _add(conn, "a")
    _add(conn, "b")
    assert [r["job_id"] for r in q.pick_jobs(conn)][0] == "a"
    q.mark_synced(conn, ["a"])
    assert [r["job_id"] for r in q.pick_jobs(conn)] == ["b", "a"]


def test_ensure_column_is_idempotent(conn):
    q.ensure_column(conn)
    q._column_ready = False
    q.ensure_column(conn)
