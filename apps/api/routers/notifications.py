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
import time
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
# How long a retained-but-disconnected cursor stays valid before a reconnect
# reseeds from the current max instead of resuming — bounds how much history
# a long-absent client would otherwise have replayed at it (see
# _register_subscriber). 1 hour comfortably covers a tab switch, a dropped
# wifi connection, or a short break; a client gone longer than that gets the
# cheaper "start from now" behavior instead of catching up from scratch (its
# own list() truth-up fetch already has everything it missed).
_CURSOR_MAX_RETENTION_SECONDS = 3600
# Separately, prune bookkeeping for emails that are both stale AND not
# currently connected, so _last_seen_id/_last_seen_cursor_at don't grow
# unbounded over a worker's lifetime.
_CURSOR_PRUNE_AFTER_SECONDS = 86400


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
                        candidate_id    TEXT,
                        title           TEXT NOT NULL,
                        body            TEXT,
                        score           NUMERIC,
                        metadata        JSONB DEFAULT '{}'::jsonb,
                        read_at         TIMESTAMPTZ,
                        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
                # Added after the initial CREATE TABLE shipped — ALTER (not
                # folded into the CREATE above) so it actually lands on a
                # table a prior deploy already created, which IF NOT EXISTS
                # on CREATE TABLE would otherwise silently skip.
                cur.execute(
                    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS cleared_at TIMESTAMPTZ;"
                )
                # candidate_id shipped NOT NULL (every notification was
                # candidate-level then). pair_launched/pair_inactive are
                # job-level — a launch has a candidate COUNT, not one
                # candidate, and an inactivity transition has none at all —
                # so candidate_id is NULL for those. Checked against
                # information_schema first: ALTER ... DROP NOT NULL takes a
                # brief ACCESS EXCLUSIVE lock even when there's nothing to
                # change, and every one of the 8 workers runs this at every
                # boot — skip it once the column is already nullable instead
                # of paying that lock on every single deploy.
                cur.execute(
                    """
                    SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 'notifications' AND column_name = 'candidate_id'
                    """
                )
                col = cur.fetchone()
                if col and col[0] == "NO":
                    cur.execute("ALTER TABLE notifications ALTER COLUMN candidate_id DROP NOT NULL;")
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
                # Job-level dedup (candidate_id is NULL for these two types, so
                # the index above never applies to them — see
                # services/notifications_service.py's _insert_notification).
                #
                # pair_inactive: "at most one ACTIVE notification per
                # recipient/job" — cleared_at IS NULL scopes it to the current
                # inactive period, so once the user clears it (or a future
                # reactivate-then-go-inactive-again cycle starts fresh), a new
                # one can be created. This is what makes it safe to fire the
                # in-app call independently of the email's success/retry in
                # main.py's _fire_pair_inactive_notification.
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_notifications_pair_inactive_active
                        ON notifications (recipient_email, jobdiva_id)
                        WHERE type = 'pair_inactive' AND cleared_at IS NULL;
                    """
                )
                # pair_launched's same-minute dedup used to be a unique index
                # on date_trunc('minute', created_at) — DROPPED: Postgres
                # classifies date_trunc(text, timestamptz) as STABLE, not
                # IMMUTABLE (its result depends on the session timezone), and
                # refuses to build an index on a non-immutable expression.
                # That CREATE INDEX statement therefore failed on every boot
                # since it shipped, which — because every statement in this
                # function runs in ONE transaction that only commits once at
                # the end — rolled back the ENTIRE schema-init pass each
                # time, silently discarding every other change in this
                # function too. Confirmed in QA: candidates kept getting the
                # PAIR-launched EMAIL (a separate, unaffected code path) but
                # never the in-app notification, because
                # create_pair_launched_notification's INSERT ... ON CONFLICT
                # referenced an index that had never actually been created,
                # which fails outright (not silently) — caught by that
                # function's own top-level try/except and logged, never
                # surfaced. Dedup for this type is now a plain pre-insert
                # existence check instead (see
                # services/notifications_service.py) — no index-expression
                # immutability to get wrong a second time.
                cur.execute(
                    "DROP INDEX IF EXISTS uniq_notifications_pair_launched_per_minute;"
                )
                # Not a dedup guarantee (that's the pre-insert check in
                # notifications_service.py) — just makes that check's own
                # lookup (recipient_email, jobdiva_id, type='pair_launched',
                # created_at >= ...) an index scan instead of a sequential
                # scan, since it now runs once per recipient on every launch.
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_pair_launched_recent
                        ON notifications (recipient_email, jobdiva_id, created_at)
                        WHERE type = 'pair_launched';
                    """
                )
                # Same purpose as idx_notifications_pair_launched_recent above,
                # for recruiter_assigned's identical pre-insert existence check
                # (see _job_level_duplicate_in_last_minute in
                # services/notifications_service.py) — the job wizard's
                # autosave can call this diff-and-notify path frequently.
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_recruiter_assigned_recent
                        ON notifications (recipient_email, jobdiva_id, created_at)
                        WHERE type = 'recruiter_assigned';
                    """
                )
                # Same purpose again, for zero_match_warning's identical
                # pre-insert existence check.
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_zero_match_warning_recent
                        ON notifications (recipient_email, jobdiva_id, created_at)
                        WHERE type = 'zero_match_warning';
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
        "SELECT COUNT(*) FROM notifications WHERE recipient_email = %s AND read_at IS NULL AND cleared_at IS NULL",
        (email,),
    )
    return cur.fetchone()[0]


def _list_notifications_sync(
    email: str, limit: int, before_id: Optional[int], unread_only: bool
) -> NotificationListResponse:
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["recipient_email = %s", "cleared_at IS NULL"]
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


def _mark_notification_unread_sync(notification_id: int, email: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE notifications
                SET read_at = NULL
                WHERE id = %s AND recipient_email = %s AND read_at IS NOT NULL
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


def _clear_notifications_sync(email: str) -> None:
    """Soft-delete (stamp cleared_at) rather than hard-delete, and clears
    the recipient's ENTIRE inbox, not just whatever page the client has
    fetched — same scope as mark-all-read.

    Hard-deleting was the original approach, but it's backwards from what
    made it look safe: the unique index on (recipient_email, candidate_id,
    jobdiva_id, type) only blocks a second row WHILE THE FIRST ONE STILL
    EXISTS. Delete it, and `reconcile_missed_pass_notifications` — which
    re-scans every Pass candidate in its lookback window on every run,
    independent of whether a notification already fired — finds no existing
    row to conflict with and happily inserts a new one, silently reviving a
    notification the user just cleared. Stamping cleared_at instead leaves
    the row in place (so the unique index keeps doing its job) and only
    changes what list/unread-count choose to show.
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET cleared_at = now() WHERE recipient_email = %s AND cleared_at IS NULL",
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


@router.post("/{notification_id}/unread")
async def mark_notification_unread(notification_id: int, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    updated = await asyncio.to_thread(_mark_notification_unread_sync, notification_id, email)
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


@router.post("/clear")
async def clear_notifications(user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    await asyncio.to_thread(_clear_notifications_sync, email)
    return {"success": True}


# ---------------------------------------------------------------------------
# SSE: one shared per-worker broadcaster instead of one DB poll per connection
# ---------------------------------------------------------------------------
_subscribers: Dict[str, Set["asyncio.Queue"]] = {}
_last_seen_id: Dict[str, int] = {}
# Monotonic timestamp of the last time _last_seen_id[email] was genuinely
# advanced (a reseed-to-now or a real delivery) — NOT touched by a reconnect
# that merely resumes an existing cursor. Backs the staleness check in
# _register_subscriber and the pruning pass in _broadcaster_loop.
_last_seen_cursor_at: Dict[str, float] = {}
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
                    WHERE recipient_email = %s AND id > %s AND cleared_at IS NULL
                    ORDER BY id ASC
                    LIMIT 50
                    """,
                    (email, since_id),
                )
                rows = cur.fetchall()
                if rows:
                    out[email] = rows
    return out


def _prune_stale_cursors() -> None:
    """Drop bookkeeping for emails that are both disconnected and old enough
    that nothing will ever resume from them — keeps _last_seen_id /
    _last_seen_cursor_at from growing for the life of the worker."""
    now = time.monotonic()
    stale = [
        e for e, at in _last_seen_cursor_at.items()
        if e not in _subscribers and (now - at) > _CURSOR_PRUNE_AFTER_SECONDS
    ]
    for e in stale:
        _last_seen_id.pop(e, None)
        _last_seen_cursor_at.pop(e, None)


async def _broadcaster_loop() -> None:
    while True:
        await asyncio.sleep(_STREAM_POLL_SECONDS)
        async with _get_subscribers_lock():
            emails = [e for e, qs in _subscribers.items() if qs]
        if not emails:
            _prune_stale_cursors()
            continue
        snapshot = {e: _last_seen_id.get(e, 0) for e in emails}
        try:
            rows_by_email = await asyncio.to_thread(_poll_new_notifications_sync, snapshot)
        except Exception as e:  # noqa: BLE001 - one bad tick must not kill the loop
            logger.warning("notifications_broadcaster_poll_failed: %s", e)
            continue
        for email, rows in rows_by_email.items():
            _last_seen_id[email] = max(_last_seen_id.get(email, 0), max(r["id"] for r in rows))
            _last_seen_cursor_at[email] = time.monotonic()
            async with _get_subscribers_lock():
                queues = list(_subscribers.get(email, ()))
            for row in rows:
                item = _row_to_item(row).model_dump()
                for q in queues:
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        logger.warning("notifications_stream_queue_full email=%s", email)
        _prune_stale_cursors()


async def _ensure_broadcaster_running() -> None:
    global _broadcaster_task
    async with _get_broadcaster_start_lock():
        if _broadcaster_task is None or _broadcaster_task.done():
            _broadcaster_task = asyncio.create_task(_broadcaster_loop())


async def _register_subscriber(
    email: str, queue: "asyncio.Queue", since_id: Optional[int] = None
) -> None:
    """Pick the cursor this subscriber (and the shared per-email broadcaster
    cursor) should resume from, in priority order:

    1. The client's own `since_id` (its highest-rendered notification id) —
       authoritative, standard SSE Last-Event-ID-style resume, and the only
       option that's correct across a reconnect landing on a DIFFERENT
       worker (the in-memory cursors below are per-process).
    2. A retained cursor from this email's last subscriber on THIS worker,
       if it's still fresh (< _CURSOR_MAX_RETENTION_SECONDS old) — resumes a
       short reconnect gap without a round trip.
    3. Otherwise, reseed from the current max id ("start from now"): a
       missing or stale cursor isn't worth catching up from scratch when the
       client's own list() truth-up fetch already has that history.
    """
    async with _get_subscribers_lock():
        is_new_email = email not in _subscribers
        _subscribers.setdefault(email, set()).add(queue)

    if since_id is not None:
        _last_seen_id[email] = max(0, since_id)
    elif is_new_email:
        retained = _last_seen_id.get(email)
        retained_at = _last_seen_cursor_at.get(email)
        is_fresh = retained_at is not None and (time.monotonic() - retained_at) <= _CURSOR_MAX_RETENTION_SECONDS
        if retained is None or not is_fresh:
            max_id = await asyncio.to_thread(_max_id_sync, email)
            _last_seen_id[email] = max_id
            _last_seen_cursor_at[email] = time.monotonic()
    # else: not a new email and no since_id — another tab's cursor for this
    # email is already live; nothing to seed.

    # Always ensure the broadcaster is running, even when the cursor logic
    # above took an early path (e.g. the very first subscriber on this
    # worker reconnecting with a cached since_id) — it must not depend on
    # having gone through the "new email, no since_id" branch above.
    await _ensure_broadcaster_running()


async def _unregister_subscriber(email: str, queue: "asyncio.Queue") -> None:
    async with _get_subscribers_lock():
        queues = _subscribers.get(email)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                del _subscribers[email]
                # _last_seen_id[email] is deliberately kept (not popped): a
                # reconnect within _CURSOR_MAX_RETENTION_SECONDS resumes from
                # it instead of skipping whatever arrived while disconnected
                # (the bug this fixed — see _register_subscriber for the
                # staleness bound and _prune_stale_cursors for eventual
                # cleanup).


@router.get("/stream")
async def stream_notifications(
    request: Request,
    since_id: Optional[int] = Query(
        None, ge=0, description="Resume after this notification id (the client's own high-water mark)"
    ),
    user: UserIdentity = Depends(get_current_user),
):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")

    async def event_generator():
        queue: "asyncio.Queue" = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        await _register_subscriber(email, queue, since_id=since_id)
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
