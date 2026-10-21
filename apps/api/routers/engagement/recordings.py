"""Engagement package: recordings."""

from fastapi import HTTPException
from typing import Optional, Any, Dict
import psycopg2.extras
import httpx
from routers._helpers import (
    get_db_connection,
)
from services.pair_auth import get_pair_auth_headers

from .common import EXTERNAL_INTERVIEW_API_URL, logger


def _recording_audit_row(interview_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT jobdiva_id, created_at
                FROM engage_interview_audit
                WHERE interview_id = %s
                ORDER BY id DESC
                LIMIT 1
                """,
                (interview_id,),
            )
            return cur.fetchone()


# Cap sequential S3 list_objects_v2 calls per request: when a session has no
# started_at, each one lists up to 6 prefixes (unknown-date plus a ±days
# fallback window around the interview's creation day).
_RECORDINGS_MAX_SESSIONS = 20


# ---------------------------------------------------------------------------
# 6. Outreach API Proxies
# ---------------------------------------------------------------------------
async def _proxy_get(path: str, params: dict = None):
    try:
        headers = get_pair_auth_headers()
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(f"{EXTERNAL_INTERVIEW_API_URL}{path}", params=params, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy GET {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

async def _proxy_post(path: str, json_data: dict = None):
    try:
        headers = get_pair_auth_headers(json_content_type=True)
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.post(f"{EXTERNAL_INTERVIEW_API_URL}{path}", json=json_data, headers=headers)
            res.raise_for_status()
            return res.json()
    except Exception as e:
        logger.error(f"❌ Proxy POST {path} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
