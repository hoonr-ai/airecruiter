"""The one lock the monitored_jobs cache warmer and auto-sync's writes share.

Auto-sync holds it EXCLUSIVE, transaction-scoped, only while it writes one
job's monitored_jobs row (its cycle can run ~13 of every 15 minutes, so a
whole-cycle lock would starve the warmer). The warmer holds it SHARED while
it reads: warmers on all workers can read together, never during a write. A
warmer that can't get it at once skips that cycle; the dashboard keeps its
own cache and live query. A waiting writer also blocks new shared takers, so
a steady stream of warmers can't starve a write.

Keyed by hashtext(name) rather than a number from the hand-kept 7281xx
registry (see main.py), which already has overlaps.
"""
from __future__ import annotations

from typing import Any

LOCK_NAME = "monitored_jobs_write"
# A writer waits at most this long for warmers' reads (each is bounded by
# the dashboard query's own 5s statement_timeout) before giving up the write.
WRITE_LOCK_TIMEOUT_MS = 6000


def lock_for_write(cur: Any, restore_lock_timeout_ms: int) -> None:
    """Take the exclusive lock in the caller's transaction (released at
    commit/rollback). Raises on timeout like any lock_timeout. Leaves
    lock_timeout at ``restore_lock_timeout_ms`` for the statements after."""
    cur.execute("SET LOCAL lock_timeout = %s", (f"{WRITE_LOCK_TIMEOUT_MS}ms",))
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_NAME,))
    cur.execute("SET LOCAL lock_timeout = %s", (f"{int(restore_lock_timeout_ms)}ms",))


def try_lock_for_read(cur: Any) -> bool:
    """Take the shared lock in the caller's transaction, without waiting.
    False while a write holds or is waiting for it."""
    cur.execute("SELECT pg_try_advisory_xact_lock_shared(hashtext(%s))", (LOCK_NAME,))
    return bool(cur.fetchone()[0])
