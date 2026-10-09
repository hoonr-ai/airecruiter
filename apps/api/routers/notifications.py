"""In-app notifications.

Currently a single notification type (`candidate_passed`, see
services/notifications_service.py for how/when rows are created). Scoped
strictly to the authenticated user's own email as `recipient_email` — no
team-lead/admin broadening, since a notification is "this is your candidate
who passed," not a team-wide report.

Schema/DDL convention mirrors routers/campaigns.py: a router-owned
`init_notifications_schema()` run once at startup from main.py's lifespan.

All read/write endpoints are `async def` but do their DB work inside
`asyncio.to_thread` (same pattern as `init_notifications_schema`) — psycopg2
is synchronous, and calling it directly from an `async def` body blocks the
event loop for every other request on this worker while the pool borrow and
query run.

The `/stream` endpoint is SSE but, unlike routers/live_report.py (a pure
proxy to an external upstream), this backend IS the source of events. Rather
than having every connected client poll the `notifications` table on its own
(one pooled DB connection per client per tick — doesn't scale with tabs), a
single per-worker background task (`_broadcaster_loop`) polls once per tick
for every currently-subscribed recipient email and fans new rows out to each
subscriber's in-memory `asyncio.Queue`. One DB connection per worker per
tick, regardless of how many browser tabs are connected. Still short-polling
rather than LISTEN/NOTIFY: this app's DB access is a small per-worker pooled
connection (core/db.py) that doesn't fit a long-held LISTEN/NOTIFY
connection per subscriber — the same trade-off this codebase already makes
elsewhere (e.g. the monitored_jobs_cache_warmer's 25s interval poll).
"""
import asyncio
import json
import logging
from typing import Dict, List, Optional, Set

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.auth import UserIdentity, get_current_user
from core.db import get_db_connection
from models import NotificationItem, NotificationListResponse, UnreadCountResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Notifications"])

_STREAM_POLL_SECONDS = 3.5
_STREAM_QUEUE_MAXSIZE = 200


def init_notifications_schema_sync() -> None:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        id              BIGSERIAL PRIMARY KEY,
                        recipient_email TEXT NOT NULL,
                        type            TEXT NOT NULL DEFAULT 'candidate_passed',
                        job_id          TEXT,
                        jobdiva_id      TEXT NOT NULL,
                        candidate_id    TEXT NOT NULL,
                        title           TEXT NOT NULL,
                        body            TEXT,
                        score           NUMERIC,
                        metadata        JSONB DEFAULT '{}'::jsonb,
                        read_at         TIMESTAMPTZ,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_recipient_created
                        ON notifications (recipient_email, created_at DESC);
                    """
                )
                # Partial index for the unread-count query (WHERE recipient_email = %s
                # AND read_at IS NULL): cheaper than scanning the composite index above
                # since it only has to index rows that are actually unread.
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_recipient_unread
                        ON notifications (recipient_email) WHERE read_at IS NULL;
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_notifications_recipient_candidate_job_type
                        ON notifications (recipient_email, candidate_id, jobdiva_id, type);
                    """
                )
                conn.commit()
    except Exception as e:  # noqa: BLE001 - schema init must never crash boot
        logger.error(f"init_notifications_schema failed: {e}")


async def init_notifications_schema() -> None:
    await asyncio.to_thread(init_notifications_schema_sync)


def _row_to_item(row) -> NotificationItem:
    return NotificationItem(
        id=row["id"],
        type=row["type"],
        job_id=row["job_id"],
        jobdiva_id=row["jobdiva_id"],
        candidate_id=row["candidate_id"],
        title=row["title"],
        body=row["body"],
        score=float(row["score"]) if row["score"] is not None else None,
        metadata=row.get("metadata") or {},
        read_at=row["read_at"].isoformat() if row["read_at"] else None,
        created_at=row["created_at"].isoformat(),
    )


def _unread_count_for(cur, email: str) -> int:
    cur.execute(
        "SELECT COUNT(*) FROM notifications WHERE recipient_email = %s AND read_at IS NULL",
        (email,),
    )
    return cur.fetchone()[0]


def _list_notifications_sync(
    email: str, limit: int, before_id: Optional[int], unread_only: bool
) -> NotificationListResponse:
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["recipient_email = %s"]
            params: list = [email]
            if unread_only:
                conditions.append("read_at IS NULL")
            if before_id is not None:
                conditions.append("id < %s")
                params.append(before_id)
            where_clause = " AND ".join(conditions)
            params.append(limit)
            cur.execute(
                f"""
                SELECT id, type, job_id, jobdiva_id, candidate_id, title, body,
                       score, metadata, read_at, created_at
                FROM notifications
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
            unread_count = _unread_count_for(cur, email)

    return NotificationListResponse(
        notifications=[_row_to_item(r) for r in rows],
        unread_count=unread_count,
    )


def _get_unread_count_sync(email: str) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            return _unread_count_for(cur, email)


def _mark_notification_read_sync(notification_id: int, email: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notifications
                SET read_at = now()
                WHERE id = %s AND recipient_email = %s AND read_at IS NULL
                RETURNING id
                """,
                (notification_id, email),
            )
            updated = cur.fetchone()
            conn.commit()
    return updated is not None


def _mark_all_notifications_read_sync(email: str) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notifications
                SET read_at = now()
                WHERE recipient_email = %s AND read_at IS NULL
                """,
                (email,),
            )
            conn.commit()


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    before_id: Optional[int] = Query(None, description="Return notifications with id < before_id (pagination cursor)"),
    unread_only: bool = Query(False),
    user: UserIdentity = Depends(get_current_user),
):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    return await asyncio.to_thread(_list_notifications_sync, email, limit, before_id, unread_only)


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    count = await asyncio.to_thread(_get_unread_count_sync, email)
    return UnreadCountResponse(unread_count=count)


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    updated = await asyncio.to_thread(_mark_notification_read_sync, notification_id, email)
    if not updated:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


@router.post("/mark-all-read")
async def mark_all_notifications_read(user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    await asyncio.to_thread(_mark_all_notifications_read_sync, email)
    return {"success": True}


# ---------------------------------------------------------------------------
# SSE: one shared per-worker broadcaster instead of one DB poll per connection
# ---------------------------------------------------------------------------
_subscribers: Dict[str, Set["asyncio.Queue"]] = {}
_last_seen_id: Dict[str, int] = {}
_broadcaster_task: Optional[asyncio.Task] = None

# Lazily constructed on first use, INSIDE an async function. asyncio.Lock()
# built at import time (module load, before any event loop is guaranteed to
# exist on this thread) raises on Python <3.10's `Lock.__init__`, which calls
# `get_event_loop()`. Deferring construction to first await — a single-
# threaded event loop never interleaves between the `is None` check and the
# assignment below, so this needs no lock of its own — sidesteps that
# entirely regardless of interpreter version.
_subscribers_lock: Optional[asyncio.Lock] = None
_broadcaster_start_lock: Optional[asyncio.Lock] = None


def _get_subscribers_lock() -> asyncio.Lock:
    global _subscribers_lock
    if _subscribers_lock is None:
        _subscribers_lock = asyncio.Lock()
    return _subscribers_lock


def _get_broadcaster_start_lock() -> asyncio.Lock:
    global _broadcaster_start_lock
    if _broadcaster_start_lock is None:
        _broadcaster_start_lock = asyncio.Lock()
    return _broadcaster_start_lock


def _max_id_sync(email: str) -> int:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(id), 0) FROM notifications WHERE recipient_email = %s",
                (email,),
            )
            return cur.fetchone()[0]


def _poll_new_notifications_sync(last_seen: Dict[str, int]) -> Dict[str, List[dict]]:
    """One DB connection, one query per subscribed email, each above its own
    last-seen id. Still far cheaper than one connection per browser tab."""
    out: Dict[str, List[dict]] = {}
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for email, since_id in last_seen.items():
                cur.execute(
                    """
                    SELECT id, type, job_id, jobdiva_id, candidate_id, title, body,
                           score, metadata, read_at, created_at
                    FROM notifications
                    WHERE recipient_email = %s AND id > %s
                    ORDER BY id ASC
                    LIMIT 50
                    """,
                    (email, since_id),
                )
                rows = cur.fetchall()
                if rows:
                    out[email] = rows
    return out


async def _broadcaster_loop() -> None:
    while True:
        await asyncio.sleep(_STREAM_POLL_SECONDS)
        async with _get_subscribers_lock():
            emails = [e for e, qs in _subscribers.items() if qs]
        if not emails:
            continue
        snapshot = {e: _last_seen_id.get(e, 0) for e in emails}
        try:
            rows_by_email = await asyncio.to_thread(_poll_new_notifications_sync, snapshot)
        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the loop
            logger.warning("notifications_broadcaster_poll_failed: %s", e)
            continue
        for email, rows in rows_by_email.items():
            _last_seen_id[email] = max(_last_seen_id.get(email, 0), max(r["id"] for r in rows))
            async with _get_subscribers_lock():
                queues = list(_subscribers.get(email, ()))
            for row in rows:
                item = _row_to_item(row).model_dump()
                for q in queues:
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        logger.warning("notifications_stream_queue_full email=%s", email)


async def _ensure_broadcaster_running() -> None:
    global _broadcaster_task
    async with _get_broadcaster_start_lock():
        if _broadcaster_task is None or _broadcaster_task.done():
            _broadcaster_task = asyncio.create_task(_broadcaster_loop())


async def _register_subscriber(email: str, queue: "asyncio.Queue") -> None:
    async with _get_subscribers_lock():
        is_new_email = email not in _subscribers
        _subscribers.setdefault(email, set()).add(queue)
    if is_new_email:
        # Seed from "now" so a fresh subscriber doesn't replay old history —
        # anything before this point was already delivered by the client's
        # own truth-up list() fetch on mount/reconnect.
        max_id = await asyncio.to_thread(_max_id_sync, email)
        _last_seen_id.setdefault(email, max_id)
    await _ensure_broadcaster_running()


async def _unregister_subscriber(email: str, queue: "asyncio.Queue") -> None:
    async with _get_subscribers_lock():
        queues = _subscribers.get(email)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del _subscribers[email]
                _last_seen_id.pop(email, None)


@router.get("/stream")
async def stream_notifications(request: Request, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")

    async def event_generator():
        queue: "asyncio.Queue" = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        await _register_subscriber(email, queue)
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_STREAM_POLL_SECONDS)
                    yield f"data: {json.dumps(item)}\n\n".encode("utf-8")
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await _unregister_subscriber(email, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
