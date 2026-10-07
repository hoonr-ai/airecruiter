"""core.startup_once against fake locks and an in-memory marker table."""
import asyncio

from core import startup_once


class _Lock:
    def __init__(self, grants):
        self.grants = list(grants)
        self.released = False

    async def try_acquire(self):
        return self.grants.pop(0) if self.grants else False

    async def release(self):
        self.released = True


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.description = None
        self._row = None

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.description = None
        if sql.startswith("SELECT 1 FROM app_startup_runs"):
            self.description = True
            self._row = (1,) if tuple(params) in self.db.rows else None
        elif sql.startswith("INSERT INTO app_startup_runs"):
            new = tuple(params) not in self.db.rows
            self.db.rows.add(tuple(params))
            if "RETURNING" in sql:
                self.description = True
                self._row = (1,) if new else None

    def fetchone(self):
        return self._row


class _DB:
    def __init__(self):
        self.rows = set()

    def connect(self):
        db = self

        class _Conn:
            def cursor(self):
                return _Cursor(db)

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        return _Conn()


def _run(coro):
    return asyncio.run(coro)


def test_schema_runs_once_then_later_workers_skip():
    db, calls = _DB(), []

    async def fn():
        calls.append(1)
        return True

    for _ in range(3):
        lock = _Lock([True])
        _run(startup_once.run_schema_once("s", fn, lock=lock, connect=db.connect, wait_s=0))
        assert lock.released
    assert calls == [1]


def test_failed_schema_run_is_retried_by_next_worker():
    db, results = _DB(), [False, True]

    async def fn():
        return results.pop(0)

    _run(startup_once.run_schema_once("s", fn, lock=_Lock([True]), connect=db.connect, wait_s=0))
    _run(startup_once.run_schema_once("s", fn, lock=_Lock([True]), connect=db.connect, wait_s=0))
    assert results == []


def test_waits_for_lock_holder(monkeypatch):
    monkeypatch.setattr(startup_once, "_POLL_S", 0)
    db, calls = _DB(), []

    async def fn():
        calls.append(1)
        return True

    db.rows.add((startup_once.BOOT_ID, "s"))  # the holder finished meanwhile
    lock = _Lock([False, False, True])
    _run(startup_once.run_schema_once("s", fn, lock=lock, connect=db.connect, wait_s=5))
    assert calls == [] and lock.released and lock.grants == []


def test_runs_anyway_when_lock_never_comes():
    db, calls = _DB(), []

    async def fn():
        calls.append(1)
        return True

    lock = _Lock([])
    _run(startup_once.run_schema_once("s", fn, lock=lock, connect=db.connect, wait_s=0))
    assert calls == [1] and not lock.released


def test_claim_once_has_one_winner():
    db = _DB()
    wins = [_run(startup_once.claim_once("t", connect=db.connect)) for _ in range(4)]
    assert wins == [True, False, False, False]


def test_claim_once_runs_when_db_fails():
    def boom():
        raise RuntimeError("db down")

    assert _run(startup_once.claim_once("t", connect=boom)) is True
