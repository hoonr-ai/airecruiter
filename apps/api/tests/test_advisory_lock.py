"""core.advisory_lock.AdvisoryLock against a fake psycopg2 connection."""
import asyncio

from core.advisory_lock import AdvisoryLock


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("connection lost")
        if "pg_try_advisory_lock" in sql:
            self._row = (self.conn.grant,)
        elif "pg_locks" in sql:
            self._row = (self.conn.held,)
        else:
            self._row = (True,)

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, grant=True, held=True, fail_on=None):
        self.grant, self.held, self.fail_on = grant, held, fail_on
        self.executed = []
        self.closed = False
        self.rolled_back = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _sql(conn):
    return [sql for sql, _ in conn.executed]


def test_acquire_hold_and_release():
    conn = _Conn()
    lock = AdvisoryLock(728195, "T", connect=lambda: conn)

    async def run():
        assert await lock.try_acquire()
        assert await lock.still_held()
        await lock.release()

    asyncio.run(run())
    assert any("pg_advisory_unlock" in s for s in _sql(conn))
    assert conn.rolled_back and conn.closed


def test_not_granted_closes_connection_and_release_is_noop():
    conn = _Conn(grant=False)
    lock = AdvisoryLock(1, "T", connect=lambda: conn)

    async def run():
        assert not await lock.try_acquire()
        assert not await lock.still_held()
        await lock.release()

    asyncio.run(run())
    assert conn.closed
    assert not any("pg_advisory_unlock" in s for s in _sql(conn))


def test_acquire_error_is_false_and_closes():
    conn = _Conn(fail_on="pg_try_advisory_lock")
    lock = AdvisoryLock(1, "T", connect=lambda: conn)
    assert asyncio.run(lock.try_acquire()) is False
    assert conn.closed


def test_lost_connection_reports_not_held():
    conn = _Conn(fail_on="pg_locks")
    lock = AdvisoryLock(1, "T", connect=lambda: conn)

    async def run():
        assert await lock.try_acquire()
        return await lock.still_held()

    assert asyncio.run(run()) is False


def test_lock_gone_reports_not_held():
    conn = _Conn(held=False)
    lock = AdvisoryLock(1, "T", connect=lambda: conn)

    async def run():
        assert await lock.try_acquire()
        return await lock.still_held()

    assert asyncio.run(run()) is False


def test_held_query_uses_the_lock_key():
    conn = _Conn()
    lock = AdvisoryLock(728195, "T", connect=lambda: conn)

    async def run():
        await lock.try_acquire()
        await lock.still_held()

    asyncio.run(run())
    held = [(s, p) for s, p in conn.executed if "pg_locks" in s]
    assert held and held[0][1] == (728195,)
