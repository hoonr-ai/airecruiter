"""Recruiter warm handoff — Pair's side of the contract with Pair-bot.

Pair-bot holds a candidate's call open after its last question and asks the
launch's recruiter to join.
This module:

  * records the signed `handoff_*` webhooks, once each (Pair-bot retries, and
    retries can arrive out of order), in `recruiter_handoff_events`;
  * turns `handoff_requested` into an in-app notification for the one
    recruiter who can join, carrying the join path; and `handoff_missed`
    into a follow-up notification for every recruiter on the launch;
  * retires the "join now" notification once the handoff is taken or over;
  * calls Pair-bot's claim / close / state endpoints on behalf of the
    logged-in recruiter. The email sent is always the server-resolved login,
    never a request field, so a recruiter can only ever claim as themself.

Raw psycopg2 + startup DDL, like the rest of this app.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from core.db import get_db_connection
from services.pair_auth import get_pair_auth_headers

logger = logging.getLogger(__name__)

EVENTS = ("handoff_requested", "handoff_claimed", "handoff_closed", "handoff_missed", "handoff_cancelled")

NOTIFY_REQUESTED = "recruiter_handoff_requested"
NOTIFY_MISSED = "recruiter_handoff_missed"


def join_path(interview_id: int) -> str:
    """The in-app join page. Pair-bot's RECRUITER_HANDOFF_JOIN_URL_TEMPLATE
    should point at `<app origin>/handoff/{interview_id}`."""
    return f"/handoff/{int(interview_id)}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_recruiter_handoff_schema_sync() -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recruiter_handoff_events (
                    interview_id  BIGINT      NOT NULL,
                    event         TEXT        NOT NULL,
                    payload       JSONB       NOT NULL,
                    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (interview_id, event)
                );
                """
            )
            # The join-now notification is found again by its interview id
            # when the handoff ends, to retire it.
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_notifications_handoff_interview
                    ON notifications ((metadata->>'interview_id'))
                    WHERE type = 'recruiter_handoff_requested';
                """
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Webhook processing
# ---------------------------------------------------------------------------

def _clean_emails(raw: Any) -> List[str]:
    out: List[str] = []
    for e in raw or []:
        e = str(e or "").strip().lower()
        if e and e not in out:
            out.append(e)
    return out


def _insert_notification(cur, *, ntype: str, recipient: str, payload: Dict[str, Any],
                         title: str, body: str) -> None:
    interview_id = int(payload["interview_id"])
    metadata = {
        "interview_id": interview_id,
        "join_path": join_path(interview_id),
        "expires_at": payload.get("expires_at"),
        "candidate_name": payload.get("candidate_name"),
    }
    cur.execute(
        """
        INSERT INTO notifications
            (recipient_email, type, job_id, jobdiva_id, candidate_id, title, body, metadata)
        VALUES (%s, %s, NULL, %s, NULL, %s, %s, %s::jsonb)
        """,
        (recipient, ntype, str(payload.get("jobdiva_id") or ""), title, body, json.dumps(metadata)),
    )


_NOTIFICATION_FIELD_MAX_CHARS = 200


def apply_event(payload: Dict[str, Any]) -> str:
    """Record one webhook. Returns "recorded", or "duplicate" for a retry of
    an event already seen for this interview."""
    event = payload["event"]
    interview_id = int(payload["interview_id"])
    # Capped before landing in notification title/body text: these are
    # signed (so the risk is low — PR review), but nothing upstream bounds
    # their length, and an oversized value here would otherwise ride
    # straight into a user-facing notification unvalidated.
    candidate = (payload.get("candidate_name") or "A candidate")[:_NOTIFICATION_FIELD_MAX_CHARS]
    role = (payload.get("role_position") or "the role")[:_NOTIFICATION_FIELD_MAX_CHARS]
    recruiter = (payload.get("recruiter_email") or "").strip().lower()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO recruiter_handoff_events (interview_id, event, payload)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (interview_id, event) DO NOTHING
                RETURNING interview_id
                """,
                (interview_id, event, json.dumps(payload)),
            )
            if cur.fetchone() is None:
                conn.commit()
                return "duplicate"

            # A later state has already arrived (retries can reorder):
            # never raise a "join now" for a handoff that is already over.
            cur.execute(
                "SELECT 1 FROM recruiter_handoff_events WHERE interview_id = %s AND event <> ALL(%s) LIMIT 1",
                (interview_id, ["handoff_requested"]),
            )
            already_moved_on = cur.fetchone() is not None

            if event == "handoff_requested" and recruiter and not already_moved_on:
                _insert_notification(
                    cur, ntype=NOTIFY_REQUESTED, recipient=recruiter, payload=payload,
                    title=f"{candidate} is waiting for you",
                    body=f"Finished the PAIR interview for {role}. Join the call now.",
                )
            elif event != "handoff_requested":
                cur.execute(
                    """
                    UPDATE notifications SET read_at = COALESCE(read_at, now())
                     WHERE type = %s AND metadata->>'interview_id' = %s
                    """,
                    (NOTIFY_REQUESTED, str(interview_id)),
                )
            if event == "handoff_missed":
                for email in _clean_emails([recruiter, *(payload.get("cc_recruiter_emails") or [])]):
                    _insert_notification(
                        cur, ntype=NOTIFY_MISSED, recipient=email, payload=payload,
                        title=f"Missed handoff: {candidate}",
                        body=f"No recruiter joined {candidate}'s call for {role} in time. Please follow up.",
                    )
        conn.commit()
    return "recorded"


# ---------------------------------------------------------------------------
# Calls to Pair-bot as the logged-in recruiter
# ---------------------------------------------------------------------------

class PairbotHandoffError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _base_url() -> str:
    """Pair-bot's base URL — EXTERNAL_INTERVIEW_API_URL if set, else a
    default that follows the environment, same pattern already used by
    routers/live_report.py's _get_external_interview_api_url() for the same
    variable. Previously defaulted to the QA host unconditionally: a prod
    deploy missing this one env var would silently send recruiter claims,
    emails, and state reads to QA's Pair-bot instead of prod's, with no
    error (PR review).
    """
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    default_url = "https://pairbot.hoonr.ai" if env in {"production", "prod"} else "https://pairbotqa.hoonr.ai"
    return os.getenv("EXTERNAL_INTERVIEW_API_URL", default_url).rstrip("/")


async def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, url, json=body,
                                        headers={**get_pair_auth_headers(json_content_type=True), **(headers or {})})
    except httpx.HTTPError as exc:
        logger.warning("pairbot handoff %s %s failed: %s", method, path, exc)
        raise PairbotHandoffError(502, "PAIR-bot is unreachable") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail") or resp.text
        except ValueError:
            detail = resp.text
        raise PairbotHandoffError(resp.status_code, str(detail)[:500])
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("pairbot handoff %s %s returned a non-object body: %.200s", method, path, resp.text)
        raise PairbotHandoffError(502, "PAIR-bot sent an unexpected reply")
    return data


async def claim(interview_id: int, recruiter_email: str) -> Dict[str, Any]:
    return await _call("POST", f"/api/interviews/{int(interview_id)}/handoff/claim",
                       {"recruiter_email": recruiter_email})


async def close(interview_id: int, recruiter_email: str, notes: Optional[str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"recruiter_email": recruiter_email}
    if notes:
        body["notes"] = notes
    return await _call("POST", f"/api/interviews/{int(interview_id)}/handoff/close", body)


async def state(interview_id: int) -> Dict[str, Any]:
    return await _call("GET", f"/api/interviews/{int(interview_id)}/handoff")


# Recruiter copilot reads (Pair-bot docs/recruiter-copilot-design.md §4).
# Pair-bot allows only the handoff's own recruiter; the login email rides as
# an X-Recruiter-Email header on these GETs (kept out of URLs and access
# logs), as claim/close carry it in the body.

def _as(recruiter_email: str) -> Dict[str, str]:
    return {"X-Recruiter-Email": recruiter_email}


async def context(interview_id: int, recruiter_email: str) -> Dict[str, Any]:
    return await _call("GET", f"/api/interviews/{int(interview_id)}/handoff/context",
                       headers=_as(recruiter_email))


async def transcript(interview_id: int, recruiter_email: str) -> Dict[str, Any]:
    return await _call("GET", f"/api/interviews/{int(interview_id)}/handoff/transcript",
                       headers=_as(recruiter_email))


async def recording(interview_id: int, recruiter_email: str) -> Dict[str, Any]:
    return await _call("GET", f"/api/interviews/{int(interview_id)}/handoff/recording",
                       headers=_as(recruiter_email))


# ---------------------------------------------------------------------------
# Resume, from Pair's own DB
# ---------------------------------------------------------------------------

def resume_for_interview_sync(interview_id: int) -> Optional[Dict[str, Any]]:
    """The candidate's resume for a Pair-bot interview id, or None when Pair
    never launched it. engage_interview_audit maps interview_id -> (candidate,
    job) at launch; sourced_candidates holds resume_text per (candidate, job).
    """
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT candidate_id, jobdiva_id, candidate_name
                  FROM engage_interview_audit
                 WHERE interview_id = %s
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (str(int(interview_id)),),
            )
            audit = cur.fetchone()
            if not audit:
                return None
            candidate_id, jobdiva_id, candidate_name = audit[0], audit[1], audit[2]
            # Only the launch's own job row: another job's resume may be a
            # different version, or one this recruiter has no access to.
            cur.execute(
                """
                SELECT name, resume_text
                  FROM sourced_candidates
                 WHERE candidate_id = %s AND jobdiva_id = %s
                   AND COALESCE(resume_text, '') <> ''
                 LIMIT 1
                """,
                (str(candidate_id), str(jobdiva_id or "")),
            )
            row = cur.fetchone()
    return {
        "candidate_name": (row[0] if row else None) or candidate_name,
        "resume_text": (row[1] if row else None) or None,
    }
