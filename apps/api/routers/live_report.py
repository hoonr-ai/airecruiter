import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.auth import UserIdentity, get_current_user, get_user_scope_emails
from core.db import get_db_connection

logger = logging.getLogger("live_report_router")

router = APIRouter(tags=["Live Report"])


def _get_external_interview_api_url() -> str:
    default_url = "https://pairbot.hoonr.ai" if os.getenv("ENVIRONMENT", "").lower() in {"production", "prod"} else "https://pairbotqa.hoonr.ai"
    return os.getenv("EXTERNAL_INTERVIEW_API_URL", default_url).rstrip("/")


def _get_pair_headers() -> Dict[str, str]:
    headers = {}
    pair_api_key = os.getenv("PAIR_API_KEY", "").strip()
    if pair_api_key:
        headers["Authorization"] = f"Bearer {pair_api_key}"
    elif os.getenv("ENVIRONMENT", "production").lower() in {"production", "prod"}:
        logger.warning("PAIR_API_KEY is not configured in production environment!")
    return headers


def _get_user_accessible_jobdiva_ids(user: UserIdentity) -> Optional[Set[str]]:
    """
    Returns the set of JobDiva IDs that this user is allowed to view.
    If user.is_admin, returns None (unrestricted, view all).
    For recruiters and team leads, queries monitored_jobs scoped by recruiter_emails.
    """
    if user.is_admin:
        return None

    allowed_emails = {e.lower().strip() for e in get_user_scope_emails(user) if e}
    if not allowed_emails:
        return set()

    accessible_ids: Set[str] = set()

    try:
        conn = get_db_connection()
        try:
            email_list = list(allowed_emails)
            with conn.cursor() as cur:
                try:
                    cur.execute("""
                        SELECT DISTINCT jobdiva_id
                        FROM monitored_jobs
                        WHERE jobdiva_id IS NOT NULL AND jobdiva_id != ''
                          AND (
                            recruiter_emails::jsonb ?| %s
                            OR lower(recruiter_emails::text) = ANY(%s)
                          )
                    """, (email_list, email_list))
                    for (jobdiva_id,) in cur.fetchall():
                        if jobdiva_id:
                            accessible_ids.add(str(jobdiva_id).strip())
                except Exception:
                    conn.rollback()
                    conditions = " OR ".join(["lower(recruiter_emails::text) LIKE %s" for _ in email_list])
                    params = [f"%{e}%" for e in email_list]
                    cur.execute(f"""
                        SELECT jobdiva_id, recruiter_emails
                        FROM monitored_jobs
                        WHERE jobdiva_id IS NOT NULL AND jobdiva_id != ''
                          AND ({conditions})
                    """, params)
                    for jobdiva_id, raw_emails in cur.fetchall():
                        if not raw_emails:
                            continue
                        clean_emails: Set[str] = set()
                        if isinstance(raw_emails, list):
                            clean_emails = {str(e).strip().lower() for e in raw_emails if e}
                        elif isinstance(raw_emails, str):
                            try:
                                parsed = json.loads(raw_emails) if raw_emails.strip().startswith("[") else [raw_emails]
                                clean_emails = {str(e).strip().lower() for e in parsed if e}
                            except Exception:
                                clean_emails = {raw_emails.strip().lower()}

                        if not clean_emails.isdisjoint(allowed_emails):
                            accessible_ids.add(str(jobdiva_id).strip())
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Error resolving accessible jobs for {user.email}: {e}")

    return accessible_ids


@router.get("/api/analytics/live-report/launches")
async def get_live_report_launches(user: UserIdentity = Depends(get_current_user)):
    """Fetch all bulk launches, isolated so recruiters only see their launched jobs."""
    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/launches"
    headers = _get_pair_headers()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch launches from PairBot: {resp.status_code} {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail="Failed to fetch launches from PairBot")
            launches = resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to PairBot service")

    retention_days = launches.get("retention_days", 14) if isinstance(launches, dict) else 14
    launch_list = launches.get("launches", []) if isinstance(launches, dict) else (launches if isinstance(launches, list) else [])

    # If Admin, return all launches with canonical shape
    if user.is_admin:
        return {"launches": launch_list, "retention_days": retention_days}

    # If Recruiter / Team Lead, filter by accessible jobdiva_ids
    accessible_ids = _get_user_accessible_jobdiva_ids(user)
    if not accessible_ids:
        return {"launches": [], "retention_days": retention_days}

    filtered = []
    for item in launch_list:
        item_jobdivas = {str(j).strip() for j in item.get("jobdiva_ids", [])}
        if not item_jobdivas.isdisjoint(accessible_ids):
            filtered.append(item)

    return {"launches": filtered, "retention_days": retention_days}


@router.get("/api/analytics/live-report/health")
async def get_live_report_health(user: UserIdentity = Depends(get_current_user)):
    """Fetch system health strip (restricted to Admins & Team Leads; generic status for recruiters)."""
    if not (user.is_admin or user.is_team_lead):
        return {"healthy": True}

    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/health"
    headers = _get_pair_headers()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch health from PairBot: {resp.status_code} {resp.text}")
                return {"healthy": False, "error": f"PairBot service returned {resp.status_code}"}
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot health: {e}")
        return {"healthy": False, "error": "PairBot service unavailable"}


@router.get("/api/analytics/live-report/{bulk_id}")
async def get_live_report_snapshot(
    bulk_id: str,
    reveal: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    """Fetch baseline snapshot of a specific launch, scoped to recruiter's jobs."""
    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/{bulk_id}"
    headers = _get_pair_headers()
    params = {"reveal": "true" if reveal else "false"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(target_url, headers=headers, params=params)
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Launch not found")
            if resp.status_code != 200:
                logger.error(f"Failed to fetch snapshot from PairBot: {resp.status_code} {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail="Failed to fetch launch snapshot")
            snapshot = resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot snapshot: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to PairBot service")

    # If Admin, return unmodified snapshot
    if user.is_admin:
        return snapshot

    # If Recruiter / Team Lead, filter snapshot jobs
    accessible_ids = _get_user_accessible_jobdiva_ids(user)
    scoped_jobs = []
    if accessible_ids and "jobs" in snapshot:
        for j in snapshot.get("jobs", []):
            if str(j.get("jobdiva_id", "")).strip() in accessible_ids:
                scoped_jobs.append(j)

    if not scoped_jobs:
        raise HTTPException(
            status_code=403,
            detail="Access denied. You do not have access to any jobs in this launch.",
        )

    scoped_snapshot = dict(snapshot)
    scoped_snapshot["jobs"] = scoped_jobs
    return scoped_snapshot


@router.get("/api/analytics/live-report/{bulk_id}/stream")
async def stream_live_report(
    bulk_id: str,
    request: Request,
    user: UserIdentity = Depends(get_current_user),
):
    """Proxy the SSE delta event stream from PairBot to the client with RBAC validation."""
    accessible_ids = _get_user_accessible_jobdiva_ids(user) if not user.is_admin else None
    if accessible_ids is not None and not accessible_ids:
        raise HTTPException(status_code=403, detail="Access denied. You do not have access to this launch stream.")

    # Resolve allowed interview IDs for non-admin recruiters (fails closed)
    allowed_interview_ids: Optional[Set[int]] = set() if accessible_ids is not None else None
    if accessible_ids is not None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as snap_client:
                snap_resp = await snap_client.get(
                    f"{_get_external_interview_api_url()}/api/analytics/live-report/{bulk_id}",
                    headers=_get_pair_headers(),
                )
                if snap_resp.status_code == 200:
                    snap_data = snap_resp.json()
                    allowed_interview_ids = set()
                    for job in snap_data.get("jobs", []):
                        if str(job.get("jobdiva_id", "")).strip() in accessible_ids:
                            for cand in job.get("candidates", []):
                                iid = cand.get("interview_id")
                                if iid:
                                    allowed_interview_ids.add(int(iid))
        except Exception as e:
            logger.warning(f"Failed to prefetch snapshot interview IDs for RBAC stream filtering: {e}")

    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/{bulk_id}/stream"
    headers = _get_pair_headers()

    async def event_generator():
        client_timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=client_timeout) as client:
                async with client.stream("GET", target_url, headers=headers) as upstream_response:
                    if upstream_response.status_code >= 400:
                        err_body = (await upstream_response.aread())[:200]
                        logger.error(f"PairBot live report stream rejected: {upstream_response.status_code} {err_body}")
                        yield b"data: {\"type\": \"error\", \"detail\": \"Upstream stream unavailable\"}\n\n"
                        return

                    # If admin, stream raw bytes directly
                    if allowed_interview_ids is None:
                        async for chunk in upstream_response.aiter_bytes():
                            if await request.is_disconnected():
                                break
                            yield chunk
                    else:
                        # For non-admin, filter by allowed_interview_ids
                        async for line in upstream_response.aiter_lines():
                            if await request.is_disconnected():
                                break
                            if line.startswith(":"):
                                # Heartbeat comment, always yield
                                yield f"{line}\n\n".encode("utf-8")
                            elif line.startswith("data:"):
                                raw_json = line[5:].strip()
                                try:
                                    data = json.loads(raw_json)
                                    iid = data.get("interview_id") or data.get("interviewId")
                                    # Allow connected/system events or events for candidates in recruiter's jobs
                                    if not iid or int(iid) in allowed_interview_ids:
                                        yield f"{line}\n\n".encode("utf-8")
                                except Exception:
                                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Live report stream proxy ended for bulk_id {bulk_id}: {e}")
            yield b": stream_closed\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
