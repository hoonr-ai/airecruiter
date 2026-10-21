"""Transactional outbox for side effects that must follow a DB change.

The problem it solves: code that commits a row (say, "passed email sent")
and then fires a side effect (a JobDiva note) loses the side effect if the
process dies between the two. With the outbox, the caller inserts the side
effect as a row with `add` *on the same cursor, before the same commit*, so
the change and its side effects commit or roll back together. `relay_once`
later claims due rows and runs them as core.queue tasks.

Delivery is at-least-once for tasks registered with ``max_tries > 1``: a row
whose relay died mid-run is reclaimed after `_STALE_AFTER`. A live relay
refreshes ``locked_at`` on its claimed rows every `_HEARTBEAT_SECONDS`, so a
task that runs longer than `_STALE_AFTER` (or waits behind one) is never
mistaken for an orphan. A row for a
``max_tries=1`` task (not safe to repeat) that is left running is marked
``dead`` instead, so it shows up for a human rather than running twice.
``dedupe_key`` makes `add` a no-op while a row with that key exists, so a
retried request cannot queue the same side effect twice.

The relay runs every few seconds: as an arq cron job in the worker when
ARQ_ENABLED, else on the API's scheduler. Rows are claimed with
``FOR UPDATE SKIP LOCKED``, so any number of relays can run at once. With
ARQ_ENABLED a dead worker stops delivery entirely, so the API checks
`oldest_pending_age_seconds` and logs an error when it passes
`LAG_ALERT_SECONDS`. `purge_old` deletes finished rows after `_RETENTION`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TABLE = "task_outbox"

_SCHEMA_STATEMENT = """
CREATE TABLE IF NOT EXISTS task_outbox (
    id BIGSERIAL PRIMARY KEY,
    task TEXT NOT NULL,
    args JSONB NOT NULL DEFAULT '[]'::jsonb,
    dedupe_key TEXT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 1,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ NULL,
    last_error TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""
_INDEX_STATEMENT = """
CREATE INDEX IF NOT EXISTS task_outbox_due_idx
    ON task_outbox (available_at, id) WHERE status IN ('pending', 'running')"""

SCHEMA_STATEMENTS: Tuple[str, ...] = (_SCHEMA_STATEMENT, _INDEX_STATEMENT)

_STALE_AFTER = "30 minutes"
_HEARTBEAT_SECONDS = 300  # well under _STALE_AFTER
_RELAY_CONCURRENCY = 8
_RETENTION = "14 days"
LAG_ALERT_SECONDS = 600
_BACKOFF_SECONDS = (30, 120, 600)
_ERROR_MAX_CHARS = 2000

_INSERT_SQL = """
    INSERT INTO task_outbox (task, args, dedupe_key, max_attempts)
    VALUES (%s, %s::jsonb, %s, %s)
    ON CONFLICT (dedupe_key) DO NOTHING
    RETURNING id
"""

# A row left 'running' by a dead relay: retry-safe tasks go back to pending,
# the rest are parked as dead.
_REAP_SQL = f"""
    UPDATE task_outbox
    SET status = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'dead' END,
        last_error = COALESCE(last_error, 'relay died while running'),
        locked_at = NULL,
        updated_at = now()
    WHERE status = 'running' AND locked_at < now() - interval '{_STALE_AFTER}'
"""

_CLAIM_SQL = """
    UPDATE task_outbox o
    SET status = 'running', attempts = o.attempts + 1, locked_at = now(), updated_at = now()
    WHERE o.id IN (
        SELECT id FROM task_outbox
        WHERE status = 'pending' AND available_at <= now()
        ORDER BY id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    RETURNING o.id, o.task, o.args, o.attempts, o.max_attempts
"""

_HEARTBEAT_SQL = "UPDATE task_outbox SET locked_at = now() WHERE id = ANY(%s) AND status = 'running'"

_PURGE_SQL = f"""
    DELETE FROM task_outbox
    WHERE status IN ('done', 'dead') AND updated_at < now() - interval '{_RETENTION}'
"""

_OLDEST_PENDING_SQL = """
    SELECT EXTRACT(EPOCH FROM now() - MIN(available_at)) FROM task_outbox
    WHERE status = 'pending' AND available_at <= now()
"""

_DONE_SQL = "UPDATE task_outbox SET status = 'done', locked_at = NULL, last_error = NULL, updated_at = now() WHERE id = %s"

_FAIL_SQL = """
    UPDATE task_outbox
    SET status = %s, available_at = now() + make_interval(secs => %s),
        locked_at = NULL, last_error = %s, updated_at = now()
    WHERE id = %s
"""


def _connect():
    from core.db import get_db_connection

    return get_db_connection()


def init_task_outbox_schema() -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        for stmt in SCHEMA_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def add(cur: Any, task_name: str, *args: Any, dedupe_key: Optional[str] = None) -> Optional[int]:
    """Queue ``task_name(*args)`` on the caller's cursor, inside its transaction.

    The caller commits. Returns the row id, or None when ``dedupe_key`` is
    already taken. Args must be JSON-serialisable.
    """
    import core.queue_tasks  # noqa: F401  (registers the tasks)
    from core.queue import registered_tasks

    t = registered_tasks().get(task_name)
    if t is None:
        raise KeyError(f"unknown queue task {task_name!r}")
    cur.execute(_INSERT_SQL, (task_name, json.dumps(list(args)), dedupe_key, t.max_tries))
    row = cur.fetchone()
    if row is None:
        logger.info("outbox_add_duplicate task=%s dedupe_key=%s", task_name, dedupe_key)
        return None
    return row["id"] if isinstance(row, dict) else row[0]


def _claim(limit: int) -> List[Tuple[int, str, list, int, int]]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(_REAP_SQL)
        cur.execute(_CLAIM_SQL, (limit,))
        rows = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in cur.fetchall()]
        conn.commit()
        cur.close()
        return rows
    finally:
        conn.close()


def _finish(row_id: int, error: Optional[str], attempts: int, max_attempts: int) -> None:
    conn = _connect()
    try:
        cur = conn.cursor()
        if error is None:
            cur.execute(_DONE_SQL, (row_id,))
        else:
            dead = attempts >= max_attempts
            delay = 0 if dead else _BACKOFF_SECONDS[min(attempts - 1, len(_BACKOFF_SECONDS) - 1)]
            cur.execute(_FAIL_SQL, ("dead" if dead else "pending", delay, error[:_ERROR_MAX_CHARS], row_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _execute(sql: str, params: Sequence[Any] = ()) -> Any:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        rowcount = cur.rowcount
        conn.commit()
        cur.close()
        return row if row is not None else rowcount
    finally:
        conn.close()


def _heartbeat(row_ids: List[int]) -> None:
    _execute(_HEARTBEAT_SQL, (row_ids,))


def purge_old() -> int:
    """Delete done/dead rows older than `_RETENTION`. Returns the count."""
    n = _execute(_PURGE_SQL)
    if n:
        logger.info("outbox_purged rows=%s", n)
    return n


def oldest_pending_age_seconds() -> Optional[float]:
    """Seconds the oldest due 'pending' row has waited, or None if none."""
    row = _execute(_OLDEST_PENDING_SQL)
    val = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return float(val) if val is not None else None


def check_lag() -> None:
    """Log an error when due rows are not being delivered (relay/worker down)."""
    try:
        age = oldest_pending_age_seconds()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox_lag_check_failed error=%s", exc)
        return
    from core.newrelic import record_custom_metric

    record_custom_metric("Custom/TaskOutbox/OldestPendingAgeSeconds", age or 0.0)
    if age is not None and age > LAG_ALERT_SECONDS:
        logger.error("outbox_relay_lagging oldest_pending_age_s=%.0f; is the worker running?", age)


async def _heartbeat_loop(live: set) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        if live:
            try:
                await asyncio.to_thread(_heartbeat, list(live))
            except Exception as exc:  # noqa: BLE001
                logger.warning("outbox_heartbeat_failed error=%s", exc)


async def relay_once(limit: int = 50) -> Dict[str, int]:
    """Claim up to ``limit`` due rows and run each one. Never raises."""
    import core.queue_tasks  # noqa: F401  (registers the tasks)
    from core.queue import registered_tasks

    counts = {"claimed": 0, "done": 0, "failed": 0}
    try:
        rows = await asyncio.to_thread(_claim, limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox_claim_failed error=%s", exc)
        return counts
    counts["claimed"] = len(rows)
    tasks = registered_tasks()
    live = {r[0] for r in rows}
    sem = asyncio.Semaphore(_RELAY_CONCURRENCY)

    async def run(row_id: int, task_name: str, args: Any, attempts: int, max_attempts: int) -> None:
        error: Optional[str] = None
        t = tasks.get(task_name)
        if t is None:
            error = f"unknown task {task_name!r}"
            attempts = max_attempts  # never retry an unknown task
        else:
            try:
                call_args: Sequence[Any] = args if isinstance(args, list) else json.loads(args)
                async with sem:
                    await asyncio.wait_for(t.func(*call_args), timeout=t.timeout_s)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
        if error is None:
            counts["done"] += 1
        else:
            counts["failed"] += 1
            logger.warning(
                "outbox_task_failed id=%s task=%s attempt=%s/%s error=%s",
                row_id, task_name, attempts, max_attempts, error,
            )
        try:
            await asyncio.to_thread(_finish, row_id, error, attempts, max_attempts)
        except Exception as exc:  # noqa: BLE001
            # The row stays 'running' and is reaped after _STALE_AFTER.
            logger.error("outbox_finish_failed id=%s error=%s", row_id, exc)
        finally:
            live.discard(row_id)

    heartbeat = asyncio.create_task(_heartbeat_loop(live))
    try:
        await asyncio.gather(*(run(*r) for r in rows))
    finally:
        heartbeat.cancel()
    if rows:
        logger.info("outbox_relay %s", counts)
    return counts
