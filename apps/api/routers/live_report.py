import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.auth import UserIdentity, get_current_user

logger = logging.getLogger("live_report_router")

router = APIRouter(tags=["Live Report"])

def _get_external_interview_api_url() -> str:
    return os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai").rstrip("/")


def _require_admin_or_team_lead(user: UserIdentity) -> None:
    """RBAC dependency: Live report monitor is restricted to Admins and Team Leads."""
    if not (user.is_admin or user.is_team_lead):
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin or Team Lead access required.",
        )


def _get_pair_headers() -> Dict[str, str]:
    headers = {}
    pair_api_key = os.getenv("PAIR_API_KEY", "").strip()
    if pair_api_key:
        headers["Authorization"] = f"Bearer {pair_api_key}"
    return headers


@router.get("/api/analytics/live-report/launches")
async def get_live_report_launches(user: UserIdentity = Depends(get_current_user)):
    """Fetch all bulk launches (live, replayable, archived) from PairBot."""
    _require_admin_or_team_lead(user)
    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/launches"
    headers = _get_pair_headers()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch launches from PairBot: {resp.status_code} {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail="Failed to fetch launches from PairBot")
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to PairBot service")


@router.get("/api/analytics/live-report/health")
async def get_live_report_health(user: UserIdentity = Depends(get_current_user)):
    """Fetch system health strip (DB pool, worker fleet, queue backlog)."""
    _require_admin_or_team_lead(user)
    target_url = f"{_get_external_interview_api_url()}/api/analytics/live-report/health"
    headers = _get_pair_headers()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch health from PairBot: {resp.status_code} {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail="Failed to fetch system health from PairBot")
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot health: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to PairBot service")


@router.get("/api/analytics/live-report/{bulk_id}")
async def get_live_report_snapshot(
    bulk_id: str,
    reveal: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    """Fetch baseline snapshot of a specific launch."""
    _require_admin_or_team_lead(user)
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
            return resp.json()
    except httpx.RequestError as e:
        logger.error(f"Network error contacting PairBot snapshot: {e}")
        raise HTTPException(status_code=502, detail="Failed to connect to PairBot service")


@router.get("/api/analytics/live-report/{bulk_id}/stream")
async def stream_live_report(
    bulk_id: str,
    request: Request,
    user: UserIdentity = Depends(get_current_user),
):
    """Proxy the SSE delta event stream from PairBot to the client."""
    _require_admin_or_team_lead(user)
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
                        # Check if client disconnected
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
