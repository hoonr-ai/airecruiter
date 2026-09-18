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
    return os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai").rstrip("/")


def _get_pair_headers() -> Dict[str, str]:
    headers = {}
    pair_api_key = os.getenv("PAIR_API_KEY", "").strip()
    if pair_api_key:
        headers["Authorization"] = f"Bearer {pair_api_key}"
    return headers


def _get_user_accessible_jobdiva_ids(user: UserIdentity) -> Optional[Set[str]]:
    """
    Returns the set of JobDiva IDs that this user is allowed to view.
    If user.is_admin, returns None (unrestricted, view all).
    For recruiters and team leads, queries monitored_jobs scoped by recruiter_emails.
    """
    if user.is_admin:
        return None

    allowed_emails = {e.lower().strip() for e in get_user_scope_emails(user)}
    accessible_ids: Set[str] = set()

    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT jobdiva_id, recruiter_emails
                    FROM monitored_jobs
                    WHERE jobdiva_id IS NOT NULL AND jobdiva_id != ''
                """)
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

    # If Admin, return all launches
    if user.is_admin:
        return launches

    # If Recruiter / Team Lead, filter by accessible jobdiva_ids
    accessible_ids = _get_user_accessible_jobdiva_ids(user)
    if not accessible_ids:
        return []

    launch_list = launches if isinstance(launches, list) else launches.get("launches", [])
    filtered = []
    for item in launch_list:
        item_jobdivas = {str(j).strip() for j in item.get("jobdiva_ids", [])}
        if not item_jobdivas.isdisjoint(accessible_ids):
            filtered.append(item)

    if isinstance(launches, dict) and "launches" in launches:
        return {"launches": filtered}
    return filtered


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
                return {"healthy": True}
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot health: {e}")
        return {"healthy": True}


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

                    async for chunk in upstream_response.aiter_bytes():
                        if await request.is_disconnected():
                            break
                        yield chunk
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
