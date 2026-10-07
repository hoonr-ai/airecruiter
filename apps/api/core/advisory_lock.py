"""Cross-worker Postgres session advisory lock for scheduler jobs.

Every uvicorn worker runs the same APScheduler jobs; a session advisory lock
lets one worker run a cycle while the others skip it. The DB calls are
blocking psycopg2, so they run in a thread instead of on the event loop.

A session lock lives only as long as its connection. If that connection
drops mid-cycle, Postgres releases the lock and another worker may start a
cycle — so long-running holders should call ``still_held()`` between units
of work and stop when it returns False.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# pg_locks splits a bigint advisory key into classid (high 32 bits) and objid
# (low 32 bits), with objsubid = 1 for the single-bigint form.
_HELD_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid()
          AND objsubid = 1
          AND ((classid::bigint << 32) | objid::bigint) = %s
    )
"""


class AdvisoryLock:
    def __init__(self, key: int, name: str, connect: Optional[Callable[[], Any]] = None):
        self.key = key
        self.name = name
        self._connect = connect
        self._conn: Optional[Any] = None

    def _get_connection(self) -> Any:
        if self._connect is not None:
            return self._connect()
        from core.db import get_session_db_connection
        return get_session_db_connection()

    def _try_acquire_sync(self) -> bool:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
            got = bool(cur.fetchone()[0])
            conn.commit()
        except Exception:
            conn.close()
            raise
        if got:
            self._conn = conn
        else:
            conn.close()
        return got

    async def try_acquire(self) -> bool:
        """True if this worker now holds the lock. Never raises."""
        try:
            return await asyncio.to_thread(self._try_acquire_sync)
        except Exception as exc:
            log.error("[%s] could not take advisory lock: %s", self.name, exc)
            return False

    def _still_held_sync(self) -> bool:
        if self._conn is None:
            return False
        cur = self._conn.cursor()
        cur.execute(_HELD_SQL, (self.key,))
        held = bool(cur.fetchone()[0])
        self._conn.commit()
        return held

    async def still_held(self) -> bool:
        """False if the lock connection dropped or the lock is gone. Never raises."""
        try:
            return await asyncio.to_thread(self._still_held_sync)
        except Exception as exc:
            log.error("[%s] advisory lock check failed (connection lost?): %s", self.name, exc)
            return False

    def _release_sync(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        # A pooled connection outlives close(); a session lock left on it
        # would block every later cycle on every worker.
        try:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_unlock(%s)", (self.key,))
            conn.commit()
        except Exception as exc:
            log.warning("[%s] advisory unlock failed: %s", self.name, exc)
        finally:
            conn.close()

    async def release(self) -> None:
        """Release the lock and return the connection. Never raises."""
        try:
            await asyncio.to_thread(self._release_sync)
        except Exception as exc:
            log.warning("[%s] advisory release failed: %s", self.name, exc)
