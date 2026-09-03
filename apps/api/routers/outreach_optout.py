"""Do-not-contact endpoints — "Stop outreach" from inside pair.

pair launches the outreach (POST /api/bulk-interviews) but, before this
router, only pair-bot could stop it, and only through pair-bot's own
dashboard. A recruiter in pair who heard "stop contacting me" had to leave
pair, find the candidate in pair-bot and click a second button — and if they
didn't, the automated reminder *calls* kept going. pair-bot's side of the
contract is documented in OPT_OUT_API.md, which the pair-bot team owns and is
not vendored here — ask them for the current copy rather than trusting a stale
one.

Two writes happen per stop, and both matter:

1. **pair-bot** (services/opt_out.py) suppresses the contact and cancels the
   queued sends. It owns every channel, so this is the write that actually
   stops the calls. It is also the only one that can reach the candidate's
   *other* interviews — pair-bot de-duplicates on
   ``(candidate_id, LOWER(role_position))``, so "Data Engineer" and "Data
   Engineer II" are two rows with two independent schedules.
2. **pair's local DNC** (services/dnc_storage.py) records the same suppression
   here, so the existing ``dnc_stopped_at`` gates keep *us* from re-launching
   the candidate on the next import. Without it, a candidate re-sourced next
   month arrives as a fresh row with a NULL flag and sails back through
   Launch PAIR.

Mounted under /api/v1 so the existing nginx `location /api/` passthrough
routes it — no nginx allowlist change needed. The prefix is /api/v1/outreach
rather than the /api/v1/candidates that mirrors pair-bot's own path, because
routers/candidate_processing.py already owns a `GET /{jobdiva_id}` there and
would shadow `GET /api/v1/candidates/opt-out`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import UserIdentity, get_current_user
from routers._helpers import get_db_connection
from services.dnc_storage import (
    local_suppression_status,
    record_opt_out_audit,
    release_contact_locally,
    suppress_contact_locally,
)
from utils.pii import mask_email, mask_phone
from services.opt_out import (
    PairBotOptOutError,
    normalize_channels,
    pairbot_opt_in,
    pairbot_opt_out,
    pairbot_opt_out_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/outreach", tags=["Outreach Opt-Out"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class OptOutRequest(BaseModel):
    # Any one of these four identifies the person. candidate_id is pair's own
    # key and is not something pair-bot understands — we resolve it to contact
    # details below.
    candidate_id: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    interview_id: Optional[int] = None
    reason: Optional[str] = None
    # Omitted → pair-bot defaults to all three channels, which is what "stop
    # contacting me" means. Narrow it only when the candidate did.
    channels: Optional[List[str]] = None
    # Omitted → pair-bot defaults to scope "curate" (pair's own tenant).
    # Pass "global" when the candidate means every product we run.
    scope: Optional[str] = None


class OptInRequest(BaseModel):
    candidate_id: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    reason: Optional[str] = None
    # Omitted → pair-bot clears EVERY scope. Deliberately asymmetric with
    # opt-out; see services/opt_out.pairbot_opt_in.
    scope: Optional[str] = None


# ---------------------------------------------------------------------------
# Contact resolution
# ---------------------------------------------------------------------------
def _resolve_contact(candidate_id: str) -> Dict[str, Optional[str]]:
    """Latest known email/phone for a pair candidate_id.

    Returns empty strings rather than raising: the caller may still have an
    interview_id or a typed-in address to work with, and refusing to stop
    outreach because a lookup missed is the wrong failure mode.
    """
    out: Dict[str, Optional[str]] = {"email": None, "phone": None, "name": None}
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT email, phone, name
                    FROM sourced_candidates
                    WHERE candidate_id = %s
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (candidate_id,),
                )
                row = cur.fetchone()
            if row:
                out["email"] = (row[0] or "").strip().lower() or None
                out["phone"] = (row[1] or "").strip() or None
                out["name"] = (row[2] or "").strip() or None
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"opt_out_contact_lookup_failed candidate_id={candidate_id}: {e}")
    return out


def _merge_identifiers(
    candidate_id: Optional[str],
    email: Optional[str],
    phone: Optional[str],
) -> Dict[str, Optional[str]]:
    """Caller-supplied contact details win; the DB fills in what's missing.

    Both email and phone are sent to pair-bot whenever both are known. They are
    stored there as two separate identities, and a suppression recorded against
    only the address will not match a Twilio STOP that later arrives carrying
    only the number.

    The address is lower-cased here, not just trimmed. pair-bot documents that
    it normalizes case server-side, and dnc_storage lower-cases for its own
    matching — but sending the canonical form means the two sides agree no
    matter what the caller typed, instead of relying on that promise for the
    half of the write that stops the actual sending.
    """
    resolved = {
        "email": (email or "").strip().lower() or None,
        "phone": (phone or "").strip() or None,
        "name": None,
    }
    if candidate_id and not (resolved["email"] and resolved["phone"]):
        looked_up = _resolve_contact(candidate_id)
        resolved["email"] = resolved["email"] or looked_up["email"]
        resolved["phone"] = resolved["phone"] or looked_up["phone"]
        resolved["name"] = looked_up["name"]
    return resolved


def _pairbot_http_error(
    e: PairBotOptOutError,
    unreachable_prefix: str,
    extra_note: str = "",
) -> HTTPException:
    """Translate a pair-bot failure into a status the recruiter can act on.

    A 4xx is a request the caller can fix and means nothing was suppressed
    anywhere. Anything else (5xx, timeout, DNS) means the call did not reach
    pair-bot and must be retried — 502, never 500, so it is not confused with
    a bug in pair.

    401/403 collapse to 401 so the browser's own auth handling kicks in rather
    than rendering a "forbidden" a recruiter cannot act on: a rejected M2M key
    is an ops problem, not a permissions one.
    """
    if e.status_code and 400 <= e.status_code < 500:
        status = 401 if e.status_code in (401, 403) else e.status_code
        return HTTPException(status_code=status, detail=e.message)
    detail = f"{unreachable_prefix}: {e.message}."
    if extra_note:
        detail = f"{detail} {extra_note}"
    return HTTPException(status_code=502, detail=detail)


def _local_fallback_note(local: Dict[str, Any]) -> str:
    """What to tell the recruiter about the local half when pair-bot is down.

    Only claim a local suppression when one was actually recorded. There are
    two ways it is not: an interview_id-only request (this endpoint accepts
    one, but the local DNC list is keyed on contact details, so
    suppress_contact_locally has nothing to write), or a failed write. Saying
    "marked do-not-contact in pair" in either case reads as a partial success
    and invites the recruiter to treat a candidate who is still being called as
    handled — the exact failure this feature exists to prevent.
    """
    recorded = not local.get("error") and local.get("locally_suppressed")
    if recorded:
        return (
            "The candidate has been marked do-not-contact in pair, so no new "
            "outreach will be launched, but already-queued messages and calls "
            "were not cancelled — please retry."
        )
    return (
        "Nothing was suppressed on either side — this candidate is still "
        "being contacted. Retry, and escalate if it keeps failing."
    )


# ---------------------------------------------------------------------------
# POST /api/v1/outreach/opt-out
# ---------------------------------------------------------------------------
@router.post("/opt-out")
async def stop_outreach(
    request: OptOutRequest,
    user: UserIdentity = Depends(get_current_user),
) -> Dict[str, Any]:
    """Stop contacting a candidate — every channel, every interview.

    Authenticated but not admin-gated: hearing "stop contacting me" is a
    recruiter's job, and making them escalate to an admin is how the calls
    keep going in the meantime.

    Ordering: pair-bot first, local DNC second. A 4xx from pair-bot means the
    request was rejected and nothing was suppressed on either side, which is
    the consistent outcome. When pair-bot is *unreachable* we still write the
    local suppression before failing — pair may not be able to stop the queued
    sends, but it can at least refuse to add more.
    """
    contact = _merge_identifiers(request.candidate_id, request.email, request.phone)
    email, phone = contact["email"], contact["phone"]

    if not email and not phone and request.interview_id is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Need an email, a phone number or an interview id to stop "
                "outreach. No contact details are on file for this candidate."
            ),
        )

    try:
        channels = normalize_channels(request.channels)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    reason = (request.reason or "").strip() or None
    actor = user.email or "unknown"
    # Stamp the actor into the reason pair-bot stores: its audit trail is the
    # one the on-call engineer reads first, and "Candidate asked to stop" with
    # no attribution is the answer nobody can follow up on.
    upstream_reason = f"{reason} (via pair, {actor})" if reason else f"Stopped in pair by {actor}"

    try:
        pairbot_response = await pairbot_opt_out(
            email=email,
            phone=phone,
            interview_id=request.interview_id,
            reason=upstream_reason,
            channels=channels,
            scope=request.scope,
        )
    except PairBotOptOutError as e:
        local: Dict[str, Any] = {}
        note = ""
        if e.retryable:
            local = suppress_contact_locally(
                phone=phone, email=email, reason=reason, created_by=actor
            )
            note = _local_fallback_note(local)
        record_opt_out_audit(
            action="opt-out",
            email=email,
            phone=phone,
            interview_id=request.interview_id,
            candidate_id=request.candidate_id,
            scope=request.scope,
            channels=",".join(channels) if channels else None,
            reason=reason,
            created_by=actor,
            pairbot_ok=False,
            pairbot_response={"status_code": e.status_code, "error": e.message, **e.payload},
            local_result=local,
        )
        raise _pairbot_http_error(
            e, "Could not stop outreach at pair-bot", note
        ) from e

    local = suppress_contact_locally(
        phone=phone, email=email, reason=reason, created_by=actor
    )
    record_opt_out_audit(
        action="opt-out",
        email=email,
        phone=phone,
        interview_id=request.interview_id,
        candidate_id=request.candidate_id,
        scope=request.scope,
        channels=",".join(channels) if channels else None,
        reason=reason,
        created_by=actor,
        pairbot_ok=True,
        pairbot_response=pairbot_response,
        local_result=local,
    )
    # Contact details masked (utils/pii.py); the audit row holds the full
    # values for anyone tracing a specific case.
    logger.info(
        "opt_out_ok by=%s candidate_id=%s email=%s phone=%s cancelled=%s interviews=%s",
        actor, request.candidate_id, mask_email(email), mask_phone(phone),
        (pairbot_response.get("data") or {}).get("cancelled"),
        (pairbot_response.get("data") or {}).get("interview_ids"),
    )

    # `message` is pair-bot's and is passed through untouched — it carries the
    # "across N interviews" count recruiters need, and the plain-English note
    # for when a scoped request had to be enforced across every tenant anyway
    # (shared Twilio numbers, shared EMAIL_FROM). Do not recompose it.
    return {
        "success": True,
        "message": pairbot_response.get("message") or "Outreach stopped.",
        "data": pairbot_response.get("data") or {},
        "local": local,
    }


# ---------------------------------------------------------------------------
# POST /api/v1/outreach/opt-in
# ---------------------------------------------------------------------------
@router.post("/opt-in")
async def resume_outreach(
    request: OptInRequest,
    user: UserIdentity = Depends(get_current_user),
) -> Dict[str, Any]:
    """Lift a suppression, for a candidate who asks to be contacted again.

    Takes email and/or phone — never interview_id, mirroring pair-bot:
    re-enabling contact is a decision about a person and should be made against
    the address or number explicitly, not inferred from whichever record
    happened to be on screen.

    Ordering is the reverse of opt-out: pair-bot first, and if it refuses we
    leave the local suppression standing. Failing closed here keeps the strict
    outcome (nobody gets contacted) rather than the lenient one.

    Does not re-queue the cancelled outreach on either side. Resuming a
    campaign someone had stopped is a deliberate act, not a side effect of
    clearing a flag.
    """
    contact = _merge_identifiers(request.candidate_id, request.email, request.phone)
    email, phone = contact["email"], contact["phone"]

    if not email and not phone:
        raise HTTPException(
            status_code=422,
            detail="Need an email or a phone number to resume outreach.",
        )

    reason = (request.reason or "").strip() or None
    actor = user.email or "unknown"
    upstream_reason = f"{reason} (via pair, {actor})" if reason else f"Resumed in pair by {actor}"

    try:
        pairbot_response = await pairbot_opt_in(
            email=email, phone=phone, reason=upstream_reason, scope=request.scope
        )
    except PairBotOptOutError as e:
        record_opt_out_audit(
            action="opt-in",
            email=email,
            phone=phone,
            interview_id=None,
            candidate_id=request.candidate_id,
            scope=request.scope,
            channels=None,
            reason=reason,
            created_by=actor,
            pairbot_ok=False,
            pairbot_response={"status_code": e.status_code, "error": e.message, **e.payload},
            local_result={"skipped": "pair-bot rejected the opt-in; suppression left in place"},
        )
        raise _pairbot_http_error(
            e,
            "Could not resume outreach at pair-bot",
            "The candidate is still suppressed everywhere. Please retry.",
        ) from e

    local = release_contact_locally(phone=phone, email=email, created_by=actor)
    record_opt_out_audit(
        action="opt-in",
        email=email,
        phone=phone,
        interview_id=None,
        candidate_id=request.candidate_id,
        scope=request.scope,
        channels=None,
        reason=reason,
        created_by=actor,
        pairbot_ok=True,
        pairbot_response=pairbot_response,
        local_result=local,
    )
    logger.info(
        "opt_in_ok by=%s email=%s phone=%s",
        actor, mask_email(email), mask_phone(phone),
    )

    message = pairbot_response.get("message") or "Suppression lifted."
    if local.get("dnc_phone_retained_other_source"):
        # The imported Zoom DNC list still names this number. pair-bot will
        # contact them again; pair will not. Say so rather than reporting a
        # clean success the recruiter would read as "we can call them now".
        message = (
            f"{message} Note: this number is on pair's imported Do-Not-Contact "
            "list, so pair will still not launch new outreach to it."
        )
    return {
        "success": True,
        "message": message,
        "data": pairbot_response.get("data") or {},
        "local": local,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/outreach/opt-out
# ---------------------------------------------------------------------------
@router.get("/opt-out")
async def outreach_opt_out_status(
    candidate_id: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    interview_id: Optional[int] = Query(default=None),
    scope: Optional[str] = Query(default=None),
    user: UserIdentity = Depends(get_current_user),
) -> Dict[str, Any]:
    """"Are we still contacting this person?" — the question asked right after
    a complaint arrives.

    Reports pair-bot's answer (from pair's tenant point of view, with every
    scope still listed under ``records``) alongside pair's own local gate, so a
    divergence between the two is visible instead of guessed at.

    Unreachable pair-bot is a 200 with ``pairbot.error`` set, not a 502: the
    local half of the answer is still worth showing, and this endpoint is read
    on a page load where a hard failure would just render an empty panel.
    """
    contact = _merge_identifiers(candidate_id, email, phone)
    r_email, r_phone = contact["email"], contact["phone"]

    if not r_email and not r_phone and interview_id is None:
        raise HTTPException(
            status_code=422,
            detail="Need an email, a phone number or an interview id.",
        )

    pairbot: Dict[str, Any]
    try:
        pairbot = await pairbot_opt_out_status(
            email=r_email, phone=r_phone, interview_id=interview_id, scope=scope
        )
    except PairBotOptOutError as e:
        pairbot = {"error": e.message, "status_code": e.status_code}

    return {
        "success": True,
        "email": r_email,
        "phone": r_phone,
        "pairbot": pairbot,
        "local": local_suppression_status(phone=r_phone, email=r_email),
    }
