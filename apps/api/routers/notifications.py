"""In-app notifications.

Currently a single notification type (`candidate_passed`, see
services/notifications_service.py for how/when rows are created). Scoped
strictly to the authenticated user's own email as `recipient_email` — no
team-lead/admin broadening, since a notification is "this is your candidate
who passed," not a team-wide report.

Schema/DDL convention mirrors routers/campaigns.py: a router-owned
`init_notifications_schema()` run once at startup from main.py's lifespan.

The `/stream` endpoint is SSE but, unlike routers/live_report.py (a pure
proxy to an external upstream), this backend IS the source of events: it
short-polls the `notifications` table every few seconds and yields any row
newer than the last one seen on this connection. This app's DB access is a
small per-worker pooled connection (core/db.py), which does not fit a
long-held LISTEN/NOTIFY connection per browser tab — short-polling is the
same trade-off this codebase already makes elsewhere (e.g. the
monitored_jobs_cache_warmer's 25s interval poll).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.auth import UserIdentity, get_current_user
from core.db import get_db_connection
from models import NotificationItem, NotificationListResponse, UnreadCountResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Notifications"])

_STREAM_POLL_SECONDS = 3.5


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
                        jobdiva_id      TEXT,
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
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_notifications_recipient_unread
                        ON notifications (recipient_email, read_at, created_at DESC);
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


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            return UnreadCountResponse(unread_count=_unread_count_for(cur, email))


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
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
    if not updated:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"success": True}


@router.post("/mark-all-read")
async def mark_all_notifications_read(user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")
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
    return {"success": True}


@router.get("/stream")
async def stream_notifications(request: Request, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="Authentication required")

    async def event_generator():
        last_seen_id = 0
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM notifications WHERE recipient_email = %s",
                        (email,),
                    )
                    last_seen_id = cur.fetchone()[0]
        except Exception as e:  # noqa: BLE001
            logger.warning("notifications_stream_init_failed email=%s: %s", email, e)

        yield b": connected\n\n"

        while True:
            if await request.is_disconnected():
                break
            try:
                with get_db_connection() as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(
                            """
                            SELECT id, type, job_id, jobdiva_id, candidate_id, title, body,
                                   score, metadata, read_at, created_at
                            FROM notifications
                            WHERE recipient_email = %s AND id > %s
                            ORDER BY id ASC
                            LIMIT 50
                            """,
                            (email, last_seen_id),
                        )
                        rows = cur.fetchall()
                if rows:
                    for row in rows:
                        last_seen_id = max(last_seen_id, row["id"])
                        item = _row_to_item(row).model_dump()
                        yield f"data: {json.dumps(item)}\n\n".encode("utf-8")
                else:
                    yield b": heartbeat\n\n"
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("notifications_stream_poll_failed email=%s: %s", email, e)
                yield b": heartbeat\n\n"
            await asyncio.sleep(_STREAM_POLL_SECONDS)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
