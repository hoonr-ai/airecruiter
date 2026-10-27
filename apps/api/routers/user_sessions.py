"""Platform sessions (services/user_sessions.py).

  POST /api/v1/sessions/ping   body {session_id, active_ms, end?, user_name?, screen?, timezone?, language?, entry_path?}
  GET  /api/v1/admin/user-sessions?start_date&end_date&team_id&user

One write endpoint: the browser mints the session id, so its first ping is
the login and a ping with end="logout" is the Sign Out
(apps/web/lib/user-session.ts). Telemetry: a DB failure answers 200
{"status": "error"}, never a 500. The read follows recruiter-activity
scoping: admins see everyone (or one team), team leads their people,
recruiters 403.
"""

import asyncio
import datetime
import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import UserIdentity, get_current_user, get_user_scope_emails, resolve_report_scope
from routers._helpers import _load_team_scope, get_db_connection
from routers.recruiter_activity import MAX_RANGE_DAYS, _parse_day
from services import user_sessions as us
from services.recruiter_activity import EASTERN

router = APIRouter(prefix="/api/v1", tags=["User Sessions"])
logger = logging.getLogger(__name__)


class SessionPing(BaseModel):
    session_id: uuid.UUID
    active_ms: int = Field(default=0, ge=0)
    end: Optional[Literal["logout"]] = None
    # Device details the server cannot see; only the first ping needs them.
    user_name: Optional[str] = Field(default=None, max_length=200)
    screen: Optional[str] = Field(default=None, max_length=20)
    timezone: Optional[str] = Field(default=None, max_length=64)
    language: Optional[str] = Field(default=None, max_length=20)
    entry_path: Optional[str] = Field(default=None, max_length=300)


def _client_ip(request: Request) -> str:
    # The VM is internet-facing, so nginx's peer address is the client. nginx
    # overwrites X-Real-IP with it, but appends to X-Forwarded-For
    # ($proxy_add_x_forwarded_for), whose first entry is whatever the client
    # sent — so only its last entry is trustworthy.
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else ""


def _ping_sync(**kwargs) -> bool:
    conn = get_db_connection()
    try:
        return us.ping(conn, **kwargs)
    finally:
        conn.close()


@router.post("/sessions/ping")
async def session_ping(body: SessionPing, request: Request, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if "@" not in email:
        return {"status": "ignored"}
    try:
        ok = await asyncio.to_thread(
            _ping_sync, session_id=str(body.session_id), email=email, active_ms=body.active_ms,
            end_reason=body.end, user_name=body.user_name, role=user.role, team_name=user.team_name,
            ip=_client_ip(request), user_agent=request.headers.get("user-agent"), screen=body.screen,
            timezone=body.timezone, language=body.language, entry_path=body.entry_path,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[UserSessions] ping failed for session {body.session_id}: {e}")
        return {"status": "error"}
    # "ended": signed out (or not this user's id) — the client mints a new one.
    return {"status": "success" if ok else "ended"}


def _fetch_sync(scope_team_id: Optional[str], who: Optional[str],
                start: datetime.datetime, end: datetime.datetime) -> List[dict]:
    conn = get_db_connection()
    try:
        emails: Optional[List[str]] = None
        if scope_team_id:
            emails = list(_load_team_scope(conn, scope_team_id).get("emails") or [])
        if who:
            emails = [who] if emails is None or who in emails else []
        return us.fetch_sessions(conn, start=start, end=end, emails=emails)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


@router.get("/admin/user-sessions")
async def list_user_sessions(
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; default 7 days ago"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; default today"),
    team_id: Optional[str] = Query(default=None, description="Admins only; team leads are pinned to their team"),
    user_email: Optional[str] = Query(default=None, alias="user"),
    user: UserIdentity = Depends(get_current_user),
):
    """Each session: user, login / logout time, time spent, active time,
    device, OS, browser, IP, and how it ended."""
    scope_team_id = resolve_report_scope(user, team_id, "user sessions")
    today = datetime.datetime.now(EASTERN).date()
    end = _parse_day(end_date, today)
    start = _parse_day(start_date, end - datetime.timedelta(days=6))
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days.")
    who = (user_email or "").strip().lower() or None
    if who and not user.is_admin:
        allowed = {e.strip().lower() for e in await asyncio.to_thread(get_user_scope_emails, user) if e}
        if who not in allowed:
            raise HTTPException(status_code=403, detail="Access denied. That user is not on your team.")
    start_dt = datetime.datetime.combine(start, datetime.time.min, EASTERN)
    end_dt = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, EASTERN)
    try:
        rows = await asyncio.to_thread(_fetch_sync, scope_team_id, who, start_dt, end_dt)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"USER-SESSIONS: failed ({scope_team_id or 'all'}, {start}..{end}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load user sessions.")
    return {"status": "success", "data": rows}
