"""pair-bot do-not-contact client.

Thin async wrapper over pair-bot's three opt-out endpoints. The contract is
documented in OPT_OUT_API.md, owned by the pair-bot team and not vendored into
this repo:

    POST /api/candidates/opt-out   suppress + cancel queued outreach
    POST /api/candidates/opt-in    lift a suppression
    GET  /api/candidates/opt-out   "are we still contacting this person?"

Auth is the same M2M key already used for /api/bulk-interviews — no new
credential.

Why this is not another `_proxy_post` in routers/engagement.py: those helpers
collapse every upstream failure into a flat 500. Here the distinction matters.
A 422 (no identifying field) or a 400 (the interview has neither an email nor a
phone) means *nothing was suppressed* and the recruiter must fix the input; a
502 means pair-bot is down and the stop has to be retried. Reporting both as
500 would leave a recruiter unable to tell "bad request" from "still calling".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Channels pair-bot accepts. Omitting the field entirely defaults to all three
# server-side, which is what "stop contacting me" means; we send the list only
# when a caller narrows it.
VALID_CHANNELS = ("email", "sms", "call")


class PairBotOptOutError(Exception):
    """Upstream refused or was unreachable.

    ``status_code`` is pair-bot's own status when it answered, else None (for a
    transport error). ``retryable`` is True when the stop did not happen for a
    reason the recruiter cannot fix by editing the request.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code >= 500


def _base_url() -> str:
    return os.getenv(
        "EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai"
    ).rstrip("/")


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = os.getenv("PAIR_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        # Not raised: QA hosts have run unauthenticated before. Log loudly so a
        # 401 from upstream is traceable to a missing env var, not a bad key.
        logger.warning("pairbot_opt_out_no_api_key: PAIR_API_KEY is unset")
    return headers


def normalize_channels(channels: Optional[List[str]]) -> Optional[List[str]]:
    """Lower-case + de-duplicate, preserving pair-bot's channel vocabulary.

    Returns None when the caller passed nothing, so the field is omitted and
    pair-bot's all-three default applies. Raises ValueError on an unknown name
    rather than silently dropping it — a dropped channel is a channel that
    keeps sending.
    """
    if not channels:
        return None
    seen: List[str] = []
    for raw in channels:
        name = (raw or "").strip().lower()
        if not name:
            continue
        if name not in VALID_CHANNELS:
            raise ValueError(
                f"Unknown channel {raw!r}; expected any of {', '.join(VALID_CHANNELS)}"
            )
        if name not in seen:
            seen.append(name)
    return seen or None


def _extract_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return fallback


async def _request(method: str, path: str, **kwargs) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            res = await client.request(method, url, headers=_headers(), **kwargs)
    except httpx.HTTPError as e:
        logger.error(f"pairbot_opt_out_transport_error {method} {path}: {e}")
        raise PairBotOptOutError(
            f"Could not reach pair-bot to stop outreach: {e}"
        ) from e

    try:
        body = res.json()
    except ValueError:
        body = {"raw": res.text[:500]}

    if res.status_code >= 400:
        logger.error(
            "pairbot_opt_out_error %s %s status=%s body=%s",
            method, path, res.status_code, str(body)[:500],
        )
        raise PairBotOptOutError(
            _extract_message(body, f"pair-bot returned {res.status_code}"),
            status_code=res.status_code,
            payload=body if isinstance(body, dict) else {},
        )

    return body if isinstance(body, dict) else {"data": body}


async def pairbot_opt_out(
    email: Optional[str] = None,
    phone: Optional[str] = None,
    interview_id: Optional[int] = None,
    reason: Optional[str] = None,
    channels: Optional[List[str]] = None,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Suppress a contact at pair-bot and cancel their queued outreach.

    Send BOTH email and phone whenever both are known: pair-bot stores them as
    two separate identities, and a suppression recorded against only the
    address will not match a Twilio STOP that later arrives carrying only the
    number.

    ``scope`` is left off by default, which pair-bot reads as "curate" — pair's
    own tenant. Note that today every channel is enforced across all tenants
    anyway (one shared Twilio number pool, one shared EMAIL_FROM); pair-bot
    reports that back in ``enforced_globally`` and folds it into ``message``,
    which is why callers must surface ``message`` verbatim instead of composing
    their own.
    """
    payload: Dict[str, Any] = {}
    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone
    if interview_id is not None:
        payload["interview_id"] = interview_id
    if reason:
        payload["reason"] = reason[:500]
    if channels:
        payload["channels"] = channels
    if scope:
        payload["scope"] = scope
    return await _request("POST", "/api/candidates/opt-out", json=payload)


async def pairbot_opt_in(
    email: Optional[str] = None,
    phone: Optional[str] = None,
    reason: Optional[str] = None,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Lift a suppression. Takes email and/or phone — never interview_id.

    Omitting ``scope`` clears every scope, including a global opt-out. That is
    pair-bot's documented asymmetry with opt-out (which defaults to one
    tenant): defaulting opt-in narrowly would leave a global opt-out standing
    and still report success.

    Does not re-queue the cancelled outreach.
    """
    payload: Dict[str, Any] = {}
    if email:
        payload["email"] = email
    if phone:
        payload["phone"] = phone
    if reason:
        payload["reason"] = reason[:500]
    if scope:
        payload["scope"] = scope
    return await _request("POST", "/api/candidates/opt-in", json=payload)


async def pairbot_opt_out_status(
    email: Optional[str] = None,
    phone: Optional[str] = None,
    interview_id: Optional[int] = None,
    scope: Optional[str] = None,
) -> Dict[str, Any]:
    """Answered from pair's point of view; ``records`` still lists every scope."""
    params: Dict[str, Any] = {}
    if email:
        params["email"] = email
    if phone:
        params["phone"] = phone
    if interview_id is not None:
        params["interview_id"] = interview_id
    if scope:
        params["scope"] = scope
    return await _request("GET", "/api/candidates/opt-out", params=params)
