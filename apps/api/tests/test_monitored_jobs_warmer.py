"""The dashboard cache warmer gives up fast and never reads during auto-sync's
monitored_jobs writes (core/monitored_jobs_lock)."""
import psycopg2.errors
import pytest

from routers import jobs as jobs_router


class _Cursor:
    def __init__(self, lock_free=True, fail_with=None):
        self.lock_free = lock_free
        self.fail_with = fail_with
        self.statements = []
        self._result = None
        self.description = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "pg_try_advisory_xact_lock_shared" in sql:
            self._result = (self.lock_free,)
        elif sql.startswith("SET LOCAL"):
            pass
        elif self.fail_with is not None:
            raise self.fail_with
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self, *a, **k):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _use(monkeypatch, cursor):
    conn = _Conn(cursor)
    monkeypatch.setattr(jobs_router, "get_db_connection", lambda: conn)
    return conn


def test_warmer_skips_while_auto_sync_is_writing(monkeypatch):
    cursor = _Cursor(lock_free=False)
    conn = _use(monkeypatch, cursor)
    with pytest.raises(jobs_router.WarmerSkipped):
        jobs_router._get_monitored_jobs_sync(False, "summary", warmer=True)
    assert not any(sql.startswith("SELECT") and "monitored_jobs" in sql for sql, _ in cursor.statements)
    assert conn.rolled_back and conn.closed  # shared lock released, connection returned


def test_warmer_uses_a_short_lock_timeout(monkeypatch):
    cursor = _Cursor(lock_free=True)
    _use(monkeypatch, cursor)
    jobs_router._get_monitored_jobs_sync(False, "summary", warmer=True)
    assert ("SET LOCAL lock_timeout = %s", ("300ms",)) in cursor.statements


def test_warmer_turns_a_lock_wait_timeout_into_a_skip(monkeypatch):
    _use(monkeypatch, _Cursor(fail_with=psycopg2.errors.LockNotAvailable("lock timeout")))
    with pytest.raises(jobs_router.WarmerSkipped):
        jobs_router._get_monitored_jobs_sync(False, "summary", warmer=True)


def test_dashboard_requests_keep_their_timeout_and_take_no_lock(monkeypatch):
    cursor = _Cursor(lock_free=False)
    _use(monkeypatch, cursor)
    jobs_router._get_monitored_jobs_sync(False, "summary")
    assert ("SET LOCAL lock_timeout = %s", ("2000ms",)) in cursor.statements
    assert not any("advisory" in sql for sql, _ in cursor.statements)
