"""Recruiter activity: where recruiters spend their time, and what they do.

  POST /api/v1/activity/time      body {surface, job_ref?, active_ms}
  POST /api/v1/activity/actions   body {actions: [{action, job_ref?, candidate_id?, attrs?}]}
  GET  /api/v1/admin/recruiter-activity?start_date&end_date&team_id&recruiter

The two writes are the browser's half (apps/web/hooks/use-active-time.ts,
apps/web/lib/activity.ts): active time on a surface (Rankings, the Assess
modal, the launch modals, the dashboards …) and the actions only the page
sees. Actions an endpoint can attribute are recorded server-side at that
endpoint (services/recruiter_activity.record_action). Both writes are
telemetry under the page: an unknown surface / action or a non-person
identity is ignored, and a DB failure answers 200 {"status": "error"} (the
client keeps the time and retries), never a 500. Under /api/ so nginx needs
no allowlist change.

The read: admins see everyone (or one team); team leads and org managers are
pinned to their own people (core.auth.resolve_report_scope); recruiters 403.
"""

import asyncio
import datetime
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth import UserIdentity, get_current_user, get_user_scope_emails, resolve_report_scope
from routers._helpers import _load_team_scope, get_db_connection
from services import recruiter_activity as ra

router = APIRouter(prefix="/api/v1", tags=["Recruiter Activity"])
logger = logging.getLogger(__name__)

MAX_ACTIONS_PER_REPORT = 20
MAX_RANGE_DAYS = 92
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_JOB_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class ActiveTimeReport(BaseModel):
    surface: str = Field(..., max_length=40)
    job_ref: Optional[str] = Field(default=None, max_length=64)
    # Negative is a client bug; oversized is clamped by the service.
    active_ms: int = Field(..., ge=0)


class ClientAction(BaseModel):
    action: str = Field(..., max_length=40)
    job_ref: Optional[str] = Field(default=None, max_length=64)
    candidate_id: Optional[str] = Field(default=None, max_length=200)
    attrs: Optional[Dict[str, Any]] = None


class ClientActionsReport(BaseModel):
    actions: List[ClientAction] = Field(default_factory=list, max_length=MAX_ACTIONS_PER_REPORT)


def _job_key(raw: Optional[str]) -> str:
    ref = (raw or "").strip()
    return ref if ref and _JOB_REF_RE.match(ref) else ""


def _record_time_sync(email: str, surface: str, job_key: str, active_ms: int) -> bool:
    conn = get_db_connection()
    try:
        return ra.record_active_time(conn, email, surface, job_key, active_ms)
    finally:
        conn.close()


@router.post("/activity/time")
async def record_activity_time(report: ActiveTimeReport, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if "@" not in email or report.surface not in ra.SURFACES:
        return {"status": "ignored"}
    try:
        ok = await asyncio.to_thread(_record_time_sync, email, report.surface, _job_key(report.job_ref), report.active_ms)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[RecruiterActivity] could not record {report.surface} time ({report.active_ms}ms): {e}")
        return {"status": "error"}
    return {"status": "success" if ok else "ignored"}


def _record_actions_sync(email: str, actions: List[ClientAction]) -> int:
    n = 0
    for a in actions:
        if a.action in ra.CLIENT_ACTIONS and ra.record_action(
            email, a.action, job_key=_job_key(a.job_ref) or None, candidate_id=a.candidate_id,
            attrs=a.attrs, source="client",
        ):
            n += 1
    return n


@router.post("/activity/actions")
async def record_activity_actions(report: ClientActionsReport, user: UserIdentity = Depends(get_current_user)):
    email = (user.email or "").strip().lower()
    if "@" not in email or not report.actions:
        return {"status": "ignored", "recorded": 0}
    recorded = await asyncio.to_thread(_record_actions_sync, email, report.actions)
    return {"status": "success", "recorded": recorded}


def _parse_day(raw: Optional[str], default: datetime.date) -> datetime.date:
    value = (raw or "").strip()
    if not value:
        return default
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail="Invalid date: expected YYYY-MM-DD.")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date: expected YYYY-MM-DD.")


def _compute_sync(scope_team_id: Optional[str], recruiter: Optional[str],
                  start: datetime.date, end: datetime.date) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        emails: Optional[List[str]] = None
        if scope_team_id:
            emails = list(_load_team_scope(conn, scope_team_id).get("emails") or [])
        if recruiter:
            emails = [recruiter] if emails is None or recruiter in emails else []
        return ra.fetch_activity(conn, start=start, end=end, emails=emails)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


@router.get("/admin/recruiter-activity")
async def get_recruiter_activity(
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; default 7 days ago"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD, Eastern; default today"),
    team_id: Optional[str] = Query(default=None, description="Admins only; team leads are pinned to their team"),
    recruiter: Optional[str] = Query(default=None),
    user: UserIdentity = Depends(get_current_user),
):
    """Per recruiter: active time by surface and day, actions, decision
    latency, searches per launch, time waiting on PAIR, and the New Relic id
    (recruiter_hash) that their NR events carry."""
    scope_team_id = resolve_report_scope(user, team_id, "recruiter activity")
    today = datetime.datetime.now(ra.EASTERN).date()
    end = _parse_day(end_date, today)
    start = _parse_day(start_date, end - datetime.timedelta(days=6))
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must not be after end_date.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days.")
    who = (recruiter or "").strip().lower() or None
    if who and not user.is_admin:
        allowed = {e.strip().lower() for e in await asyncio.to_thread(get_user_scope_emails, user) if e}
        if who not in allowed:
            raise HTTPException(status_code=403, detail="Access denied. That recruiter is not on your team.")
    try:
        data = await asyncio.to_thread(_compute_sync, scope_team_id, who, start, end)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"RECRUITER-ACTIVITY: failed ({scope_team_id or 'all'}, {start}..{end}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to compute recruiter activity.")
    return {"status": "success", "data": data}
