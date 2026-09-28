"""Apollo personal phone numbers via webhook (2026-09-28).

Apollo never puts personal / mobile numbers in the synchronous people/match
response. With ``reveal_phone_number=true`` it runs a separate phone job and
POSTs the result to ``webhook_url`` (8 credits per number found). Its documented
``poll_only`` mode does not work on our account: the job ends
``failed: "Webhook URL is missing"`` with 0 credits (probed 2026-09-28, twice).
So a public webhook is required.

Flow (Launch PAIR / the phone button — routers/candidates._enrich_candidate_contact_impl,
only while the candidate still lacks a phone after ZoomInfo):

  1. ``request_phone``: people/match with ``reveal_phone_number`` and our webhook
     ``{APP_BASE_URL}/api/webhooks/apollo/phone?token=<random>``. A pending row is
     stored in ``apollo_phone_requests`` keyed by the token's SHA-256 (the token
     itself is never stored) and the Apollo request_id.
  2. ``wait_for_phone``: up to APOLLO_PHONE_WAIT_S, watching that row (the webhook
     can land on any of the 8 workers). At the deadline, one call to Apollo's free
     ``GET /webhook_result/{request_id}``, which keeps the payload even when the
     delivery to us failed.
  3. ``deliver`` (webhook or poll, whichever is first; atomic, idempotent): picks
     the best number, then stores it on the row, in the contact cache, and on the
     candidate's sourced_candidates rows that still lack a phone. So a phone that
     arrives after step 2 gave up is still kept for next time.

Number choice (``pick_phone``): mobile > home > direct > other. The company
switchboard (work_hq) and ``invalid_number`` are never used. Numbers Apollo flags
on a do-not-call registry are skipped unless APOLLO_PHONE_ALLOW_DNC=true. PAIR's
own DNC list is still enforced at Launch.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"
APOLLO_WEBHOOK_RESULT_URL = "https://api.apollo.io/api/v1/webhook_result/{request_id}"
WEBHOOK_PATH = "/api/webhooks/apollo/phone"

# A delivery for a request older than this is refused (Apollo delivers within
# minutes; retries are hours at most).
REQUEST_TTL_S = 3 * 24 * 3600

_TYPE_RANK = {"mobile": 0, "cell": 0, "home": 1, "work_direct": 2, "direct": 2, "other": 3}
_SKIP_TYPES = {"work_hq", "hq", "organization", "company"}
_SKIP_STATUSES = {"invalid_number", "invalid", "no_status_invalid"}
_DNC_CLEAR = {"", "not_found", "none", "clear", "not_on_dnc"}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS apollo_phone_requests (
        token_hash TEXT PRIMARY KEY,
        request_id TEXT,
        apollo_person_id TEXT,
        linkedin_url TEXT,
        candidate_id TEXT,
        jobdiva_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        phone TEXT,
        phone_type TEXT,
        dnc_status TEXT,
        credits_consumed INTEGER,
        via TEXT,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        delivered_at TIMESTAMPTZ
    )
"""

INSERT_SQL = """
    INSERT INTO apollo_phone_requests
        (token_hash, request_id, apollo_person_id, linkedin_url, candidate_id, jobdiva_id)
    VALUES (%(token_hash)s, %(request_id)s, %(apollo_person_id)s, %(linkedin_url)s,
            %(candidate_id)s, %(jobdiva_id)s)
"""

GET_SQL = """
    SELECT token_hash, request_id, apollo_person_id, linkedin_url, candidate_id, jobdiva_id,
           status, phone, EXTRACT(EPOCH FROM (NOW() - requested_at)) AS age_s
    FROM apollo_phone_requests WHERE token_hash = %(token_hash)s
"""

# Atomic claim: only a pending row is settled, so a duplicate webhook, a retry,
# or the poll racing the webhook writes exactly once.
SETTLE_SQL = """
    UPDATE apollo_phone_requests
    SET status = %(status)s, phone = %(phone)s, phone_type = %(phone_type)s,
        dnc_status = %(dnc_status)s, credits_consumed = %(credits_consumed)s,
        via = %(via)s, delivered_at = NOW()
    WHERE token_hash = %(token_hash)s AND status = 'pending'
    RETURNING token_hash
"""

FILL_ROWS_SQL = """
    UPDATE sourced_candidates
    SET phone = %(phone)s, updated_at = CURRENT_TIMESTAMP
    WHERE candidate_id = %(candidate_id)s
      AND (%(jobdiva_id)s::text IS NULL OR jobdiva_id = %(jobdiva_id)s)
      AND COALESCE(phone, '') = ''
"""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _truthy(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def webhook_base_url() -> str:
    """Public origin Apollo can reach. Only an EXPLICIT setting counts:
    APOLLO_WEBHOOK_BASE_URL, else APP_BASE_URL (deploy-azure.sh writes it per
    environment: https://pairqa… on QA, https://pair… on PROD). A local run has
    neither, so it never registers a webhook that points at another env."""
    base = (os.getenv("APOLLO_WEBHOOK_BASE_URL") or os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
    return base if base.startswith("https://") else ""


def wait_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("APOLLO_PHONE_WAIT_S", "30")))
    except ValueError:
        return 30.0


def allow_dnc() -> bool:
    return _truthy("APOLLO_PHONE_ALLOW_DNC", "false")


def exa_after_timeout() -> bool:
    """When Apollo's number hasn't arrived by the deadline, still let the chain
    buy the phone from Exa (a late Apollo delivery is kept for next time)."""
    return _truthy("APOLLO_PHONE_EXA_AFTER_TIMEOUT", "true")


# Mobile credits run out separately from email credits: after Apollo refuses a
# reveal for credits, stop asking for a while (emails keep working).
PHONE_NO_CREDITS_COOLDOWN_S = 600.0
_phone_no_credits_until = 0.0


def phone_credits_exhausted() -> bool:
    return time.monotonic() < _phone_no_credits_until


def _trip_phone_credits(reason: str) -> None:
    global _phone_no_credits_until
    _phone_no_credits_until = time.monotonic() + PHONE_NO_CREDITS_COOLDOWN_S
    logger.error(
        "Apollo phone reveal refused for credits (%s): no Apollo phone lookups for %ds; "
        "Exa covers phones meanwhile",
        reason, int(PHONE_NO_CREDITS_COOLDOWN_S),
    )


def reveal_available() -> bool:
    from services import contact_enrichment as ce

    return (
        _truthy("APOLLO_PHONE_REVEAL_ENABLED", "true")
        and bool(webhook_base_url())
        and bool(ce.APOLLO_API_KEY)
        and not ce.apollo_out_of_credits()
        and not phone_credits_exhausted()
    )


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _digits_ok(number: str) -> bool:
    return sum(1 for ch in number if ch.isdigit()) >= 7


def pick_phone(payload: Any, person_id: str = "") -> Tuple[str, Dict[str, Any]]:
    """Best personal number in an Apollo phone payload, or ("", why).

    ``payload`` is the webhook body (== the poll endpoint's ``webhook_result``):
    ``{"status", "credits_consumed", "people": [{"id", "status",
    "phone_numbers": [{"sanitized_number", "raw_number", "type_cd",
    "status_cd", "dnc_status_cd", "confidence_cd"}]}]}``. With ``person_id``,
    only that person's numbers are considered.
    """
    from services.contact_enrichment import _normalise_phone

    meta: Dict[str, Any] = {"credits_consumed": None, "reason": ""}
    if not isinstance(payload, dict):
        meta["reason"] = "payload is not an object"
        return "", meta
    try:
        meta["credits_consumed"] = int(payload.get("credits_consumed")) if payload.get("credits_consumed") is not None else None
    except (TypeError, ValueError):
        meta["credits_consumed"] = None
    people = payload.get("people") if isinstance(payload.get("people"), list) else []
    if person_id:
        people = [p for p in people if isinstance(p, dict) and str(p.get("id") or "") == person_id]
        if not people:
            meta["reason"] = "no entry for the requested person"
            return "", meta

    options: List[Tuple[Tuple[int, int, int], str, Dict[str, Any]]] = []
    skipped_dnc = 0
    for person in people:
        if not isinstance(person, dict):
            continue
        for item in person.get("phone_numbers") or []:
            if not isinstance(item, dict):
                continue
            number = _normalise_phone(str(item.get("sanitized_number") or item.get("raw_number") or ""))
            if not _digits_ok(number):
                continue
            ptype = str(item.get("type_cd") or item.get("type") or "").strip().lower()
            status = str(item.get("status_cd") or item.get("status") or "").strip().lower()
            dnc = str(item.get("dnc_status_cd") or item.get("dnc_status") or "").strip().lower()
            if ptype in _SKIP_TYPES or status in _SKIP_STATUSES:
                continue
            if dnc not in _DNC_CLEAR and not allow_dnc():
                skipped_dnc += 1
                continue
            rank = (
                _TYPE_RANK.get(ptype, 3),
                0 if status == "valid_number" else 1,
                _CONFIDENCE_RANK.get(str(item.get("confidence_cd") or "").strip().lower(), 3),
            )
            options.append((rank, number, {"phone_type": ptype or None, "dnc_status": dnc or None}))
    if not options:
        meta["reason"] = (
            f"only do-not-call numbers ({skipped_dnc})" if skipped_dnc else "no usable number"
        )
        return "", meta
    options.sort(key=lambda o: o[0])
    _, number, extra = options[0]
    meta.update(extra)
    return number, meta


def payload_says_no_credits(payload: Any, failure_reason: Any = "") -> bool:
    text = json.dumps(payload)[:2000].lower() if isinstance(payload, (dict, list)) else str(payload or "").lower()
    text += " " + str(failure_reason or "").lower()
    return "credit" in text and ("insufficient" in text or "no more" in text or "out of" in text)


# ---------------------------------------------------------------------------
# Storage (sync; call through asyncio.to_thread)
# ---------------------------------------------------------------------------

_table_ready = False


def _connect():
    from core import db as _db
    return _db.get_db_connection()


def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('apollo_phone_requests_ddl'))")
        cur.execute(CREATE_SQL)
    conn.commit()
    _table_ready = True


def _run(sql: str, params: Dict[str, Any], *, fetch: bool = False) -> Any:
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone() if fetch else None
            cols = [d[0] for d in cur.description] if (fetch and cur.description) else []
            count = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if not fetch:
        return count
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else dict(zip(cols, row))


def get_request_sync(token_hash: str) -> Optional[Dict[str, Any]]:
    return _run(GET_SQL, {"token_hash": token_hash}, fetch=True)


# ---------------------------------------------------------------------------
# Request / wait / deliver
# ---------------------------------------------------------------------------

async def request_phone(
    candidate_id: str,
    linkedin_url: str,
    *,
    apollo_person_id: str = "",
    jobdiva_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ask Apollo for this person's phone numbers (delivered to our webhook).

    Returns ``{"token_hash", "request_id", "apollo_person_id"}`` for a job that
    is running, or None when no job was started (unavailable, refused, error).
    Never raises.
    """
    from services import contact_enrichment as ce

    if not reveal_available():
        return None
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    webhook_url = f"{webhook_base_url()}{WEBHOOK_PATH}?token={token}"
    # linkedin_url is the tested match key; the Apollo id (from the email call)
    # pins the same person when Apollo has it.
    body: Dict[str, Any] = {"linkedin_url": linkedin_url}
    if apollo_person_id:
        body["id"] = apollo_person_id
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": ce.APOLLO_API_KEY,
    }
    # The row must exist before Apollo can call back.
    try:
        await asyncio.to_thread(_run, INSERT_SQL, {
            "token_hash": token_hash, "request_id": None, "apollo_person_id": apollo_person_id or None,
            "linkedin_url": linkedin_url, "candidate_id": candidate_id, "jobdiva_id": jobdiva_id or None,
        })
    except Exception as e:
        logger.warning("apollo_phone: could not store the pending request for %s: %s", candidate_id, e)
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                APOLLO_MATCH_URL,
                headers=headers,
                params={"reveal_phone_number": "true", "webhook_url": webhook_url},
                json=body,
            )
    except Exception as e:
        logger.warning("apollo_phone: reveal request failed for %s: %s", candidate_id, e)
        await _settle(token_hash, status="failed", via="request")
        return None
    text = r.text or ""
    if r.status_code >= 400:
        if ce._is_apollo_no_credits(r.status_code, text):
            _trip_phone_credits(f"HTTP {r.status_code}")
        else:
            logger.warning("apollo_phone: reveal non-2xx for %s: %s %s", candidate_id, r.status_code, text[:200])
        await _settle(token_hash, status="failed", via="request")
        return None
    try:
        data = r.json()
    except ValueError:
        data = {}
    request_id = str(data.get("request_id") or "").strip()
    person = data.get("person") if isinstance(data.get("person"), dict) else {}
    person_id = str(person.get("id") or apollo_person_id or "").strip()
    if not request_id or not person:
        logger.info("apollo_phone: no phone job started for %s (no match / no request_id)", candidate_id)
        await _settle(token_hash, status="no_match", via="request")
        return None
    try:
        await asyncio.to_thread(_run, """
            UPDATE apollo_phone_requests SET request_id = %(request_id)s,
                   apollo_person_id = COALESCE(apollo_person_id, %(person_id)s)
            WHERE token_hash = %(token_hash)s
        """, {"request_id": request_id, "person_id": person_id or None, "token_hash": token_hash})
    except Exception as e:
        logger.warning("apollo_phone: could not store request_id for %s: %s", candidate_id, e)
    logger.info("apollo_phone: phone job %s started for %s", request_id, candidate_id)
    return {"token_hash": token_hash, "request_id": request_id, "apollo_person_id": person_id}


async def _settle(token_hash: str, *, status: str, via: str, phone: str = "",
                  meta: Optional[Dict[str, Any]] = None) -> bool:
    meta = meta or {}
    try:
        settled = await asyncio.to_thread(_run, SETTLE_SQL, {
            "token_hash": token_hash, "status": status, "phone": phone or None,
            "phone_type": meta.get("phone_type"), "dnc_status": meta.get("dnc_status"),
            "credits_consumed": meta.get("credits_consumed"), "via": via,
        }, fetch=True)
    except Exception as e:
        logger.warning("apollo_phone: settle failed (%s): %s", status, e)
        return False
    return settled is not None


async def deliver(row: Dict[str, Any], payload: Any, *, via: str) -> Dict[str, Any]:
    """Settle a pending request from an Apollo phone payload (webhook or poll).
    Returns ``{"settled": bool, "phone": str, "status": str}``; only the first
    caller for a request settles it."""
    from services import contact_cache

    phone, meta = pick_phone(payload, str(row.get("apollo_person_id") or ""))
    if not phone and payload_says_no_credits(payload):
        _trip_phone_credits("phone job result")
    status = "delivered" if phone else "no_phone"
    settled = await _settle(str(row["token_hash"]), status=status, via=via, phone=phone, meta=meta)
    if not settled:
        return {"settled": False, "phone": "", "status": "duplicate"}
    logger.info(
        "apollo_phone: %s for %s via %s (type=%s, credits=%s%s)",
        status, row.get("candidate_id"), via, meta.get("phone_type"), meta.get("credits_consumed"),
        f", {meta['reason']}" if meta.get("reason") else "",
    )
    if phone:
        await contact_cache.record(row.get("linkedin_url"), phone=phone, phone_provider="apollo")
        if row.get("candidate_id"):
            try:
                await asyncio.to_thread(_run, FILL_ROWS_SQL, {
                    "phone": phone, "candidate_id": row["candidate_id"], "jobdiva_id": row.get("jobdiva_id"),
                })
            except Exception as e:
                logger.warning("apollo_phone: could not fill sourced rows for %s: %s", row.get("candidate_id"), e)
    return {"settled": True, "phone": phone, "status": status}


async def poll_result(request_id: str) -> Tuple[str, Any, str]:
    """One call to Apollo's free poll endpoint.
    Returns (state, payload, failure_reason): state is "done" | "pending" | "gone"."""
    from services import contact_enrichment as ce

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                APOLLO_WEBHOOK_RESULT_URL.format(request_id=request_id),
                headers={"X-Api-Key": ce.APOLLO_API_KEY, "Accept": "application/json"},
            )
    except Exception as e:
        logger.info("apollo_phone: poll failed for %s: %s", request_id, e)
        return "pending", None, ""
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError:
            return "pending", None, ""
        return "done", body.get("webhook_result"), str(body.get("failure_reason") or "")
    if r.status_code == 404 and "pending" in (r.text or "").lower():
        return "pending", None, ""
    return "gone", None, f"HTTP {r.status_code}"


async def wait_for_phone(job: Dict[str, Any], *, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Wait for the phone job to finish. Returns
    ``{"phone": str, "state": "delivered" | "no_phone" | "timeout" | "failed"}``."""
    timeout = wait_seconds() if timeout is None else timeout
    token_hash = job["token_hash"]
    deadline = time.monotonic() + timeout
    while True:
        try:
            row = await asyncio.to_thread(get_request_sync, token_hash)
        except Exception as e:
            logger.info("apollo_phone: wait read failed: %s", e)
            row = None
        if row and row.get("status") != "pending":
            state = "delivered" if row.get("phone") else (row.get("status") or "no_phone")
            return {"phone": str(row.get("phone") or ""), "state": state}
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    # Backup: the delivery to our webhook may have failed or be slow; Apollo keeps
    # the result for 30 days and polling it costs nothing.
    if job.get("request_id") and row is not None:
        state, payload, failure = await poll_result(str(job["request_id"]))
        if state == "done":
            if payload_says_no_credits(payload, failure):
                _trip_phone_credits(failure or "poll result")
            outcome = await deliver(row, payload, via="poll")
            if outcome["settled"]:
                return {"phone": outcome["phone"], "state": outcome["status"]}
            again = await asyncio.to_thread(get_request_sync, token_hash)
            if again and again.get("status") != "pending":
                return {"phone": str(again.get("phone") or ""), "state": again.get("status") or "no_phone"}
    return {"phone": "", "state": "timeout"}
