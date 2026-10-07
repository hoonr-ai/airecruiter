"""Run startup work once per deploy instead of once per uvicorn worker.

Every worker runs the FastAPI lifespan, so schema DDL, counter backfills and
the first AutoSync used to run 8 times per restart (and again whenever a
worker is recycled by ``--limit-max-requests``). Two helpers:

- ``run_schema_once`` — schema init that requests depend on. One worker runs
  it under a Postgres advisory lock; the others wait for the lock, see the
  boot marker and skip. A run that had failures is not marked, so the next
  worker (or a recycled one) tries again.
- ``claim_once`` — fire-and-forget work (backfills, first sync). The first
  worker to insert the boot marker runs it; the rest skip.

A "boot" is one uvicorn master process on one host running one release:
workers, including recycled ones, share the master's pid (``os.getppid()``).
The id also carries the master's start time (so a reused pid after a reboot
is a new boot) and the release (``STARTUP_RELEASE_ID``, else the git commit),
because ``systemctl reload`` (SIGHUP) respawns workers under the same master
with new code — a deploy that ships new schema DDL must not be skipped.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import time
from typing import Any, Awaitable, Callable, Optional

from core.advisory_lock import AdvisoryLock

log = logging.getLogger(__name__)

STARTUP_SCHEMA_LOCK_KEY = 728196  # 728193-728195: poll, jobdiva_bi_sync, AutoSync
SCHEMA_WAIT_S = float(os.getenv("STARTUP_SCHEMA_WAIT_S", "150") or 150)
_POLL_S = 1.0



def _process_start_ticks(pid: int) -> str:
    """Start time of ``pid`` in clock ticks since boot (Linux), else ''."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        # Field 22; split after the ")" closing comm, which may hold spaces.
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _release_id() -> str:
    rel = os.getenv("STARTUP_RELEASE_ID")
    if rel:
        return rel
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _boot_id() -> str:
    if os.getenv("STARTUP_BOOT_ID"):
        return os.environ["STARTUP_BOOT_ID"]
    ppid = os.getppid()
    return ":".join([socket.gethostname(), str(ppid), _process_start_ticks(ppid), _release_id()[:12]])


BOOT_ID = _boot_id()

_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS app_startup_runs (
        boot_id TEXT NOT NULL,
        task TEXT NOT NULL,
        done_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (boot_id, task)
    )
"""


def _default_connect() -> Any:
    from core.db import get_db_connection
    return get_db_connection()


def _exec(connect: Callable[[], Any], sql: str, params: tuple = ()) -> Optional[tuple]:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(_TABLE_SQL)
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _is_done(connect, task: str) -> bool:
    row = _exec(connect, "SELECT 1 FROM app_startup_runs WHERE boot_id = %s AND task = %s", (BOOT_ID, task))
    return row is not None


def _mark_done(connect, task: str) -> None:
    _exec(
        connect,
        "INSERT INTO app_startup_runs (boot_id, task) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (BOOT_ID, task),
    )
    # Keep the table small; old boots are never looked up again.
    _exec(connect, "DELETE FROM app_startup_runs WHERE done_at < now() - interval '30 days'")


def _claim(connect, task: str) -> bool:
    row = _exec(
        connect,
        "INSERT INTO app_startup_runs (boot_id, task) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1",
        (BOOT_ID, task),
    )
    return row is not None


async def claim_once(task: str, connect: Optional[Callable[[], Any]] = None) -> bool:
    """True for exactly one worker per boot. On a DB error, True (old behaviour)."""
    try:
        won = await asyncio.to_thread(_claim, connect or _default_connect, task)
    except Exception as exc:  # noqa: BLE001
        log.warning("[startup] %s: claim failed (%s); running in this worker", task, exc)
        return True
    if not won:
        log.info("[startup] %s already claimed by another worker (boot %s); skipping", task, BOOT_ID)
    return won


async def run_schema_once(
    task: str,
    fn: Callable[[], Awaitable[bool]],
    *,
    lock: Optional[AdvisoryLock] = None,
    connect: Optional[Callable[[], Any]] = None,
    wait_s: float = SCHEMA_WAIT_S,
) -> None:
    """Run ``fn`` in one worker per boot; the others wait for it to finish.

    ``fn`` returns True when every step succeeded; only then is the boot
    marked done. If the lock or marker table is unavailable, ``fn`` runs in
    this worker, as it did before.
    """
    connect = connect or _default_connect
    lock = lock or AdvisoryLock(STARTUP_SCHEMA_LOCK_KEY, f"startup:{task}")
    deadline = time.monotonic() + wait_s
    got = await lock.try_acquire()
    while not got and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_S)
        got = await lock.try_acquire()
    if not got:
        log.warning("[startup] %s: lock not acquired in %.0fs; running in this worker", task, wait_s)
    try:
        try:
            if await asyncio.to_thread(_is_done, connect, task):
                log.info("[startup] %s already done for boot %s; skipping", task, BOOT_ID)
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("[startup] %s: marker check failed (%s); running anyway", task, exc)
        ok = await fn()
        if ok:
            try:
                await asyncio.to_thread(_mark_done, connect, task)
            except Exception as exc:  # noqa: BLE001
                log.warning("[startup] %s: could not record completion: %s", task, exc)
        else:
            log.warning("[startup] %s finished with failures; the next worker will retry", task)
    finally:
        if got:
            await lock.release()
