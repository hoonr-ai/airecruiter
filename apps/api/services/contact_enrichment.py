"""Contact enrichment via Kipplo, ZoomInfo, Apollo and Exa.

Kipplo (services/kipplo.py, by LinkedIn URL) is the first provider in both
chains since 2026-09-28; the ZoomInfo -> Apollo -> Exa steps below run only for
what it did not find.

Two surfaces:

1. Provider helpers (`extract_zoominfo_contact_fields`, `extract_apollo_contact_fields`,
   `apollo_enrich_by_linkedin`) — moved here from `routers/candidates.py` so both
   the on-demand enrichment endpoint and the in-line sourcing path share the
   same parsers and HTTP shapes.

2. `enrich_contact_for_sourcing(linkedin_url, jobdiva_id, full_name)` —
   first-hit-wins helper for the sourcing pipeline. The new ZoomInfo Data
   API doesn't accept a `linkedinUrl` as a match input (tested empirically —
   `PFAPI0005 / Invalid field requested`), so the flow is:
       ContactSearch by firstName + lastName  →  personId
                                              →  ContactEnrich by personId
   On any miss (no name → can't search; search returns nothing; enrich
   returns no usable fields), fall through to Apollo by LinkedIn URL.

   The legacy `/enrich/contact` POST is dead: ZoomInfo retired it for our
   account and it 401s even with a fresh OAuth-minted token. That entire
   code path was deleted.

   Auth: `services.zoominfo_auth.get_access_token()` mints fresh 24h JWTs
   via OAuth2 client_credentials. No more static `ZOOMINFO_BEARER_TOKEN`.

   Capped at `PER_JOB_CAP` enrichments per job to bound provider cost.
   Failures are logged and swallowed — sourcing must not fail on enrichment.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import weakref
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from core.config import (
    APOLLO_API_KEY as _APOLLO_ENV_KEY,
    EXA_API_KEY,
    EXA_CONTACT_DEEP_DATA_SOURCES,
    EXA_CONTACT_DEEP_EFFORT,
    EXA_CONTACT_DEEP_TIMEOUT_S,
    EXA_CONTACT_ENRICH_CONCURRENCY,
    EXA_CONTACT_ENRICH_EFFORT,
    EXA_ONDEMAND_CONTACT_CONCURRENCY,
    EXA_CONTACT_ENRICH_ENABLED,
    EXA_CONTACT_ENRICH_RETRY,
    EXA_CONTACT_ENRICH_TIMEOUT_S,
    EXA_LATE_RESULT_WAIT_S,
)
from services import contact_cache, exa_telemetry
from services.exa_errors import credit_tag
from services.kipplo import enrich_by_linkedin as kipplo_enrich_by_linkedin
from services.kipplo import supported_fields as kipplo_supported_fields
from services.zoominfo_auth import (
    ZoomInfoAuthFailed,
    ZoomInfoAuthNotConfigured,
    get_access_token,
)

logger = logging.getLogger(__name__)

APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/enrich"

# ---- Exa Agent API (contact enrichment by LinkedIn URL) ----
EXA_AGENT_RUNS_URL = "https://api.exa.ai/agent/runs"
EXA_AGENT_BETA = "agent-2026-05-07"
_EXA_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_EXA_POLL_INTERVAL_S = 4  # per Exa docs
_EXA_LATE_POLL_S = 15      # background watch of a run we stopped waiting for
# Structured output we ask the agent to fill. Only the two billable contact
# fields (email $0.02 / phone $0.07 per run) — richer schemas just cost more.
# The `description` on each field matters: per Exa engineering, contact-field
# descriptions in the outputSchema are what activate the agent's contact
# enrichment tool — a bare {"type": "string"} is NOT enough.
_EXA_CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact": {
            "type": "object",
            "description": "Contact details for the person named in the query.",
            "properties": {
                "email": {
                    "type": "string",
                    "format": "email",
                    "description": (
                        "The person's best current email address "
                        "(work email preferred, personal email acceptable)."
                    ),
                },
                "phone": {
                    "type": "string",
                    "format": "phone",
                    "description": (
                        "The person's own personal mobile phone number, including "
                        "country code. Not an office, company, switchboard or "
                        "work direct-dial number: leave empty if only those exist."
                    ),
                },
            },
        }
    },
}
EXA_CONTACT_FIELDS = ("email", "phone")


_FIBER_FIELD_HINT = " From Fiber.ai's contact reveal when it has one."


def _exa_contact_schema(fields: Tuple[str, ...], via_fiber: bool = False) -> Dict[str, Any]:
    """``_EXA_CONTACT_SCHEMA`` narrowed to ``fields``. The agent bills per
    contact field it fills, so a lookup for a candidate who already has an
    email asks for the phone only (and vice versa). ``via_fiber`` names
    Fiber.ai in each field: an attached data partner is only a tool the agent
    MAY call, and it reaches for it when the schema asks for its data."""
    contact = _EXA_CONTACT_SCHEMA["properties"]["contact"]
    props = {f: dict(contact["properties"][f]) for f in fields}
    if via_fiber:
        for prop in props.values():
            prop["description"] = prop["description"] + _FIBER_FIELD_HINT
    return {
        "type": "object",
        "properties": {
            "contact": {
                **{k: v for k, v in contact.items() if k != "properties"},
                "properties": props,
            }
        },
    }


def deep_data_sources() -> Tuple[str, ...]:
    """Exa Connect providers attached to the deep search (EXA_CONTACT_DEEP_DATA_SOURCES)."""
    raw = str(EXA_CONTACT_DEEP_DATA_SOURCES or "")
    return tuple(dict.fromkeys(p.strip().lower() for p in raw.split(",") if p.strip()))


# Free/consumer mailbox domains → classify the agent's email as personal.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "proton.me", "protonmail.com", "live.com", "me.com", "msn.com",
}

# Legacy in-repo Apollo key — kept so deployments without APOLLO_API_KEY in env
# don't lose enrichment. New deployments should set the env var; the WARN
# below at first import flags the legacy path.
_APOLLO_LEGACY_KEY = "cB7rogHZj4XRrhnTEqTlXQ"
APOLLO_API_KEY = (_APOLLO_ENV_KEY or _APOLLO_LEGACY_KEY).strip()
APOLLO_KEY_SOURCE = "env" if (_APOLLO_ENV_KEY or "").strip() else "legacy_fallback"
if APOLLO_KEY_SOURCE == "legacy_fallback":
    logger.warning(
        "Apollo enrichment using legacy in-repo key; set APOLLO_API_KEY env var to rotate"
    )

# Sourcing-time concurrency + cost guards. Module-level state because the
# producers in unified_candidate_search.py run as independent asyncio tasks
# and need a single shared rate-limit budget.
_PROVIDER_SEMAPHORE = asyncio.Semaphore(8)
_JOB_ENRICH_COUNTERS: Dict[str, int] = {}
_JOB_ENRICH_LOCK = asyncio.Lock()
PER_JOB_CAP = 50

# Separate, tighter budget for the Exa Agent fallback (paid ~$0.115/run and
# slow — it polls). Counted per job, independent of the ZoomInfo/Apollo cap
# so a burst of Exa runs can't starve the cheap providers or vice versa.
# Like PER_JOB_CAP this is per-uvicorn-worker in-process state — a search
# request streams entirely on one worker, so one search sees one budget;
# re-runs landing on other workers get a fresh one (bounded worst case:
# workers × cap).
_JOB_EXA_COUNTERS: Dict[str, int] = {}

# Cumulative Exa runs per job. Deliberately NOT cleared by reset_job_counter:
# _JOB_EXA_COUNTERS is a per-RUN budget (reset each search so one filled counter
# doesn't starve enrichment forever), which means it bounds nothing across
# re-runs. This one is the spend ceiling.
_JOB_EXA_LIFETIME: Dict[str, int] = {}

# Bound on in-flight sourcing-time Exa contact runs. The fallback runs outside
# _PROVIDER_SEMAPHORE (its slow polling would starve the cheap chain), so it
# gets its own bound: EXA_CONTACT_ENRICH_CONCURRENCY. It used to borrow the
# deep-search agent's EXA_AGENT_CONCURRENCY (1), which queued a job's contact
# lookups one at a time behind each other.
def _exa_semaphore() -> asyncio.Semaphore:
    global _EXA_SEMAPHORE
    if _EXA_SEMAPHORE is None:
        _EXA_SEMAPHORE = asyncio.Semaphore(max(1, int(EXA_CONTACT_ENRICH_CONCURRENCY or 1)))
    return _EXA_SEMAPHORE

_EXA_SEMAPHORE: Optional[asyncio.Semaphore] = None

# Per event loop, so the bound never outlives the loop it was made on.
_ONDEMAND_EXA_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def ondemand_exa_semaphore() -> asyncio.Semaphore:
    """Bound on in-flight on-demand Exa contact runs in this worker
    (EXA_ONDEMAND_CONTACT_CONCURRENCY)."""
    loop = asyncio.get_running_loop()
    sem = _ONDEMAND_EXA_SEMAPHORES.get(loop)
    if sem is None:
        sem = _ONDEMAND_EXA_SEMAPHORES[loop] = asyncio.Semaphore(EXA_ONDEMAND_CONTACT_CONCURRENCY)
    return sem


def lookup_provider_enabled(name: str) -> bool:
    """Whether the contact lookups ask provider ``name`` (kipplo, zoominfo,
    apollo, exa): sourcing_config.CONTACT_LOOKUP_PROVIDERS, default apollo + exa."""
    from core import sourcing_config as _sc

    listed = getattr(_sc, "CONTACT_LOOKUP_PROVIDERS", ("apollo", "exa")) or ()
    return name.strip().lower() in {str(p).strip().lower() for p in listed}


def exa_deep_enabled() -> bool:
    """Whether a field the normal Exa run did not find gets a deep search
    (EXA_CONTACT_DEEP_EFFORT, "off" disables)."""
    return str(EXA_CONTACT_DEEP_EFFORT or "").strip().lower() not in {"", "off", "none", "false", "0"}


# What one Exa Agent contact run costs (exa.ai/pricing, 2026-10-14): a flat fee
# per effort, plus a fee per field it finds. Fiber.ai credits on a deep run
# average ~$0.06 (run list 9/29-10/13). Only for the "Search again (~$X)" hint
# of a final miss: telemetry reports what a run actually cost.
_EXA_EFFORT_USD = {"minimal": 0.012, "low": 0.025, "medium": 0.10, "high": 0.50}
_EXA_FOUND_USD = {"email": 0.02, "phone": 0.07}
_EXA_FIBER_RUN_USD = 0.06


def rebuy_estimate_usd(fields: Tuple[str, ...], tier: str) -> float:
    """About what one more Exa run (``tier`` "deep" or "normal") for
    ``fields`` costs if it finds them: deep ~$0.18-0.25, normal ~$0.05-0.12."""
    deep = tier == "deep"
    effort = str((EXA_CONTACT_DEEP_EFFORT if deep else EXA_CONTACT_ENRICH_EFFORT) or "").strip().lower()
    usd = _EXA_EFFORT_USD.get(effort, _EXA_EFFORT_USD["medium" if deep else "low"])
    usd += sum(_EXA_FOUND_USD.get(f, 0.0) for f in set(fields or ()))
    if deep and "fiber" in deep_data_sources():
        usd += _EXA_FIBER_RUN_USD
    return round(usd, 2)


# Launch PAIR's deep email run for a candidate already holding a personal
# phone: a holdout (sourcing_config.EXA_DEEP_SKIP_REACHABLE_PCT). The bucket is
# a hash of the LinkedIn slug, never Python's hash() (salted per process): a
# person stays in one arm across the 8 workers and every restart.
_DEEP_SKIP_SALT = b"deep_skip_reachable:"


def deep_skip_reachable_pct() -> int:
    """EXA_DEEP_SKIP_REACHABLE_PCT (sourcing_config, default 50), 0-100, read
    at call time: the share of Launch PAIR's reachable people who skip it."""
    from core import sourcing_config as _sc

    try:
        pct = int(getattr(_sc, "EXA_DEEP_SKIP_REACHABLE_PCT", 50))
    except (TypeError, ValueError):
        return 50
    return min(100, max(0, pct))


def deep_skip_bucket(linkedin_url: Any) -> Optional[int]:
    """The person's holdout bucket, 0-99 (None without a LinkedIn slug)."""
    slug = contact_cache.linkedin_slug(linkedin_url)
    if not slug:
        return None
    return int(hashlib.sha1(_DEEP_SKIP_SALT + slug.encode("utf-8")).hexdigest(), 16) % 100


def deep_skip_arm(linkedin_url: Any, pct: int) -> str:
    """The person's arm at ``pct``: "skip" (no deep email run while a
    personal phone is held), "control" (the deep email run as before) or
    "off" (pct 0, or no LinkedIn slug)."""
    bucket = deep_skip_bucket(linkedin_url)
    if pct <= 0 or bucket is None:
        return "off"
    return "skip" if bucket < pct else "control"


_LINKEDIN_PROFILE_RE = re.compile(r"linkedin\.com/in/", re.IGNORECASE)


def _normalise_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    plus = "+" if raw.startswith("+") else ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{plus}{digits}" if digits else ""


def extract_zoominfo_contact_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    """Parse ZoomInfo new Data API contact-enrich response into canonical fields."""
    data = payload.get("data") or []
    first = data[0] if isinstance(data, list) and data else {}
    attrs = first.get("attributes") if isinstance(first, dict) else {}
    if not isinstance(attrs, dict):
        attrs = {}

    email_alt = attrs.get("emailAlt")
    alt_email = ""
    if isinstance(email_alt, list):
        for item in email_alt:
            if isinstance(item, dict):
                candidate = str(item.get("value") or "").strip()
                if candidate:
                    alt_email = candidate
                    break

    return {
        "mobilePhone": str(attrs.get("mobilePhone") or attrs.get("mobilePhoneAlt") or "").strip(),
        "workPhone": str(attrs.get("phone") or attrs.get("directPhone") or attrs.get("directPhoneAlt") or "").strip(),
        "workEmail": str(attrs.get("email") or "").strip(),
        "personalEmail": alt_email,
    }


def _extract_enrichment_fields_legacy(payload: Any) -> Dict[str, str]:
    """Walk an arbitrarily-shaped ZoomInfo legacy enrich response and pull out
    the four canonical contact fields. Handles three shapes seen in production:
    A) {"fieldName":"mobilePhone","value":"+1..."}, B) flat dict, C) nested."""
    targets = {"workPhone", "mobilePhone", "workEmail", "personalEmail"}
    targets_lower = {t.lower(): t for t in targets}
    found: Dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            field_name = node.get("fieldName")
            field_value = node.get("value")
            if isinstance(field_name, str) and isinstance(field_value, str) and field_value.strip():
                canonical = targets_lower.get(field_name.strip().lower())
                if canonical and canonical not in found:
                    found[canonical] = field_value.strip()
            for k, v in node.items():
                canonical = targets_lower.get(str(k).strip().lower())
                if canonical and isinstance(v, str) and v.strip() and canonical not in found:
                    found[canonical] = v.strip()
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


# Apollo returns a masked placeholder rather than omitting the field when a
# record exists but the contact is not unlocked on the current plan/credits —
# canonically `email_not_unlocked@domain.com`. It is a syntactically valid
# address, so nothing downstream would reject it.
_APOLLO_MASKED_EMAIL_MARKERS = ("not_unlocked", "notunlocked", "email_not_unlocked")

# Phone types that mean the candidate's own number. Only these may become the
# candidate's phone; work / office / direct / switchboard / untyped never do.
_PERSONAL_PHONE_TYPES = {"mobile", "cell", "cellphone", "cell_phone", "mobile_phone", "home", "personal"}


def _apollo_real_email(value: Any) -> str:
    """Drop Apollo's masked-email placeholders, keep genuine addresses.

    Without this, a plan that can MATCH but not REVEAL yields
    `email_not_unlocked@domain.com`, and because it parses as a real address it
    would be stored as the candidate's email, shown in the UI, counted as
    "reachable" by the sourcing gate (suppressing the Exa fallback the candidate
    actually needs), and only fail at Launch PAIR. Worth guarding before Apollo
    credits are topped up: an out-of-credits key 422s and never reaches here, so
    restoring credits is exactly what would start surfacing these.
    """
    email = str(value or "").strip()
    if not email or "@" not in email:
        return ""
    lowered = email.lower()
    if any(marker in lowered for marker in _APOLLO_MASKED_EMAIL_MARKERS):
        return ""
    return email


def extract_apollo_contact_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse Apollo people-enrich response into canonical fields + a phone-candidate list."""
    person = payload.get("person") if isinstance(payload, dict) else {}
    if not isinstance(person, dict):
        person = {}

    def _first_non_empty(*values: Any) -> str:
        for value in values:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return ""

    def _extract_phone_value(item: Any) -> str:
        if isinstance(item, str):
            return str(item).strip()
        if isinstance(item, dict):
            return _first_non_empty(
                item.get("sanitized_number"),
                item.get("raw_number"),
                item.get("number"),
                item.get("value"),
            )
        return ""

    phone_candidates: List[str] = []
    seen_phone_candidates: set = set()

    def _add_phone_candidate(raw_phone: Any) -> None:
        candidate = _normalise_phone(str(raw_phone or "").strip())
        if not candidate:
            return
        if sum(1 for ch in candidate if ch.isdigit()) < 7:
            return
        if candidate in seen_phone_candidates:
            return
        seen_phone_candidates.add(candidate)
        phone_candidates.append(candidate)

    work_email = _apollo_real_email(
        _first_non_empty(person.get("email"), person.get("work_email"))
    )

    personal_email = ""
    personal_emails = person.get("personal_emails")
    if isinstance(personal_emails, list):
        for item in personal_emails:
            candidate = _apollo_real_email(
                _first_non_empty(
                    item.get("email") if isinstance(item, dict) else None,
                    item,
                )
            )
            if candidate:
                personal_email = candidate
                break

    # mobilePhone is the PERSONAL number slot (user 2026-09-28: "we only need
    # personal phone number"): only numbers Apollo types as mobile / cell /
    # home. Direct dials, office lines and untyped numbers are work numbers and
    # go to workPhone, which is never used as the candidate's phone.
    mobile_phone = _first_non_empty(
        person.get("mobile_phone"),
        person.get("mobile"),
        person.get("cell_phone"),
        person.get("cell"),
        person.get("home_phone"),
    )
    work_phone = _first_non_empty(
        person.get("sanitized_phone"),
        person.get("work_phone"),
        person.get("organization_phone"),
        person.get("direct_phone"),
        person.get("office_phone"),
        person.get("phone"),
        person.get("phone_number"),
    )

    _add_phone_candidate(mobile_phone)
    _add_phone_candidate(work_phone)

    phone_numbers = person.get("phone_numbers")
    if isinstance(phone_numbers, list):
        for item in phone_numbers:
            number = _extract_phone_value(item)
            if not number:
                continue
            ptype = str(item.get("type") or "").strip().lower() if isinstance(item, dict) else ""
            if not mobile_phone and ptype in _PERSONAL_PHONE_TYPES:
                mobile_phone = number
            elif not work_phone and ptype in {"work", "office", "direct"}:
                work_phone = number
            elif not work_phone:
                work_phone = number
            _add_phone_candidate(number)

    # Employer switchboard numbers. Tracked separately: they are the COMPANY's
    # line, not the candidate's, so they must never be promoted into
    # mobilePhone/workPhone. Apollo returns an org phone on almost every matched
    # record while personal phones need an explicit (credit-consuming) reveal, so
    # the old blanket promotion below turned "we found the employer's front desk"
    # into "this is the candidate's mobile" — measured on 5/5 probed profiles,
    # including well-known ones with no personal phone in the payload at all.
    # That pollutes outreach (texting a switchboard) and, since the sourcing gate
    # treats any phone as reachable, it also suppressed the Exa fallback that
    # could have found a real mobile. Kept in phoneCandidates as context.
    org_phone_keys: set = set()

    def _add_org_phone(raw_phone: Any) -> None:
        normalised = _normalise_phone(str(raw_phone or "").strip())
        if not normalised or sum(1 for ch in normalised if ch.isdigit()) < 7:
            return
        org_phone_keys.add(normalised)
        _add_phone_candidate(raw_phone)

    person_organization = person.get("organization")
    if isinstance(person_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_org_phone(person_organization.get(key))

    payload_organization = payload.get("organization") if isinstance(payload, dict) else None
    if isinstance(payload_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_org_phone(payload_organization.get(key))

    if not mobile_phone:
        mobile_phone = _extract_phone_value(payload.get("mobile_phone"))
    if not work_phone:
        work_phone = _extract_phone_value(payload.get("phone"))

    _add_phone_candidate(payload.get("mobile_phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone_number") if isinstance(payload, dict) else "")

    # An untyped person-level number is not known to be personal, so it can
    # fill workPhone but never mobilePhone (this used to promote the first one
    # into mobilePhone, which put direct work lines on candidates as mobiles).
    person_numbers = [p for p in phone_candidates if p not in org_phone_keys and p != _normalise_phone(mobile_phone)]
    if not work_phone and person_numbers:
        work_phone = person_numbers[0]

    return {
        "mobilePhone": mobile_phone,
        "workPhone": work_phone,
        "workEmail": work_email,
        "personalEmail": personal_email,
        "phoneCandidates": phone_candidates,
    }


# Apollo answers 422 "insufficient credits" on EVERY call once the plan runs
# dry. Each miss was logged at WARNING per candidate and looked like an ordinary
# no-match, so an empty account read as "Exa rows get no contact" (Apollo is the
# only provider that matches a LinkedIn URL). After one such reply we log it
# loudly and skip Apollo for a cool-down instead of paying the round-trip per
# row; the chain goes straight to its next step.
#
# The cool-down is shared across workers through Redis (`apollo:no_credits`,
# with a TTL) so one worker's 422 stops all of them, and the ERROR is logged
# once per window instead of once per worker every few minutes. The local
# mirror keeps working when Redis is unavailable.
APOLLO_NO_CREDITS_COOLDOWN_S = 3600.0
APOLLO_NO_CREDITS_REDIS_KEY = "apollo:no_credits"
_apollo_no_credits_until = 0.0


def _is_apollo_no_credits(status_code: int, body: str) -> bool:
    text = (body or "").lower()
    return status_code in (402, 422, 429) and "credit" in text and (
        "insufficient" in text or "no more" in text or "out of" in text
    )


def apollo_out_of_credits() -> bool:
    """True while Apollo is in its out-of-credits cool-down."""
    return time.monotonic() < _apollo_no_credits_until


async def _sync_shared_no_credits() -> None:
    """Adopt a cool-down another worker opened (mirrored locally until it expires)."""
    global _apollo_no_credits_until
    if apollo_out_of_credits():
        return
    from services import jobdiva_rate_limit as _rl  # shared Redis client with retry/back-off

    client = _rl._get_redis()
    if client is None:
        return
    try:
        ttl_ms = await client.pttl(APOLLO_NO_CREDITS_REDIS_KEY)
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return
    if ttl_ms and ttl_ms > 0:
        _apollo_no_credits_until = time.monotonic() + ttl_ms / 1000.0


async def _trip_no_credits(status_code: int) -> None:
    global _apollo_no_credits_until
    _apollo_no_credits_until = time.monotonic() + APOLLO_NO_CREDITS_COOLDOWN_S
    logger.error(
        "Apollo is OUT OF CREDITS (%s, key source=%s): LinkedIn-URL contact "
        "lookups fall through to paid Exa until it is topped up or "
        "APOLLO_API_KEY is rotated; skipping Apollo for %ds on all workers",
        status_code, APOLLO_KEY_SOURCE, int(APOLLO_NO_CREDITS_COOLDOWN_S),
    )
    from services import jobdiva_rate_limit as _rl

    client = _rl._get_redis()
    if client is None:
        return
    try:
        await client.set(
            APOLLO_NO_CREDITS_REDIS_KEY, str(status_code),
            px=int(APOLLO_NO_CREDITS_COOLDOWN_S * 1000),
        )
    except Exception as exc:
        _rl._mark_redis_down(exc)


async def apollo_enrich_by_linkedin(candidate_id: str, linkedin_url: str) -> Dict[str, Any]:
    """Call Apollo's people/enrich by LinkedIn URL. Pure async, no DB writes.

    Returns {"ok": bool, "fields"|"message": ...}.
    """
    if not APOLLO_API_KEY or APOLLO_API_KEY == "PASTE_APOLLO_API_KEY_HERE":
        logger.warning("Apollo enrichment skipped for %s: API key not configured", candidate_id)
        return {"ok": False, "message": "Apollo API key not configured"}
    await _sync_shared_no_credits()
    if apollo_out_of_credits():
        return {"ok": False, "message": "Apollo out of credits"}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": APOLLO_API_KEY,
    }
    payload = {"linkedin_url": linkedin_url}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            ares = await client.post(APOLLO_ENRICH_URL, headers=headers, json=payload)
    except Exception as e:
        logger.warning("Apollo request failed for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"Apollo request failed: {str(e)}"}

    if _is_apollo_no_credits(ares.status_code, ares.text):
        await _trip_no_credits(ares.status_code)
        return {"ok": False, "message": "Apollo out of credits"}

    if ares.status_code >= 400:
        logger.warning(
            "Apollo non-2xx for %s: %s %s",
            candidate_id,
            ares.status_code,
            ares.text[:300],
        )
        return {"ok": False, "message": f"Apollo API error ({ares.status_code})"}

    try:
        apollo_data = ares.json()
    except Exception:
        apollo_data = {"raw": ares.text}

    extracted = extract_apollo_contact_fields(apollo_data)
    if not any(extracted.get(k) for k in ("mobilePhone", "workPhone", "workEmail", "personalEmail")):
        logger.info("Apollo returned no usable contact fields for %s", candidate_id)
    person = apollo_data.get("person") if isinstance(apollo_data, dict) else None
    # Apollo's id for the matched person: lets the phone reveal
    # (services/apollo_phone.py) target exactly this record.
    person_id = str(person.get("id") or "") if isinstance(person, dict) else ""
    return {"ok": True, "fields": extracted, "person_id": person_id}


def extract_exa_contact_fields(structured: Any) -> Dict[str, Any]:
    """Parse the Exa Agent structured output into canonical fields + candidates.

    The agent returns at most one email and one phone (see ``_EXA_CONTACT_SCHEMA``).
    Classify the email as work vs personal by domain; treat the phone as a mobile
    candidate. Shape matches ``extract_apollo_contact_fields`` so the merge is uniform.
    """
    contact: Any = {}
    if isinstance(structured, dict):
        inner = structured.get("contact")
        contact = inner if isinstance(inner, dict) else structured

    email = ""
    phone_raw = ""
    if isinstance(contact, dict):
        email = str(contact.get("email") or "").strip()
        phone_raw = str(contact.get("phone") or "").strip()

    phone = _normalise_phone(phone_raw)
    if sum(1 for ch in phone if ch.isdigit()) < 7:
        phone = ""

    work_email = ""
    personal_email = ""
    if email and "@" in email:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in _PERSONAL_EMAIL_DOMAINS:
            personal_email = email
        else:
            work_email = email
    elif email:
        work_email = email

    return {
        "mobilePhone": phone,
        "workPhone": "",
        "workEmail": work_email,
        "personalEmail": personal_email,
        "phoneCandidates": [phone] if phone else [],
    }


def sanitize_agent_contact(email: Any, phone: Any) -> Tuple[str, str]:
    """Sanity-gate contact values returned by an Exa Agent run before they
    touch candidate rows: email must at least look like an email; phone is
    normalised (digits + optional leading '+') and must carry >= 7 digits.
    Failing values come back as "" so callers can treat them as absent.
    """
    e = str(email or "").strip()
    if "@" not in e:
        e = ""
    p = _normalise_phone(str(phone or ""))
    if sum(1 for ch in p if ch.isdigit()) < 7:
        p = ""
    return e, p


def _build_exa_contact_query(
    full_name: str,
    company: str,
    linkedin_url: str,
    fields: Tuple[str, ...] = EXA_CONTACT_FIELDS,
    deep: bool = False,
    via_fiber: bool = False,
) -> str:
    """Natural-language query for the Exa Agent contact-enrichment run. The
    deep search asks for a thorough search of the sources a quick run skips,
    and with Fiber.ai attached, to look the person up there (the wording of the
    2026-09-29 live check)."""
    who = (full_name or "").strip() or "this person"
    wanted = " and ".join(
        label for field, label in (("email", "work email"), ("phone", "personal mobile phone number"))
        if field in fields
    )
    parts = [f"Find the {wanted} for {who}"]
    company = (company or "").strip()
    if company:
        parts.append(f"at {company}")
    url = (linkedin_url or "").strip()
    if url:
        parts.append(f". LinkedIn: {url}")
    query = " ".join(parts).replace(" .", ".")
    if deep:
        query += (
            ". A quick lookup found nothing, so search thoroughly: the person's own website, "
            "GitHub, portfolio, resume or CV pages, conference and speaker pages, publications "
            "and professional directories. Only return details that belong to this person."
        )
        if via_fiber:
            query += (
                " Look this person up in the Fiber.ai people database by their LinkedIn URL "
                "and reveal their contact details there first."
            )
    return query


async def exa_enrich_by_linkedin(
    candidate_id: str,
    linkedin_url: str,
    full_name: str = "",
    company: str = "",
    fields: Tuple[str, ...] = EXA_CONTACT_FIELDS,
    deep: bool = False,
    wait_retry: bool = True,
) -> Dict[str, Any]:
    """Enrich one person via the Exa Agent API (by LinkedIn URL). Pure async.

    ``fields`` limits the lookup to the contact the caller still lacks (a
    subset of ``EXA_CONTACT_FIELDS``) — the agent bills per field it fills.
    ``deep`` = the deep search: EXA_CONTACT_DEEP_EFFORT, a thorough-search
    query and EXA_CONTACT_DEEP_TIMEOUT_S, for what a normal run did not find,
    with the Exa Connect partners of EXA_CONTACT_DEEP_DATA_SOURCES attached
    (Fiber.ai by default). Should Exa refuse them (a 400), the deep search runs
    once more on web search alone rather than not at all.
    A failure a second run would hit too (disabled, no key, a 4xx such as a
    bad key or no credits, the out-of-credits breaker open) comes back with
    ``fatal: True``; a run still going when we stop waiting comes back with
    its ``run_id`` (see watch_late_exa_run). ``wait_retry=False``: a run that
    runs out of time is not watched for a second window (the caller has a
    next step).

    Each create carries run ``metadata`` (services/exa_telemetry.py, kill
    switch EXA_RUN_METADATA_ENABLED). Guard, before the Fiber fallback and
    whatever the error text says: a 400 on a body with metadata is re-sent
    once, identical, without it; if that is accepted, metadata is switched
    off for the process (ERROR once). A metadata rejection therefore never
    silently costs the deep search its Fiber data source.

    Returns ``{"ok": bool, "fields"|"message": ...}`` mirroring
    ``apollo_enrich_by_linkedin``. No-op (``ok=False``) when
    ``EXA_CONTACT_ENRICH_ENABLED`` is off, ``EXA_API_KEY`` is missing or no
    field is requested. Each attempt is bounded by ``EXA_CONTACT_ENRICH_TIMEOUT_S``
    and a failed attempt gets one retry (``EXA_CONTACT_ENRICH_RETRY``, see
    ``_exa_contact_attempt``); all failures logged and swallowed.
    """
    if not EXA_CONTACT_ENRICH_ENABLED:
        return {"ok": False, "message": "Exa contact enrichment disabled", "fatal": True}
    wanted = tuple(f for f in EXA_CONTACT_FIELDS if f in (fields or ()))
    if not wanted:
        return {"ok": False, "message": "no contact fields requested"}
    if not EXA_API_KEY:
        logger.warning("Exa enrichment skipped for %s: EXA_API_KEY not configured", candidate_id)
        return {"ok": False, "message": "EXA_API_KEY not configured", "fatal": True}

    tier = "deep" if deep else "normal"
    with exa_telemetry.scope(tier=tier, fields_asked=",".join(wanted)):
        return await _exa_lookup(candidate_id, linkedin_url, full_name, company, wanted, deep, wait_retry)


async def _exa_lookup(
    candidate_id: str,
    linkedin_url: str,
    full_name: str,
    company: str,
    wanted: Tuple[str, ...],
    deep: bool,
    wait_retry: bool,
) -> Dict[str, Any]:
    """exa_enrich_by_linkedin's body, inside its telemetry scope."""
    from core.vendor_limiter import EXA as _exa_limit

    if await _exa_limit.no_credits_left() > 0:
        # Nothing is sent: fatal, so no deep search follows, and never a
        # cacheable miss (only an answered lookup records one). ``skipped``:
        # a caller that took a budget slot for this lookup gives it back.
        exa_telemetry.record_exa_call("contact", "agent_run", "breaker_open")
        return {"ok": False, "message": "Exa out of credits", "fatal": True, "skipped": True}

    timeout_s = EXA_CONTACT_DEEP_TIMEOUT_S if deep else EXA_CONTACT_ENRICH_TIMEOUT_S
    headers = {
        "Content-Type": "application/json",
        "x-api-key": EXA_API_KEY,
        "Exa-Beta": EXA_AGENT_BETA,
    }
    sources = deep_data_sources() if deep else ()
    use_metadata = exa_telemetry.run_metadata_enabled()

    def _body(with_sources: bool) -> Dict[str, Any]:
        via_fiber = with_sources and "fiber" in sources
        body: Dict[str, Any] = {
            "query": _build_exa_contact_query(
                full_name, company, linkedin_url, wanted, deep=deep, via_fiber=via_fiber
            ),
            "outputSchema": _exa_contact_schema(wanted, via_fiber=via_fiber),
            "effort": EXA_CONTACT_DEEP_EFFORT if deep else EXA_CONTACT_ENRICH_EFFORT,
        }
        if with_sources and sources:
            body["dataSources"] = [{"provider": p} for p in sources]
        if use_metadata and exa_telemetry.run_metadata_enabled():
            body["metadata"] = exa_telemetry.agent_metadata("contact")
        return body

    body = _body(with_sources=True)

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            outcome = await _exa_contact_attempt(client, headers, body, candidate_id, timeout_s=timeout_s)
            if outcome.get("status") == 400 and "metadata" in body:
                # Metadata guard (see exa_enrich_by_linkedin): the same body
                # once more without metadata. A 400 refusal is not billed.
                first_detail = str(outcome.get("detail") or "")
                use_metadata = False
                body = {k: v for k, v in body.items() if k != "metadata"}
                outcome = await _exa_contact_attempt(client, headers, body, candidate_id, timeout_s=timeout_s)
                if _run_was_created(outcome):
                    exa_telemetry.latch_metadata_off(first_detail)
                    exa_telemetry.record_exa_call("contact", "agent_guard", "metadata_rejected", http_status=400)
            if sources and outcome.get("status") == 400:
                logger.warning(
                    "Exa refused the deep search's data sources (%s) for %s: %s; "
                    "deep search on web search alone",
                    ",".join(sources), candidate_id, outcome.get("detail") or "",
                )
                exa_telemetry.record_exa_call("contact", "agent_guard", "sources_refused", http_status=400)
                body = _body(with_sources=False)
                outcome = await _exa_contact_attempt(client, headers, body, candidate_id, timeout_s=timeout_s)
            if outcome.get("status") == 400:
                # Refused with no metadata and no data sources left to drop.
                exa_telemetry.record_exa_call("contact", "agent_guard", "rejected", http_status=400)
            if (
                "result" not in outcome
                and outcome.get("retry")
                and EXA_CONTACT_ENRICH_RETRY
                and (wait_retry or outcome["retry"] != "wait")
            ):
                # Exa is the last provider: one second chance before the
                # candidate is left without contact (user 2026-09-28, "get the
                # contact details at all costs"). A run that timed out or could
                # not be polled is still going and billed either way, so it is
                # watched again instead of paying for a second run.
                same_run = outcome.get("run_id") if outcome["retry"] == "wait" else ""
                logger.info(
                    "Exa contact lookup for %s: %s; %s", candidate_id, outcome.get("message"),
                    "waiting on the same run once more" if same_run else "retrying with one new run",
                )
                if not same_run:
                    body = exa_telemetry.with_fresh_call_id(body)  # one call_id per billed run
                outcome = await _exa_contact_attempt(
                    client, headers, body, candidate_id, run_id=same_run or "", timeout_s=timeout_s
                )
    except Exception as e:
        logger.warning("Exa agent request failed for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"Exa request failed: {e}"}
    if "result" in outcome:
        return outcome["result"]
    if outcome.get("lease_lost"):
        # exa_step's lease went to another lookup before this create was
        # sent: nothing bought, exa_step follows that lookup instead.
        return {"ok": False, "message": outcome.get("message") or "", "lease_lost": True}
    failed = {"ok": False, "message": outcome.get("message") or "Exa lookup failed"}
    if outcome.get("retry") == "":
        failed["fatal"] = True
    if outcome.get("retry") == "wait" and outcome.get("run_id"):
        failed["run_id"] = outcome["run_id"]
    return failed


def _run_was_created(outcome: Dict[str, Any]) -> bool:
    """Whether an ``_exa_contact_attempt`` outcome means Exa accepted the
    create (a run exists: finished, still going, or failed/cancelled)."""
    return (
        "result" in outcome
        or bool(outcome.get("run_id"))
        or str(outcome.get("message") or "").startswith("Exa run ")
    )


# A lost create is looked for among the newest runs on the key (shared with
# QA, dev and scripts, so a page of 100) created in the last 10 minutes: an
# older run with the same query is an earlier lookup, not this one.
_FIND_STARTED_RUN_LIMIT = 100
_FIND_STARTED_RUN_MAX_AGE_S = 600


def _created_recently(run: Dict[str, Any], now: datetime) -> bool:
    """Whether ``run`` was created within _FIND_STARTED_RUN_MAX_AGE_S. A run
    without a readable createdAt is given the benefit of the doubt."""
    raw = str(run.get("createdAt") or "").strip()
    if not raw:
        return True
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created >= now - timedelta(seconds=_FIND_STARTED_RUN_MAX_AGE_S)


async def _find_started_run(client: Any, headers: Dict[str, str], query: str) -> Optional[Dict[str, Any]]:
    """The newest recent run with exactly this query (a create whose answer was
    lost), or None. The query names the person and their LinkedIn URL, so a
    match is a lookup of the same person."""
    if not query:
        return None
    for attempt in range(2):
        try:
            g = await client.get(EXA_AGENT_RUNS_URL, headers=headers, params={"limit": _FIND_STARTED_RUN_LIMIT})
            if g.status_code < 400:
                now = datetime.now(timezone.utc)
                for run in (g.json() or {}).get("data") or []:
                    if (
                        ((run.get("request") or {}).get("query") or "") == query
                        and run.get("status") not in {"failed", "cancelled"}
                        and _created_recently(run, now)
                    ):
                        return run
        except Exception as e:
            logger.info("Exa run list failed while looking for a started run: %s", e)
        if attempt == 0:
            await asyncio.sleep(2)
    return None


# Runs we stopped waiting for are billed anyway: watched in the background so
# what they find is kept (contact cache + the candidate's sourced rows missing
# that field) for the next Launch or phone click. Strong references: a task
# only weakly referenced by the loop could vanish mid-run.
_LATE_WATCHES: set = set()

LATE_FILL_SQL = """
    UPDATE sourced_candidates
    SET phone = CASE WHEN COALESCE(phone, '') = '' AND %(phone)s::text IS NOT NULL THEN %(phone)s ELSE phone END,
        email = CASE WHEN COALESCE(email, '') = '' AND %(email)s::text IS NOT NULL THEN %(email)s ELSE email END,
        updated_at = CURRENT_TIMESTAMP
    WHERE candidate_id = %(candidate_id)s
      AND (%(jobdiva_id)s::text IS NULL OR jobdiva_id = %(jobdiva_id)s)
      AND ((COALESCE(phone, '') = '' AND %(phone)s::text IS NOT NULL)
           OR (COALESCE(email, '') = '' AND %(email)s::text IS NOT NULL))
"""


def _fill_sourced_rows_sync(params: Dict[str, Any]) -> None:
    from core import db as _db

    conn = _db.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(LATE_FILL_SQL, params)
        conn.commit()
    finally:
        conn.close()


async def keep_late_exa_result(
    run_id: str,
    linkedin_url: str,
    *,
    candidate_id: str = "",
    jobdiva_id: Optional[str] = None,
    fields: Tuple[str, ...] = EXA_CONTACT_FIELDS,
    provider: str = "exa",
    token: str = "",
    slug: str = "",
    wait_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Watch a run we stopped waiting for, up to EXA_LATE_RESULT_WAIT_S (or
    ``wait_s``), and keep what it finds. Returns the extracted fields ({} if
    nothing). Never raises. Records one ExaCall (``late=True``) with the run's
    final cost.

    With the person's single-flight lease (``slug`` / ``token``: handed over
    by exa_step, or taken by the orphan sweep) it renews the lease before its
    first sleep and on every poll, and stops once someone else holds it (they
    watch the run now). It fills the rows of every lookup that joined the
    lease, then releases it. Cancelled (its worker is going away), it leaves
    the lease to lapse so the next sweep adopts the run."""
    headers = {"Content-Type": "application/json", "x-api-key": EXA_API_KEY, "Exa-Beta": EXA_AGENT_BETA}
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, float(EXA_LATE_RESULT_WAIT_S if wait_s is None else wait_s))
    started = time.monotonic()
    run: Dict[str, Any] = {}
    lease = _Lease(slug, token) if slug and token else None
    release_lease = lease is not None

    def _record(outcome: str, **kw: Any) -> None:
        meta = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        exa_telemetry.record_exa_call(
            "contact", "agent_run", outcome, late=True, exa_run_id=run_id, call_id=meta.get("call_id"),
            tier="deep" if provider == "exa_deep" else "normal", fields_asked=",".join(fields),
            latency_ms=exa_telemetry.latency_ms(started),
            **(exa_telemetry.cost_from_run_json(run) if run.get("status") in _EXA_TERMINAL_STATES else {}),
            **kw,
        )

    try:
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                if lease is not None and not await lease.beat():
                    return {}
                while loop.time() < deadline:
                    await asyncio.sleep(_EXA_LATE_POLL_S)
                    if lease is not None and not await lease.beat():
                        return {}
                    g = await client.get(f"{EXA_AGENT_RUNS_URL}/{run_id}", headers=headers)
                    if g.status_code == 404:
                        _record("not_found", http_status=404)
                        return {}
                    if g.status_code >= 400:
                        continue
                    run = g.json() or {}
                    if run.get("status") in _EXA_TERMINAL_STATES:
                        break
        except Exception as e:
            logger.info("Exa late watch of run %s ended: %s", run_id, e)
            _record("poll_error", error_kind=type(e).__name__)
            return {}
        if run.get("status") != "completed":
            _record(f"run_{run['status']}" if run.get("status") in _EXA_TERMINAL_STATES else "timeout")
            return {}
        extracted = extract_exa_contact_fields((run.get("output") or {}).get("structured"))
        _record("completed", fields_found=_fields_found(extracted))
        email = str(extracted.get("workEmail") or extracted.get("personalEmail") or "") if "email" in fields else ""
        phone = str(extracted.get("mobilePhone") or "") if "phone" in fields else ""
        missed = [f for f, got in (("email", email), ("phone", phone)) if f in fields and not got]
        await contact_cache.record(
            linkedin_url, email=email, phone=phone, email_provider=provider, phone_provider=provider,
            **({"deep_missed": missed} if provider == "exa_deep" else {"missed": missed}),
        )
        joined: List[Dict[str, Any]] = []
        if lease is not None:
            release_lease = False
            joined = await lease.release()
        if email or phone:
            own = [{"candidate_id": candidate_id, "jobdiva_id": jobdiva_id}] if candidate_id else []
            await _fill_late_contexts([*own, *joined], email, phone)
        # Never the LinkedIn URL (application logs go to New Relic): a sourcing
        # lookup has no candidate id, and the run id names it.
        logger.info(
            "Exa late result kept for %s (run %s, %s): %s",
            candidate_id or "a sourcing lookup", run_id, provider,
            ",".join(f for f, got in (("email", email), ("phone", phone)) if got) or "nothing",
        )
        return {"email": email, "phone": phone}
    except asyncio.CancelledError:
        release_lease = False   # left to lapse: the next orphan sweep adopts the run
        raise
    finally:
        if release_lease and lease is not None and not lease.lost:
            await lease.release()


async def _fill_late_contexts(
    contexts: List[Dict[str, Any]], email: str, phone: str, skip: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a late answer to the sourced rows of each context (candidate_id /
    jobdiva_id) still missing those fields; ``skip`` = the caller's own (it
    saves its rows itself). Never raises."""
    seen = set()
    if skip and skip.get("candidate_id"):
        seen.add((str(skip["candidate_id"]), skip.get("jobdiva_id") or None))
    for ctx in contexts:
        key = (str(ctx.get("candidate_id") or ""), ctx.get("jobdiva_id") or None)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        try:
            await asyncio.to_thread(_fill_sourced_rows_sync, {
                "email": email or None, "phone": phone or None,
                "candidate_id": key[0], "jobdiva_id": key[1],
            })
        except Exception as e:
            logger.warning("Exa late result for %s not written to sourced rows: %s", key[0], e)


def watch_late_exa_run(run_id: str, linkedin_url: str, **kw: Any) -> bool:
    """Start keep_late_exa_result in the background. Whether it started (not
    without a run id, a late wait or a running loop). exa_step is its one
    caller (the orphan sweep starts its watches itself)."""
    if not run_id or EXA_LATE_RESULT_WAIT_S <= 0:
        return False
    return _start_late_watch(run_id, linkedin_url, **kw)


def _start_late_watch(run_id: str, linkedin_url: str, **kw: Any) -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(keep_late_exa_result(run_id, linkedin_url, **kw))
    _LATE_WATCHES.add(task)
    task.add_done_callback(_LATE_WATCHES.discard)
    return True


def _fields_found(extracted: Dict[str, Any]) -> str:
    """"email,phone" / "email" / "phone" / "none" of an extracted contact (telemetry)."""
    return ",".join(
        f for f, got in (
            ("email", extracted.get("workEmail") or extracted.get("personalEmail")),
            ("phone", extracted.get("mobilePhone")),
        ) if got
    ) or "none"


# Transport errors after which the create may have reached Exa (a run may be
# going and billed): the run list is checked before a new run is bought. Not
# ConnectError: the request never left, so there is no run to find.
_CREATE_MAY_HAVE_STARTED = (
    httpx.TimeoutException,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


async def _exa_contact_attempt(
    client: Any,
    headers: Dict[str, str],
    body: Dict[str, Any],
    candidate_id: str,
    run_id: str = "",
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Create one agent run and poll it for up to ``timeout_s`` (default
    EXA_CONTACT_ENRICH_TIMEOUT_S), or with ``run_id`` keep polling a run that
    is already going.

    Returns ``{"result": <lookup result>}`` when the run completed, else
    ``{"message", "retry", "run_id"}``: ``retry`` is "wait" (still running or the
    poll failed: watch the same run), "new" (failed/cancelled run, 5xx/429 on
    create, network error: a new run may work) or "" (a 4xx such as bad key or
    no credits, which a retry would not fix). An out-of-credits 402 also trips
    the shared breaker (core/vendor_limiter.EXA).

    Every return records one ExaCall event (cost from the terminal run JSON),
    except a create exa_step's lease stopped before it was sent
    (``lease_lost``: nothing was called).
    """
    tele: Dict[str, Any] = {"outcome": "create_error"}  # every exit sets its own
    started = time.monotonic()
    try:
        return await _exa_contact_attempt_impl(client, headers, body, candidate_id, run_id, timeout_s, tele)
    except BaseException:
        tele["outcome"] = "abandoned"  # cancelled mid-run: a billed run nobody watches
        raise
    finally:
        run = tele.get("run") or {}
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        terminal = run.get("status") in _EXA_TERMINAL_STATES
        if tele.get("outcome") != "lease_lost":
            exa_telemetry.record_exa_call(
                "contact", "agent_run", tele["outcome"],
                effort=str(body.get("effort") or "") or None,
                exa_run_id=str(run.get("id") or tele.get("run_id") or "") or None,
                call_id=meta.get("call_id"),
                http_status=tele.get("http_status"),
                error_kind=tele.get("error_kind"),
                fields_found=tele.get("fields_found"),
                rewatch=bool(run_id) or None,
                latency_ms=exa_telemetry.latency_ms(started),
                **(exa_telemetry.cost_from_run_json(run) if terminal else {}),
            )


async def _exa_contact_attempt_impl(
    client: Any,
    headers: Dict[str, str],
    body: Dict[str, Any],
    candidate_id: str,
    run_id: str,
    timeout_s: Optional[float],
    tele: Dict[str, Any],
) -> Dict[str, Any]:
    """_exa_contact_attempt's body. ``tele`` collects what the ExaCall event
    reports (outcome, status, the last run JSON)."""
    step = _STEP_RUN.get()
    try:
        found_run: Optional[Dict[str, Any]] = None
        if run_id:
            run: Dict[str, Any] = {"id": run_id}
        else:
            # Exa caps concurrent agent runs per account and answers a burst
            # with 429 — retry a couple of times rather than lose the lookup.
            from core.vendor_limiter import EXA as _exa_limit

            for attempt in range(3):
                if step is not None and not await step.before_create():
                    # Another lookup of this person holds its lease now
                    # (exa_step): never two paid runs for one person.
                    tele["outcome"] = "lease_lost"
                    return {"message": "another lookup of this person took over", "retry": "", "lease_lost": True}
                if not await _exa_limit.acquire():
                    tele["outcome"] = "no_slot"
                    return {"message": "Exa rate-limited (no slot)", "retry": "new"}
                try:
                    r = await client.post(EXA_AGENT_RUNS_URL, headers=headers, json=body)
                except _CREATE_MAY_HAVE_STARTED:
                    # Seen on QA 2026-09-29: the run was started but the answer
                    # to the create call never came, and a second run was bought
                    # 30s later. Look the started run up (listing is free).
                    found_run = await _find_started_run(client, headers, str(body.get("query") or ""))
                    if found_run is None:
                        raise
                    logger.info(
                        "Exa agent create answered late for %s: watching run %s",
                        candidate_id, found_run.get("id"),
                    )
                    break
                if r.status_code != 429:
                    break
                # Shared cooldown: the next acquire() on any worker waits it out.
                await _exa_limit.note_429(r.headers.get("retry-after"))
                if attempt == 2:
                    break
        if found_run is not None:
            run = found_run
            run_id = str(run.get("id") or "")
            if step is not None:
                await step.run_started(run_id)
        elif not run_id:
            if r.status_code >= 400:
                logger.warning(
                    "Exa agent create non-2xx for %s: %s %s",
                    candidate_id, r.status_code, r.text[:200],
                )
                transient = r.status_code >= 500 or r.status_code == 429
                tag = credit_tag(r.status_code, r.text)
                if tag:
                    # Out of credits: every worker skips Exa until the
                    # breaker's TTL; this lookup stays a fatal 4xx.
                    await _exa_limit.trip_no_credits(tag)
                tele.update(
                    http_status=r.status_code,
                    error_kind=tag or None,
                    outcome=(
                        "out_of_credits" if tag
                        else "rate_limited" if r.status_code == 429
                        else "create_rejected" if r.status_code == 400
                        else "create_error"
                    ),
                )
                return {
                    "message": f"Exa create error ({r.status_code})",
                    "retry": "new" if transient else "",
                    "status": r.status_code,
                    "detail": (r.text or "")[:200],
                }
            run = r.json()
            run_id = str(run.get("id") or "")
            if step is not None:
                await step.run_started(run_id)
        tele["run"] = run
        tele["run_id"] = run_id

        budget = EXA_CONTACT_ENRICH_TIMEOUT_S if timeout_s is None else timeout_s
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(1, budget)
        while run.get("status") not in _EXA_TERMINAL_STATES:
            if loop.time() >= deadline:
                logger.info("Exa agent run timed out for %s (>%ss)", candidate_id, budget)
                tele["outcome"] = "timeout"
                return {"message": "Exa run timed out", "retry": "wait", "run_id": run_id}
            await asyncio.sleep(_EXA_POLL_INTERVAL_S)
            g = await client.get(f"{EXA_AGENT_RUNS_URL}/{run_id}", headers=headers)
            if g.status_code >= 400:
                logger.warning("Exa agent poll non-2xx for %s: %s", candidate_id, g.status_code)
                # A poll 429 is a rate limit like a create 429 (the Agent errors
                # alert excludes rate_limited), not a broken poll.
                tele.update(
                    outcome="rate_limited" if g.status_code == 429 else "poll_error",
                    http_status=g.status_code,
                )
                return {"message": f"Exa poll error ({g.status_code})", "retry": "wait", "run_id": run_id}
            run = g.json()
            tele["run"] = run
    except Exception as e:
        logger.warning("Exa agent request failed for %s: %s", candidate_id, e)
        # A network failure (ConnectError, a create timeout whose run was not
        # found, a poll ReadTimeout) is a create / poll error like Pass B's,
        # so the Agent errors alert sees it; error_kind names the exception.
        tele.update(outcome="poll_error" if run_id else "create_error", error_kind=type(e).__name__)
        return {"message": f"Exa request failed: {e}", "retry": "wait" if run_id else "new", "run_id": run_id}

    status = run.get("status")
    if status != "completed":
        logger.info("Exa agent run %s for %s: %s", status, candidate_id, run.get("stopReason"))
        tele["outcome"] = f"run_{status}"
        return {"message": f"Exa run {status}", "retry": "new"}
    output = run.get("output") or {}
    extracted = extract_exa_contact_fields(output.get("structured"))
    if not _has_usable_field(extracted):
        logger.info("Exa returned no usable contact fields for %s", candidate_id)
    tele.update(outcome="completed", fields_found=_fields_found(extracted))
    return {"result": {"ok": True, "fields": extracted}}


# ---------------------------------------------------------------------------
# exa_step: one paid Exa contact lookup per person at a time (2026-10-15)
# ---------------------------------------------------------------------------
#
# The person cache stops sequential re-buys; this stops concurrent ones: two
# tabs, a click during Launch PAIR, one person on two jobs, Launch PAIR on two
# workers, a re-search while the first search's lookups still run. The
# person's cache row doubles as a lease (contact_cache.claim):
#
#   leader       claims it once it holds a slot of its semaphore (a lookup
#                still queued for a slot holds nobody up: a click or Launch
#                PAIR never waits on a sourcing lookup that has not started),
#                for the fields the cache still lacks with the slot in hand
#                (the lease names exactly what its run asks), renews it
#                every LEASE_HEARTBEAT_S and before each create (no
#                create is sent once someone else holds the person), stamps
#                the run it started, records the answer, then releases it. A
#                run still going is handed, with the lease, to the late watch;
#                one whose watcher dies with its worker is adopted by
#                resume_orphaned_late_watches.
#   follower     a lookup of the same person meanwhile. Fields the holder is
#                not after it buys at once (a passthrough part); for the rest:
#                when the holder's run covers them at the same tier it polls
#                that run (free; no ExaCall, the owner reports it) and records
#                what it finds, otherwise it re-reads the lease and the cache
#                every FOLLOW_POLL_S and claims what is still unanswered once
#                the lease is free. Out of time, it joins its late-fill
#                context to the lease and answers ``in_progress`` for those
#                fields (never a miss).
#   passthrough  fields the holder is not looking for, the kill switch
#                (CONTACT_LOOKUP_SINGLE_FLIGHT=false) or a DB error
#                (lease_error): no lease, as before the lease existed.

LEASE_HEARTBEAT_S = 20.0
FOLLOW_POLL_S = 3.0
# Orphaned runs one sweep of one worker adopts at most.
ADOPT_LIMIT = 50

# The shielded paid runs of exa_step and the lease heartbeats. Strong
# references: a task only weakly referenced by the loop could vanish mid-run.
_EXA_STEP_TASKS: set = set()


def single_flight_enabled() -> bool:
    """CONTACT_LOOKUP_SINGLE_FLIGHT (default on, read at call time): Exa
    contact lookups of one person share one run across the workers
    (exa_step). Off = each lookup buys its own, as before 2026-10-15."""
    return os.getenv("CONTACT_LOOKUP_SINGLE_FLIGHT", "true").strip().lower() not in {"0", "false", "no", "off"}


def run_horizon_s(tier: str) -> int:
    """How long after its start a run of ``tier`` can still answer: its
    windows (a normal run is waited on twice, a deep one once), the late watch
    (EXA_LATE_RESULT_WAIT_S) and 30 s, at most 870 s. Nobody else buys the
    person's contact while it lasts."""
    deep = tier == "deep"
    window = max(0, EXA_CONTACT_DEEP_TIMEOUT_S if deep else EXA_CONTACT_ENRICH_TIMEOUT_S) * (1 if deep else 2)
    return int(min(contact_cache.MAX_RUN_HORIZON_S, window + max(0, EXA_LATE_RESULT_WAIT_S) + 30))


def _horizons() -> Dict[str, int]:
    return {"normal_horizon_s": run_horizon_s("normal"), "deep_horizon_s": run_horizon_s("deep")}


def _keep_task(task: "asyncio.Future[Any]", tasks: Optional[set] = None) -> None:
    held = _EXA_STEP_TASKS if tasks is None else tasks
    held.add(task)
    task.add_done_callback(held.discard)


class _Lease:
    """The single-flight lease one exa_step (or late watch) holds on a person."""

    def __init__(self, slug: str, token: str) -> None:
        self.slug = slug
        self.token = token
        self.lost = False
        self._beats: Optional["asyncio.Task[None]"] = None

    async def beat(self, run_id: str = "") -> bool:
        """Renew it (stamping ``run_id`` when given). False once someone else
        holds the person; a DB error counts as still held (fail open)."""
        if not self.lost and await contact_cache.heartbeat(self.slug, self.token, run_id=run_id) == 0:
            self.lost = True
        return not self.lost

    async def _beat_loop(self) -> None:
        while not self.lost:
            await asyncio.sleep(LEASE_HEARTBEAT_S)
            await self.beat()

    def start_beats(self) -> None:
        if self._beats is None:
            self._beats = asyncio.ensure_future(self._beat_loop())
            _keep_task(self._beats)

    def stop_beats(self) -> None:
        if self._beats is not None:
            self._beats.cancel()

    async def release(self) -> List[Dict[str, Any]]:
        """End it: the late-fill contexts joined to it ([] when none, or when
        it is no longer ours)."""
        self.stop_beats()
        return (await contact_cache.release(self.slug, self.token)) or []

    def release_soon(self) -> None:
        """release() from a cancel handler, in the background so the cancel
        is not held up."""
        self.stop_beats()
        _keep_task(asyncio.ensure_future(contact_cache.release(self.slug, self.token)))


class _StepRun:
    """What exa_step's paid run hears from inside exa_enrich_by_linkedin: a
    create about to be sent (the lease is renewed first; False = do not send
    it) and the run it started. Through the _STEP_RUN ContextVar, so neither
    exa_enrich_by_linkedin nor _exa_contact_attempt changes signature (their
    test fakes keep working)."""

    __slots__ = ("lease", "run_id")

    def __init__(self, lease: Optional[_Lease]) -> None:
        self.lease = lease
        self.run_id = ""

    async def before_create(self) -> bool:
        return self.lease is None or await self.lease.beat()

    async def run_started(self, run_id: str) -> None:
        self.run_id = run_id
        if self.lease is not None and run_id:
            await self.lease.beat(run_id)


_STEP_RUN: ContextVar[Optional[_StepRun]] = ContextVar("exa_step_run", default=None)


def _asked_contact(fields: Tuple[str, ...], found: Dict[str, Any]) -> Tuple[str, str]:
    """The email / personal phone a lookup of ``fields`` found."""
    email = str(found.get("workEmail") or found.get("personalEmail") or "").strip().lower() if "email" in fields else ""
    phone = _normalise_phone(str(found.get("mobilePhone") or "")) if "phone" in fields else ""
    if sum(1 for ch in phone if ch.isdigit()) < 7:
        phone = ""
    return email, phone


def _contact_fields(values: Dict[str, str]) -> Dict[str, Any]:
    """Lookup fields (extract_exa_contact_fields' shape) of an email / phone."""
    email = values.get("email") or ""
    personal = bool(email) and email.rsplit("@", 1)[-1].lower() in _PERSONAL_EMAIL_DOMAINS
    phone = values.get("phone") or ""
    return {
        "mobilePhone": phone,
        "workPhone": "",
        "workEmail": "" if personal else email,
        "personalEmail": email if personal else "",
        "phoneCandidates": [phone] if phone else [],
    }


def _answered_since(
    before: Dict[str, Any], after: Dict[str, Any], fields: Tuple[str, ...], deep: bool,
) -> Dict[str, str]:
    """Which of ``fields`` the cache answered between two reads: {field:
    value}, or {field: ""} for a miss recorded meanwhile (a deep search's for
    a deep lookup, any for a normal one). A miss it already held does not
    count, so a re-buy the recruiter confirmed stays one."""
    out: Dict[str, str] = {}
    for f in fields:
        if after.get(f):
            out[f] = str(after[f])
            continue
        for kind in ("deep_missed",) if deep else ("missed", "deep_missed"):
            if after.get(f"{f}_{kind}") and (
                not before.get(f"{f}_{kind}") or after.get(f"{f}_{kind}_at") != before.get(f"{f}_{kind}_at")
            ):
                out[f] = ""
                break
    return out


async def _keep_answer(
    linkedin_url: str, fields: Tuple[str, ...], deep: bool, result: Dict[str, Any],
) -> Tuple[str, str]:
    """Cache what an Exa run found and, when it answered, which of ``fields``
    it found nothing for (a deep search's miss for a deep run). Returns the
    email / phone it found."""
    answered = bool(result.get("ok"))
    email, phone = _asked_contact(fields, (result.get("fields") or {}) if answered else {})
    missed = [f for f, got in (("email", email), ("phone", phone)) if answered and f in fields and not got]
    if email or phone or missed:
        provider = "exa_deep" if deep else "exa"
        await contact_cache.record(
            linkedin_url, email=email, phone=phone, email_provider=provider, phone_provider=provider,
            missed=[] if deep else missed, deep_missed=missed if deep else [],
        )
    return email, phone


class _Step:
    """One exa_step call."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)
        self.got: Dict[str, str] = {}     # fields the cache answered meanwhile ("" = a miss)
        self.paid = False                 # a paid run of its own was started
        self.bought = False               # ... and sent (not skipped, not stopped by the lease)
        self.run_id = ""                  # the run it started (telemetry)
        self.parts: List[Dict[str, Any]] = []   # answers of fields bought apart (partial overlap)
        self.going: set = set()           # fields a run (ours or the holder's) may still answer


async def exa_step(
    candidate_id: str,
    linkedin_url: str,
    full_name: str = "",
    company: str = "",
    *,
    fields: Tuple[str, ...] = EXA_CONTACT_FIELDS,
    deep: bool = False,
    wait_retry: bool = True,
    semaphore: Any = None,
    cached: Optional[Dict[str, Any]] = None,
    late: Optional[Dict[str, Any]] = None,
    enrich: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    tasks: Optional[set] = None,
    on_unused: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """One paid Exa contact step (the normal run, or the ``deep`` search) for
    ``fields`` of one person, single-flight across the workers (see above).

    Returns exa_enrich_by_linkedin's answer (``ok`` / ``fields`` / ``message``
    / ``fatal`` / ``run_id`` / ``skipped``) plus ``role`` (leader / follower /
    passthrough), ``from_cache`` (the cache answered meanwhile: nothing
    bought), ``in_progress`` (another lookup's run for some of the fields is
    still going: not an answer, never a miss) and ``in_progress_fields`` (the
    fields a run, ours handed to the late watch or another lookup's, may
    still answer; [] when none). Fields the holder is not after are bought at
    once, so ``ok`` with ``fields`` can come together with ``in_progress``
    for the rest; ``fields`` can also come without ``ok`` when one part
    answered and the other failed. It caches the answer itself (before the
    lease is released) and is the one place a run still going is handed to
    the late watch.

    ``semaphore``: the in-flight bound a paid run waits on (the lease is
    claimed only with a slot in hand; a follower never holds one). ``cached``:
    contact_cache.get() as the caller read it, against
    which "answered meanwhile" is judged (read here when omitted). ``late``:
    candidate_id / jobdiva_id of the rows a late answer fills. ``enrich``:
    exa_enrich_by_linkedin, or a caller's alias of it. The paid run is
    shielded (kept in ``tasks``): a cancelled caller never loses an answer
    already paid for. ``on_unused`` is called once, synchronously, when this
    step bought nothing itself: cancelled before its run started, answered
    by the cache or another lookup's run, or skipped by the out-of-credits
    breaker. Never raises except CancelledError.
    """
    wanted = tuple(f for f in EXA_CONTACT_FIELDS if f in (fields or ()))
    step = _Step(
        candidate_id=candidate_id, linkedin_url=linkedin_url, full_name=full_name or "",
        company=company or "", wanted=wanted, deep=bool(deep), wait_retry=wait_retry,
        tier="deep" if deep else "normal", semaphore=semaphore, before=cached,
        late={k: late[k] for k in ("candidate_id", "jobdiva_id") if (late or {}).get(k)},
        enrich=enrich or exa_enrich_by_linkedin, tasks=tasks,
        slug=contact_cache.linkedin_slug(linkedin_url),
    )
    with exa_telemetry.scope(tier=step.tier, fields_asked=",".join(wanted)):
        try:
            if not (wanted and step.slug and EXA_API_KEY and single_flight_enabled() and contact_cache.enabled()):
                result = await _passthrough(step, wanted, "off")
            else:
                if step.before is None:
                    step.before = await contact_cache.get(linkedin_url)
                result = await _single_flight(step)
        except asyncio.CancelledError:
            if not step.paid and on_unused is not None:
                on_unused()
            raise
    # Nothing of its own was sent: answered by the cache or another lookup's
    # run, skipped by the breaker, or its lease taken before the create.
    if on_unused is not None and not step.bought:
        on_unused()
    return result


async def _passthrough(
    step: _Step, fields: Tuple[str, ...], reason: str, permit: Optional[_SlotPermit] = None, **attrs: Any,
) -> Dict[str, Any]:
    """A paid run without a lease (in ``permit``'s slot when one was taken)."""
    with exa_telemetry.scope(role="passthrough"):
        result = await _run_paid(step, fields, None, permit)
    return _finish(step, result, "passthrough", "lease_error" if reason == "lease_error" else "passthrough",
                   reason=reason, **attrs)


async def _buy_part(step: _Step, fields: Tuple[str, ...]) -> None:
    """Partial overlap: the fields the person's holder is not after, bought
    at once without a lease (a passthrough) while the step waits for the
    rest. Its answer is kept in ``step.parts`` (``_finish`` folds it in); one
    ContactLookup (passthrough, ``reason`` partial_overlap)."""
    own_run = step.run_id
    with exa_telemetry.scope(role="passthrough", fields_asked=",".join(fields)):
        result = await _run_paid(step, fields, None)
        exa_telemetry.record_contact_lookup(
            "passthrough", reason="partial_overlap", from_cache=bool(result.get("from_cache")) or None,
            exa_run_id=str(result.get("run_id") or step.run_id or "") or None,
        )
    step.run_id = own_run   # the run of the rest, if it leads, names the step
    step.parts.append(result)


def _with_part(result: Dict[str, Any], part: Dict[str, Any]) -> Dict[str, Any]:
    """``result`` (the fields another lookup was after) joined with ``part``
    (the rest, bought apart): the fields either found; ``ok`` only when both
    answered (or the first is still going: ``in_progress`` covers it), so no
    field of a failed part is ever taken for a miss; ``fatal`` / the run
    still going of either; ``from_cache`` / ``skipped`` only when both."""
    out = dict(result)
    found = dict(result.get("fields") or {})
    for key, value in (part.get("fields") or {}).items():
        if value and not found.get(key):
            found[key] = value
    if found:
        out["fields"] = found
    out["ok"] = bool(part.get("ok")) and bool(result.get("ok") or result.get("in_progress"))
    if not out["ok"] and not out.get("message") and part.get("message"):
        out["message"] = part["message"]
    if part.get("fatal"):
        out["fatal"] = True
    if part.get("run_id") and not out.get("run_id"):
        out["run_id"] = part["run_id"]
    for flag in ("from_cache", "skipped"):
        if not (result.get(flag) and part.get(flag)):
            out.pop(flag, None)
    return out


def _finish(step: _Step, result: Dict[str, Any], role: str, outcome: str, **attrs: Any) -> Dict[str, Any]:
    """The step's answer: ``result`` plus what the cache answered meanwhile
    and what was bought apart (``step.parts``), with ``in_progress_fields``;
    and its ContactLookup event (for ``result``'s part)."""
    result = {**result, "role": role}
    if step.got and result.get("ok"):
        found = dict(result.get("fields") or {})
        for key, value in _contact_fields(step.got).items():
            if value and not found.get(key):
                found[key] = value
        result["fields"] = found
    run_ref = attrs.pop("exa_run_id", "") or result.get("run_id") or step.run_id
    exa_telemetry.record_contact_lookup(
        outcome, from_cache=bool(result.get("from_cache")) or None, exa_run_id=str(run_ref or "") or None, **attrs,
    )
    for part in step.parts:
        result = _with_part(result, part)
    result["in_progress_fields"] = [f for f in step.wanted if f in step.going and f not in step.got]
    return result


async def _single_flight(step: _Step) -> Dict[str, Any]:
    """exa_step with the lease: lead, follow or pass through."""
    loop = asyncio.get_running_loop()
    window = max(1, (EXA_CONTACT_DEEP_TIMEOUT_S if step.deep else EXA_CONTACT_ENRICH_TIMEOUT_S) * (
        2 if (not step.deep and step.wait_retry and EXA_CONTACT_ENRICH_RETRY) else 1
    ))
    # The window starts once the person is first found held, so a lookup
    # that queued for a slot before it found someone else had it still gets
    # all of it.
    deadline: Optional[float] = None
    started = time.monotonic()
    remaining = step.wanted
    dead_runs: set = set()
    lease_lost = False
    followed = ""

    async def _pause() -> None:
        await asyncio.sleep(max(0.0, min(FOLLOW_POLL_S, deadline - loop.time())))

    while True:
        held = await contact_cache.holder(step.slug, **_horizons())
        if held["state"] == "error":
            return await _passthrough(step, remaining, "lease_error")
        if held["state"] == "free":
            status, token, permit, remaining = await _claim_with_slot(step, remaining)
            if status == "cached":
                # The cache answered the rest once the slot was in hand:
                # nothing claimed, nothing bought.
                return _finish(step, {"ok": True, "fields": {}, "from_cache": True}, "follower", "follower",
                               lease_lost=lease_lost or None)
            if status == "error":
                return await _passthrough(step, remaining, "lease_error", permit)
            if status == "won":
                with exa_telemetry.scope(role="leader"):
                    result = await _run_paid(step, remaining, _Lease(step.slug, token), permit)
                if not result.get("lease_lost"):
                    return _finish(step, result, "leader", "leader", lease_lost=lease_lost or None)
                lease_lost = True  # another lookup holds the person now: follow it
                step.paid = False  # nothing was sent
                continue
            # Another lookup claimed it first: read it on the next round.
            deadline = loop.time() + window if deadline is None else deadline
            await _pause()
        else:
            deadline = loop.time() + window if deadline is None else deadline
            run_id = held.get("run_id") or ""
            overlap = tuple(f for f in remaining if f in held["fields"])
            if not overlap:
                return await _passthrough(step, remaining, "no_overlap")
            if overlap != remaining:
                # What the holder is not after is bought now (Launch PAIR's
                # phone behind a sourcing lookup of the email); only the
                # overlap waits on it.
                await _buy_part(step, tuple(f for f in remaining if f not in overlap))
                remaining = overlap
            elif run_id and run_id not in dead_runs and held["tier"] == step.tier:
                # The holder's run asks what we want: wait for it (free polls).
                followed = run_id
                run = await _follow_run(run_id, deadline)
                if run.get("status") == "completed":
                    found = extract_exa_contact_fields((run.get("output") or {}).get("structured"))
                    email, phone = _asked_contact(remaining, found)
                    result = {"ok": True, "fields": _contact_fields({"email": email, "phone": phone})}
                    # What the run did not find may be a field its holder stopped
                    # asking for once the cache answered it: not a miss.
                    unfound = tuple(f for f, got in (("email", email), ("phone", phone)) if f in remaining and not got)
                    if unfound:
                        step.got.update(_answered_since(
                            step.before, await contact_cache.get(step.linkedin_url), unfound, step.deep,
                        ))
                    await _keep_answer(
                        step.linkedin_url, tuple(f for f in remaining if f not in step.got), step.deep, result,
                    )
                    return _finish(step, result, "follower", "follower", exa_run_id=run_id,
                                   waited_ms=exa_telemetry.latency_ms(started), lease_lost=lease_lost or None)
                if run.get("status") in _EXA_TERMINAL_STATES:
                    dead_runs.add(run_id)  # failed / cancelled: its holder may start another
                elif loop.time() < deadline:
                    # The run could not be read (Exa unreachable, a bad
                    # answer): wait as any follower does, never spin.
                    await _pause()
            else:
                await _pause()
        # What the cache answered meanwhile (a holder records before it releases).
        remaining = await _unanswered(step, remaining)
        if not remaining:
            return _finish(step, {"ok": True, "fields": {}, "from_cache": True}, "follower", "follower",
                           waited_ms=exa_telemetry.latency_ms(started), lease_lost=lease_lost or None)
        if loop.time() >= deadline:
            joined = await contact_cache.join(step.slug, step.late)
            if not joined:
                # Nobody holds the person any more? Its holder may have
                # recorded the answer and released since the read above.
                remaining = await _unanswered(step, remaining)
                if not remaining:
                    return _finish(step, {"ok": True, "fields": {}, "from_cache": True}, "follower", "follower",
                                   waited_ms=exa_telemetry.latency_ms(started), lease_lost=lease_lost or None)
            step.going.update(remaining)
            return _finish(
                step,
                {"ok": bool(step.got), "in_progress": True,
                 "message": "Another lookup of this person is still running; its answer is kept"},
                "follower", "in_progress", exa_run_id=followed, joined=joined or None,
                waited_ms=exa_telemetry.latency_ms(started), lease_lost=lease_lost or None,
            )


async def _unanswered(step: _Step, fields: Tuple[str, ...]) -> Tuple[str, ...]:
    """``fields`` less those the cache answered since the caller read it
    (kept in ``step.got``)."""
    if step.before is None:
        return fields
    step.got.update(_answered_since(step.before, await contact_cache.get(step.linkedin_url), fields, step.deep))
    return tuple(f for f in fields if f not in step.got)


def _give_back_claim(slug: str) -> Callable[["asyncio.Future[Tuple[str, str]]"], None]:
    """Done-callback of a claim whose caller was cancelled while it ran: the
    claim still commits in its thread, so a lease it won is released here
    (else nobody holds or renews it, and every lookup of the person waits up
    to contact_cache.LEASE_S for it to lapse)."""
    def _done(claim: "asyncio.Future[Tuple[str, str]]") -> None:
        if claim.cancelled() or claim.exception() is not None:
            return
        status, token = claim.result()
        if status == "won":
            _Lease(slug, token).release_soon()
    return _done


async def _claim_with_slot(
    step: _Step, fields: Tuple[str, ...],
) -> Tuple[str, str, Optional[_SlotPermit], Tuple[str, ...]]:
    """A slot of the step's semaphore first, then the cache looked at again
    with the slot in hand (a lookup of the same person that held the slot,
    or any other lookup meanwhile, may have answered some of ``fields``),
    then the person's lease for what is still unanswered: the lease names
    exactly the fields its run asks, so a lookup of a field no run is after
    never waits on it. A lookup still queued for a slot never holds the
    person, so it never holds up one that could run now (a click or Launch
    PAIR behind a queued sourcing lookup).

    (status, token, permit, the fields to buy): ("won", token, permit, ...),
    ("held", "", None, ...) (the slot given back), ("cached", "", None, ())
    (the cache answered them all: nothing claimed, the slot given back) or
    ("error", "", permit, ...) (fail open: run in that slot). A caller
    cancelled while the claim runs releases the lease it took."""
    permit: Optional[_SlotPermit] = None
    if step.semaphore is not None:
        await step.semaphore.acquire()
        permit = _SlotPermit(step.semaphore)
    claim: Optional["asyncio.Future[Tuple[str, str]]"] = None
    try:
        if permit is not None:
            fields = await _unanswered(step, fields)
            if not fields:
                permit.release()
                return "cached", "", None, ()
        claim = asyncio.ensure_future(contact_cache.claim(
            step.slug, fields, step.tier, ctx=step.late or None, **_horizons(),
        ))
        _keep_task(claim)
        status, token = await asyncio.shield(claim)
    except BaseException:
        if permit is not None:
            permit.release()
        if claim is not None:
            claim.add_done_callback(_give_back_claim(step.slug))
        raise
    if status == "held" and permit is not None:
        permit.release()
        permit = None
    return status, token, permit, fields


async def _follow_run(run_id: str, deadline: float) -> Dict[str, Any]:
    """Poll another lookup's run until it ends or ``deadline``. GETs are free
    and send no ExaCall (the run's owner reports it). The run JSON last seen
    ({} when none could be read); a GET that fails ends it early (the caller
    waits FOLLOW_POLL_S before it looks again)."""
    headers = {"Content-Type": "application/json", "x-api-key": EXA_API_KEY, "Exa-Beta": EXA_AGENT_BETA}
    loop = asyncio.get_running_loop()
    run: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            while loop.time() < deadline:
                g = await client.get(f"{EXA_AGENT_RUNS_URL}/{run_id}", headers=headers)
                if g.status_code == 404:
                    return {"status": "failed"}
                if g.status_code < 400:
                    body = g.json()
                    run = body if isinstance(body, dict) else {}
                    if run.get("status") in _EXA_TERMINAL_STATES:
                        return run
                await asyncio.sleep(max(0.0, min(_EXA_POLL_INTERVAL_S, deadline - loop.time())))
    except Exception as e:
        logger.info("Exa run %s could not be followed: %s", run_id, e)
    return run


async def _run_paid(
    step: _Step, fields: Tuple[str, ...], lease: Optional[_Lease], permit: Optional[_SlotPermit] = None,
) -> Dict[str, Any]:
    """A run of this step's own (the leader's, or one without a lease) in a
    slot of the step's semaphore (``permit``: one already taken, the cache
    looked at again by its taker, _claim_with_slot; else it is waited for
    here and the cache looked at again with it in hand), then the run,
    shielded. Its fields go in ``step.going`` while it is going. A leader's
    ``fields`` are never narrowed here: its lease names them."""
    if lease is not None:
        lease.start_beats()
    try:
        if permit is None and step.semaphore is not None:
            await step.semaphore.acquire()
            permit = _SlotPermit(step.semaphore)
            if lease is None:
                # A lookup of the same person that held the slot (a
                # re-search after Stop, another row of the person, a lookup
                # without a lease) may have answered it meanwhile.
                fields = await _unanswered(step, fields)
                if not fields:
                    permit.release()
                    return {"ok": True, "fields": {}, "from_cache": True}
        task = asyncio.ensure_future(_paid_run(step, fields, lease, permit))
    except BaseException:
        if permit is not None:
            permit.release()
        if lease is not None:
            lease.release_soon()
        raise
    step.paid = True
    if permit is not None:
        # Gives the slot back should the task be cancelled before it ever
        # runs (loop shutdown); a no-op once the task released it.
        task.add_done_callback(permit.release)
    _keep_task(task, step.tasks)
    result = await asyncio.shield(task)
    if result.get("run_id"):
        step.going.update(fields)   # handed to the late watch: it may still answer
    return result


async def _paid_run(
    step: _Step, fields: Tuple[str, ...], lease: Optional[_Lease], permit: Optional[_SlotPermit],
) -> Dict[str, Any]:
    """_run_paid's shielded part: the run, its answer cached, then the lease
    released (the rows of lookups that joined it filled) or handed with a run
    still going to the late watch. Never raises (but CancelledError)."""
    run = _StepRun(lease)
    ctx_token = _STEP_RUN.set(run)
    keep_lease = False   # handed to the late watch, or left to the orphan sweep
    released = False
    try:
        kw: Dict[str, Any] = {"fields": fields}
        if step.deep:
            kw["deep"] = True
        if not step.wait_retry:
            kw["wait_retry"] = False
        try:
            result = await step.enrich(step.candidate_id, step.linkedin_url, step.full_name, step.company, **kw)
        except Exception as e:
            logger.warning("Exa contact step raised for %s: %s", step.candidate_id, e)
            result = {"ok": False, "message": f"Exa request failed: {e}"}
        if result.get("lease_lost") or result.get("skipped"):
            return result   # nothing was sent: nothing to remember
        step.bought = True
        email, phone = await _keep_answer(step.linkedin_url, fields, step.deep, result)
        run_id = str(result.get("run_id") or "")
        if run_id:
            keep_lease = watch_late_exa_run(
                run_id, step.linkedin_url, fields=fields, provider="exa_deep" if step.deep else "exa",
                **step.late, **({"token": lease.token, "slug": lease.slug} if lease is not None else {}),
            ) is True and lease is not None
        if lease is not None and not keep_lease:
            released = True
            joined = await lease.release()
            if email or phone:
                await _fill_late_contexts(joined, email, phone, skip=step.late)
        return result
    except asyncio.CancelledError:
        # Only the loop going away reaches this shielded task: a run already
        # started is left to the orphan sweep (its lease lapses).
        keep_lease = keep_lease or bool(run.run_id)
        raise
    finally:
        _STEP_RUN.reset(ctx_token)
        step.run_id = run.run_id or step.run_id
        if permit is not None:
            permit.release()
        if lease is not None:
            lease.stop_beats()
            if not (keep_lease or released):
                lease.release_soon()


async def resume_orphaned_late_watches() -> int:
    """APScheduler job (main.py), every 60 s in every worker: adopt the Exa
    contact runs whose late watch died with its worker (a deploy, a recycle
    under --limit-max-requests) while they can still answer, and watch each
    until its horizon. Each orphan goes to one worker (FOR UPDATE SKIP
    LOCKED), so no advisory lock. One WARNING and one ContactLookup
    (``adopted``) per run. Returns how many it adopted; never raises."""
    try:
        from core.tasks import is_stopping

        if is_stopping() or not single_flight_enabled() or not EXA_API_KEY or EXA_LATE_RESULT_WAIT_S <= 0:
            return 0
        adopted = 0
        for row in await contact_cache.adopt_orphans(limit=ADOPT_LIMIT, **_horizons()):
            if not (row["run_id"] and row["slug"] and row["token"]):
                continue
            fields = row["fields"] or EXA_CONTACT_FIELDS
            with exa_telemetry.scope(path="late_sweep", role="adopted", tier=row["tier"], fields_asked=",".join(fields)):
                # Never the profile (application logs go to New Relic): the run id names it.
                logger.warning(
                    "Exa contact run %s adopted: its watcher stopped with its worker (%ds left)",
                    row["run_id"], int(row["remaining_s"]),
                )
                exa_telemetry.record_contact_lookup(
                    "adopted", exa_run_id=row["run_id"], remaining_s=int(row["remaining_s"]),
                )
                if _start_late_watch(
                    row["run_id"], contact_cache.profile_url(row["slug"]), fields=fields,
                    provider="exa_deep" if row["tier"] == "deep" else "exa",
                    token=row["token"], slug=row["slug"], wait_s=row["remaining_s"],
                ):
                    adopted += 1
        return adopted
    except Exception as e:  # noqa: BLE001 - a scheduler job never raises
        logger.warning("Exa late-watch sweep failed: %s", e)
        return 0


ZOOMINFO_NEW_SEARCH_URL = "https://api.zoominfo.com/gtm/data/v1/contacts/search"
ZOOMINFO_NEW_ENRICH_URL = "https://api.zoominfo.com/gtm/data/v1/contacts/enrich"

# Minimum acceptance threshold for a ContactSearch match. ZoomInfo's
# `contactAccuracyScore` is a 0-100 confidence; below this we treat the
# search result as a miss rather than enriching the wrong person.
_MIN_CONTACT_ACCURACY_SCORE = 50.0


def _split_name(full_name: str) -> Dict[str, str]:
    """Split a free-form name into firstName / lastName for ContactSearch.

    Empty parts return empty strings — caller checks for both before invoking
    the search.
    """
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return {"first": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0], "last": ""}
    return {"first": parts[0], "last": " ".join(parts[1:])}


async def _zoominfo_authed_post(
    url: str,
    json_body: Dict[str, Any],
    *,
    timeout: float = 15.0,
) -> Optional[httpx.Response]:
    """POST to a ZoomInfo Data API endpoint with auto-minted auth + 401 retry.

    Returns the httpx.Response on a clean call (any status). Returns None when
    auth isn't configured or the token endpoint itself fails — those are
    soft "skip ZoomInfo for this call" signals, not exceptions.

    Retries exactly once on 401 with ``force_refresh=True`` to handle the
    case where ZoomInfo revoked the cached token server-side (admin rotated
    the secret, max session count hit, etc.).
    """
    try:
        token = await get_access_token()
    except ZoomInfoAuthNotConfigured as exc:
        logger.info("zoominfo disabled: %s", exc)
        return None
    except ZoomInfoAuthFailed as exc:
        logger.warning("zoominfo token mint failed: %s", exc)
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        logger.warning("zoominfo POST %s failed: %s", url, exc)
        return None

    if res.status_code != 401:
        return res

    # Token may have been revoked server-side; mint a fresh one and retry once.
    logger.info("zoominfo 401 on %s — forcing token refresh and retrying", url)
    try:
        token = await get_access_token(force_refresh=True)
    except (ZoomInfoAuthNotConfigured, ZoomInfoAuthFailed) as exc:
        logger.warning("zoominfo force-refresh failed: %s", exc)
        return res  # return original 401 so caller logs are accurate

    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        logger.warning("zoominfo POST %s retry failed: %s", url, exc)
        return None


def current_company_of(candidate: Any) -> str:
    """The candidate's current employer from whatever the row carries: explicit
    company fields, the LinkedIn profile captured at sourcing, the stored
    company_experience, the LLM extraction or the deep-search recent roles.
    "" when none of them names one. Sharpens the ZoomInfo name search and the
    Exa contact query."""
    if not isinstance(candidate, dict):
        return ""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else {}
    enhanced = next(
        (v for v in (candidate.get("enhanced_info"), data.get("enhanced_info")) if isinstance(v, dict)), {}
    )
    for value in (
        candidate.get("current_company"), candidate.get("company_name"), candidate.get("company"),
        data.get("current_company"), data.get("company_name"), data.get("company"),
        enhanced.get("current_company"), enhanced.get("company"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    profile = next(
        (v for v in (candidate.get("linkedin_profile"), data.get("linkedin_profile")) if isinstance(v, dict)), {}
    )
    for roles in (
        profile.get("experience"),
        candidate.get("company_experience"), data.get("company_experience"),
        enhanced.get("company_experience"),
        candidate.get("exa_recent_companies"),
    ):
        if not isinstance(roles, list):
            continue
        roles = [r for r in roles if isinstance(r, dict) and str(r.get("company") or "").strip()]
        current = [
            r for r in roles
            if r.get("current") or str(r.get("end") or r.get("end_date") or "").strip().lower() in ("", "present", "current")
        ]
        for role in current or roles:
            company = role.get("company")
            if isinstance(company, dict):
                company = company.get("name")
            if str(company or "").strip():
                return str(company).strip()
    return ""


async def _zoominfo_contact_search(first: str, last: str, company: str = "") -> Optional[Dict[str, Any]]:
    """One ContactSearch; ``{"data": [...], "total": int}`` or None on failure."""
    attributes = {"firstName": first, "lastName": last}
    if company:
        attributes["companyName"] = company
    body = {"data": {"type": "ContactSearch", "attributes": attributes}}
    res = await _zoominfo_authed_post(ZOOMINFO_NEW_SEARCH_URL, body)
    if res is None or res.status_code >= 400:
        if res is not None:
            logger.info(
                "zoominfo ContactSearch non-2xx for %s %s (company=%s): %s",
                first, last, company or "-", res.status_code,
            )
        return None
    try:
        body_json = res.json()
    except ValueError:
        return None
    data = body_json.get("data") if isinstance(body_json, dict) else None
    if not isinstance(data, list):
        return None
    meta = body_json.get("meta") if isinstance(body_json.get("meta"), dict) else {}
    try:
        total = int(meta.get("totalResults"))
    except (TypeError, ValueError):
        total = len(data)
    return {"data": data, "total": total}


async def _zoominfo_resolve_person_id(full_name: str, company: str = "") -> Optional[str]:
    """ContactSearch by name, return the personId only when the match is the person.

    ZoomInfo doesn't accept `linkedinUrl` as a search filter (tested:
    `PFAPI0005 / Invalid field requested`), so a name is the only entry point --
    and a bare name is not an identity. ``contactAccuracyScore`` rates how
    reliable ZoomInfo's data about a record is, NOT how well it matches the
    search, so taking the top "John Smith" enriched a stranger and put their
    phone on our candidate. A hit is accepted only when it is the ONE match:

    1. with the candidate's current ``company`` (ContactSearch ``companyName``);
    2. else (no company, or nobody there) by name alone, unique in ZoomInfo.

    Ambiguous names return None and the chain moves on to Apollo, which matches
    by the LinkedIn URL itself. Hits below ``_MIN_CONTACT_ACCURACY_SCORE`` are
    still skipped.
    """
    parts = _split_name(full_name)
    if not parts["first"] or not parts["last"]:
        return None

    company = (company or "").strip()
    found = None
    for scope in ([company, ""] if company else [""]):
        found = await _zoominfo_contact_search(parts["first"], parts["last"], scope)
        if found and found["data"]:
            if found["total"] == 1:
                break
            logger.info(
                "zoominfo ContactSearch ambiguous for %s (company=%s): %d matches — not guessing",
                full_name, scope or "-", found["total"],
            )
        found = None
    if not found:
        return None

    top = found["data"][0]
    attrs = top.get("attributes") if isinstance(top, dict) else {}
    score = (attrs or {}).get("contactAccuracyScore")
    try:
        score_val = float(score)
    except (TypeError, ValueError):
        score_val = 0.0
    if score_val < _MIN_CONTACT_ACCURACY_SCORE:
        logger.info(
            "zoominfo ContactSearch low score %.1f for %s — skipping",
            score_val,
            full_name,
        )
        return None

    person_id = top.get("id")
    return str(person_id) if person_id else None


async def _zoominfo_enrich_by_person_id(person_id: str) -> Dict[str, str]:
    """ContactEnrich by personId. Returns the canonical four-field dict."""
    body = {
        "data": {
            "type": "ContactEnrich",
            "attributes": {
                "matchPersonInput": [{"personId": person_id}],
                "outputFields": ["mobilePhone", "phone", "email", "emailAlt"],
            },
        }
    }
    res = await _zoominfo_authed_post(ZOOMINFO_NEW_ENRICH_URL, body)
    if res is None or res.status_code >= 400:
        if res is not None:
            logger.info(
                "zoominfo ContactEnrich non-2xx for personId=%s: %s",
                person_id,
                res.status_code,
            )
        return {}

    try:
        body_json = res.json()
    except ValueError:
        return {}

    return extract_zoominfo_contact_fields(body_json)


async def zoominfo_enrich_by_email(candidate_id: str, email: str) -> Dict[str, Any]:
    """Enrich a contact via the ZoomInfo OAuth Data API, matching by EMAIL.

    ZoomInfo cannot match by LinkedIn URL (externalURL is output-only and
    entitlement-gated), so this runs only when we already have an email. Reuses
    ``_zoominfo_authed_post`` (OAuth mint + one 401 retry). Returns
    ``{"ok": bool, "fields"|"message": ...}`` like the Apollo/Exa helpers; all
    failures are logged and swallowed so enrichment never raises.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "message": "no email to match on"}

    body = {
        "data": {
            "type": "ContactEnrich",
            "attributes": {
                "matchPersonInput": [{"emailAddress": email}],
                "outputFields": ["mobilePhone", "phone", "email", "emailAlt"],
            },
        }
    }
    res = await _zoominfo_authed_post(ZOOMINFO_NEW_ENRICH_URL, body)
    if res is None:
        return {"ok": False, "message": "ZoomInfo unavailable (auth not configured / mint failed)"}
    if res.status_code >= 400:
        logger.info("ZoomInfo enrich-by-email non-2xx for %s: %s", candidate_id, res.status_code)
        return {"ok": False, "message": f"ZoomInfo error ({res.status_code})"}
    try:
        data = res.json()
    except ValueError:
        return {"ok": False, "message": "ZoomInfo non-JSON response"}

    fields = extract_zoominfo_contact_fields(data)
    if not _has_usable_field(fields):
        logger.info("ZoomInfo enrich-by-email returned no usable fields for %s", candidate_id)
    return {"ok": True, "fields": fields}


async def _zoominfo_enrich_for_sourcing(full_name: str, company: str = "") -> Dict[str, str]:
    """Two-call ZoomInfo lookup (ContactSearch -> ContactEnrich by personId).

    Returns the canonical four-field dict (possibly all empty). Empty dict on
    any miss -- including an ambiguous name -- and the caller falls through to
    Apollo.
    """
    person_id = await _zoominfo_resolve_person_id(full_name, company)
    if not person_id:
        return {}
    return await _zoominfo_enrich_by_person_id(person_id)


def _has_usable_field(fields: Dict[str, Any]) -> bool:
    return any(str(fields.get(k) or "").strip() for k in ("mobilePhone", "workPhone", "workEmail", "personalEmail"))


async def zoominfo_enrich_by_name(candidate_id: str, full_name: str, company: str = "") -> Dict[str, Any]:
    """Enrich a contact via the ZoomInfo OAuth Data API, matching by NAME.

    ZoomInfo cannot match by LinkedIn URL, and URL-only candidates (e.g.
    Exa-sourced) arrive with no seed email, so neither the by-URL nor the
    by-email path can reach them. Name-based ContactSearch -> personId ->
    ContactEnrich is the only ZoomInfo entry point for these. Requires both a
    first and last name; ``_zoominfo_resolve_person_id`` accepts only an
    unambiguous match (scoped to ``company`` when known) so a name collision
    never enriches the wrong person. Returns ``{"ok": bool, "fields"|"message": ...}``
    like the other provider helpers; all failures are logged and swallowed.
    """
    parts = _split_name(full_name)
    if not parts["first"] or not parts["last"]:
        return {"ok": False, "message": "need first + last name to match"}

    try:
        fields = await _zoominfo_enrich_for_sourcing((full_name or "").strip(), (company or "").strip())
    except Exception as e:
        logger.warning("ZoomInfo enrich-by-name raised for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"ZoomInfo by-name failed: {e}"}

    if not _has_usable_field(fields):
        logger.info("ZoomInfo enrich-by-name returned no usable fields for %s", candidate_id)
    return {"ok": True, "fields": fields}


def contact_lookup_block_reason(source: Any, match_score: Any) -> str:
    """Why an AUTOMATED contact lookup must not spend on this candidate, or ""
    when it may. Policy (sourcing_config.CONTACT_ENRICH_*): LinkedIn and JobDiva
    candidates scoring at least CONTACT_ENRICH_MIN_SCORE; unscored rows are
    eligible only for CONTACT_ENRICH_UNSCORED_OK_PREFIXES (JobDiva). Callers: sourcing (unified_candidate_search.enrich_shown_row) and
    Launch PAIR's enrichment pass (trigger="launch" on /candidates/enrich-contact).
    """
    from core import sourcing_config as _sc

    prefixes = tuple(getattr(_sc, "CONTACT_ENRICH_SOURCE_PREFIXES", ("LinkedIn", "JobDiva")) or ())
    src = str(source or "").strip()
    if prefixes and not any(src.startswith(p) for p in prefixes):
        return f"source {src or '(none)'} is not an eligible source"
    floor = int(getattr(_sc, "CONTACT_ENRICH_MIN_SCORE", 60))
    try:
        score = float(match_score)
    except (TypeError, ValueError):
        unscored_ok = tuple(getattr(_sc, "CONTACT_ENRICH_UNSCORED_OK_PREFIXES", ("JobDiva",)) or ())
        if any(src.startswith(p) for p in unscored_ok):
            return ""
        return "candidate is unscored"
    if score < floor:
        return f"match score {score:g} is below {floor}"
    return ""


def reset_job_counter(jobdiva_id: str, *, include_lifetime: bool = False) -> None:
    """Clear the per-RUN cap counters for a job.

    Called at the start of every search so a job that filled its budget once
    isn't silently skipped for the rest of the worker's life. `_JOB_EXA_LIFETIME`
    is intentionally left alone — it is the cumulative spend ceiling on the paid
    Exa path and resetting it here would make that ceiling meaningless. Tests and
    an explicit job restart can pass include_lifetime=True.
    """
    _JOB_ENRICH_COUNTERS.pop(jobdiva_id, None)
    _JOB_EXA_COUNTERS.pop(jobdiva_id, None)
    if include_lifetime:
        _JOB_EXA_LIFETIME.pop(jobdiva_id, None)


# Sourcing-time Exa runs: exa_step's shielded tasks, kept here so the
# Step-5 stream closing never cancels a run already paid for. Strong
# references: a task only weakly referenced by the loop could vanish mid-run.
_SOURCING_EXA_TASKS: set = set()


class _SlotPermit:
    """One slot already taken from a semaphore, given back exactly once
    (``release`` also serves as a task done-callback)."""

    __slots__ = ("_sem",)

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem: Optional[asyncio.Semaphore] = sem

    def release(self, *_: Any) -> None:
        sem, self._sem = self._sem, None
        if sem is not None:
            sem.release()


def _refund_exa_slots(job_key: str) -> None:
    """Give back the per-run and lifetime Exa cap slots of a sourcing lookup
    that sent nothing. Synchronous, so safe in a cancel handler (the counters
    are only ever changed without an await in between)."""
    for counters in (_JOB_EXA_COUNTERS, _JOB_EXA_LIFETIME):
        if counters.get(job_key, 0) > 0:
            counters[job_key] -= 1


def _cached_contact_answer(cached: Dict[str, Any], wanted: Any) -> Optional[Dict[str, Any]]:
    """The sourcing answer for contact the cache holds for any of ``wanted``
    (provider_used "cache"), or None."""
    hit = {f: cached[f] for f in wanted if cached.get(f)}
    if not hit:
        return None
    email = hit.get("email", "")
    personal = bool(email) and email.rsplit("@", 1)[-1].lower() in _PERSONAL_EMAIL_DOMAINS
    return {
        "workEmail": "" if personal else email,
        "personalEmail": email if personal else "",
        "mobilePhone": hit.get("phone", ""),
        "workPhone": "",
        "provider_used": "cache",
    }


async def enrich_contact_for_sourcing(
    linkedin_url: str,
    jobdiva_id: Optional[str] = None,
    full_name: Optional[str] = None,
    include_exa: bool = False,
    company: str = "",
    seed_email: str = "",
    seed_phone: str = "",
    want_phone: bool = True,
) -> Dict[str, Any]:
    """First-hit-wins sourcing-time enrichment.

    Provider order: Kipplo-by-URL -> ZoomInfo-by-name -> Apollo-by-URL ->
    ZoomInfo-by-email -> Exa Agent (paid, last), each only when listed in
    sourcing_config.CONTACT_LOOKUP_PROVIDERS (default since 2026-09-29: Apollo
    and Exa only). No Exa deep search here: sourcing buys emails for rows the
    recruiter has not picked yet; the deep search is Launch PAIR's.

    Returns {workEmail, personalEmail, mobilePhone, workPhone, provider_used}
    on success, or `{}` when:
      - the kill switch CONTACT_ENRICHMENT_INLINE_ENABLED is "false"
      - the per-job cap has been reached
      - linkedin_url is not a LinkedIn profile URL
      - every attempted provider returned no usable fields
      - a provider call raised (logged at WARN, swallowed)

    Args:
        linkedin_url: candidate's LinkedIn profile URL. Used for the Apollo
            fallback (Apollo accepts a LinkedIn URL directly).
        jobdiva_id: job context, used as the key for the per-job cap counter.
        full_name: candidate's name. Required for the ZoomInfo path because
            the new Data API doesn't accept `linkedinUrl` as a match input,
            so we need firstName + lastName for ContactSearch. If empty, the
            ZoomInfo step is skipped and we go straight to Apollo.
        include_exa: when True, fall through to an Exa Agent contact run
            after ZoomInfo + Apollo both miss. LinkedIn-sourced candidates
            need this: ZoomInfo can't match by URL and Apollo credits run
            dry, so URL-only profiles otherwise stream in contactless.
            Gated by EXA_SOURCING_CONTACT_FALLBACK + EXA_CONTACT_ENRICH_ENABLED
            and capped per job at EXA_SOURCING_CONTACT_CAP. False for a search
            that defers paid contact to Launch PAIR (Source & Launch's full
            run, a launch session's S1: SearchCriteria.defer_paid_contact):
            the cheap providers still run, and with Exa never asked no miss
            is recorded, so Launch asks the normal run for both fields.
        company: candidate's current company, sharpens the Exa Agent query.
        seed_email / seed_phone: contact the candidate ALREADY has. Two uses:
            they seed the ZoomInfo match-by-email lookup, and they decide
            whether the paid Exa fallback is warranted at all — see
            EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT. Exa should buy a
            candidate we otherwise cannot reach, not top up one we can.
        want_phone: whether a PHONE is worth spending on. False at sourcing
            time, where an email alone makes a candidate launchable (the PAIR
            gate is phone OR email) and displayable on Step 5. Phone numbers
            are the expensive half of every provider — Apollo gates them behind
            a per-record reveal and Exa charges per run — so hunting one for a
            candidate the recruiter may never shortlist is speculative spend.
            Deferred to the moments where there is real intent: Launch PAIR, and
            the per-candidate phone button on Step 5. When False the
            phone-specific steps are skipped and an email in hand ends the chain.
    """
    from core import sourcing_config as _sc_cfg

    if os.getenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true").strip().lower() != "true":
        return {}

    # Normalise the seeds BEFORE they are allowed to influence anything. JobDiva
    # hands out synthetic placeholders (`Auto_*@jobdiva.com`, "Available upon
    # request") and unusable phone stubs; counting one as real contact would both
    # seed a pointless ZoomInfo-by-email lookup and — worse — convince the gate
    # below that an unreachable candidate is reachable, silently denying them the
    # Exa fallback they actually need.
    try:
        from utils.email_utils import is_placeholder_email as _is_placeholder
    except Exception:  # pragma: no cover - defensive
        def _is_placeholder(_email: str) -> bool:
            return False
    seed_email = (seed_email or "").strip()
    if seed_email and (_is_placeholder(seed_email) or "@" not in seed_email):
        seed_email = ""
    seed_phone = (seed_phone or "").strip()
    if sum(1 for ch in _normalise_phone(seed_phone) if ch.isdigit()) < 7:
        seed_phone = ""

    # Nothing left worth buying: the candidate already has the only field this
    # call is allowed to spend on. Returning here — before the per-job counter is
    # consumed, before any provider is touched — is what keeps sourcing free of
    # speculative phone lookups.
    if not want_phone and seed_email:
        return {}

    linkedin_url = (linkedin_url or "").strip()
    if not linkedin_url or not _LINKEDIN_PROFILE_RE.search(linkedin_url):
        return {}

    # Person-level cache (services/contact_cache.py): a contact bought on any
    # earlier job or search is reused, and a lookup that came back empty is not
    # re-bought for CONTACT_CACHE_MISS_TTL_DAYS.
    from services import contact_cache

    cached = await contact_cache.get(linkedin_url)
    wanted = [f for f, have in (("email", seed_email), ("phone", seed_phone))
              if not have and (want_phone or f != "phone")]
    cached_hit = _cached_contact_answer(cached, wanted)
    if cached_hit:
        return cached_hit
    if wanted and all(cached.get(f"{f}_missed") for f in wanted):
        logger.info(
            "contact_enrichment: %s already looked up with no %s — not buying it again",
            (full_name or linkedin_url), "/".join(wanted),
        )
        return {}

    # What the paid Exa step was asked and whether it answered: only a clean
    # answer without a field counts as a cacheable miss.
    trace: Dict[str, Any] = {}

    async def _providers() -> Dict[str, Any]:
        job_key = (jobdiva_id or "sourcing").strip() or "sourcing"

        async with _JOB_ENRICH_LOCK:
            used = _JOB_ENRICH_COUNTERS.get(job_key, 0)
            if used >= PER_JOB_CAP:
                if used == PER_JOB_CAP:
                    logger.info("contact_enrichment: per-job cap (%d) reached for %s", PER_JOB_CAP, job_key)
                    _JOB_ENRICH_COUNTERS[job_key] = used + 1  # bump once so we don't re-log every call
                return {}
            _JOB_ENRICH_COUNTERS[job_key] = used + 1

        # Kipplo first (services/kipplo.py): matches the LinkedIn URL itself and
        # bills only the field groups it finds, so it is asked for exactly the
        # fields this call may buy (no phone at sourcing). Outside the provider
        # semaphore: waiting for a Kipplo rate-limit slot must not hold up the
        # ZoomInfo/Apollo lookups of other rows. Any Kipplo failure (rate limit,
        # credits, key, network) falls through to the rest of the chain, which
        # ends at Exa.
        kipplo_fields = tuple(f for f in wanted if f in kipplo_supported_fields())
        if kipplo_fields and lookup_provider_enabled("kipplo"):
            kipplo_label = (full_name or "").strip() or linkedin_url
            try:
                kipplo_result = await kipplo_enrich_by_linkedin(kipplo_label, linkedin_url, fields=kipplo_fields)
            except Exception as e:
                logger.warning("contact_enrichment Kipplo path raised for %s: %s", kipplo_label, e)
                kipplo_result = {"ok": False}
            if kipplo_result.get("ok") and _has_usable_field(kipplo_result.get("fields") or {}):
                fields = kipplo_result["fields"]
                logger.info("contact_enrichment: kipplo hit for %s", job_key)
                return {
                    "workEmail": fields.get("workEmail", ""),
                    "personalEmail": fields.get("personalEmail", ""),
                    "mobilePhone": fields.get("mobilePhone", ""),
                    "workPhone": fields.get("workPhone", ""),
                    "provider_used": "kipplo",
                }

        async with _PROVIDER_SEMAPHORE:
            # ZoomInfo requires a name (new Data API doesn't accept linkedinUrl as
            # a match input). If we don't have one, skip straight to Apollo.
            zi_fields: Dict[str, str] = {}
            if (full_name or "").strip() and lookup_provider_enabled("zoominfo"):
                try:
                    zi_fields = await _zoominfo_enrich_for_sourcing(full_name.strip(), (company or "").strip())
                except Exception as e:
                    logger.warning("contact_enrichment ZoomInfo path raised for %s: %s", job_key, e)
                    zi_fields = {}

            if _has_usable_field(zi_fields):
                logger.info("contact_enrichment: zoominfo hit for %s", job_key)
                return {
                    "workEmail": zi_fields.get("workEmail", ""),
                    "personalEmail": zi_fields.get("personalEmail", ""),
                    "mobilePhone": zi_fields.get("mobilePhone", ""),
                    "workPhone": zi_fields.get("workPhone", ""),
                    "provider_used": "zoominfo",
                }

            apollo_result: Dict[str, Any] = {"ok": False}
            if lookup_provider_enabled("apollo"):
                try:
                    apollo_result = await apollo_enrich_by_linkedin(job_key, linkedin_url)
                except Exception as e:
                    logger.warning("contact_enrichment Apollo path raised for %s: %s", job_key, e)
                    apollo_result = {"ok": False}

            if apollo_result.get("ok") and _has_usable_field(apollo_result.get("fields") or {}):
                fields = apollo_result["fields"]
                logger.info("contact_enrichment: apollo hit for %s", job_key)
                return {
                    "workEmail": fields.get("workEmail", ""),
                    "personalEmail": fields.get("personalEmail", ""),
                    "mobilePhone": fields.get("mobilePhone", ""),
                    "workPhone": fields.get("workPhone", ""),
                    "provider_used": "apollo",
                }

            # ZoomInfo match-by-EMAIL. Runs last among the cheap providers because it
            # needs a seed email, but it is the RIGHT tool for the commonest gap: a
            # candidate who has an email and is missing only a phone. ZoomInfo can't
            # match a LinkedIn URL, but it can match an email — so this fills the
            # exact case that used to fall through to a paid Exa run. The on-demand
            # path has always done this; the sourcing path was missing the step.
            # Only reachable when want_phone is True: with an email already in hand
            # and no phone wanted, the call returned above. So this step exists purely
            # to convert an email into a PHONE, which is why it is phone-gated.
            seed_email_clean = seed_email if want_phone else ""
            if (
                seed_email_clean
                and getattr(_sc_cfg, "ZOOMINFO_SOURCING_EMAIL_LOOKUP", True)
                and lookup_provider_enabled("zoominfo")
            ):
                try:
                    zi_email = await zoominfo_enrich_by_email(job_key, seed_email_clean)
                except Exception as e:
                    logger.warning(
                        "contact_enrichment ZoomInfo-by-email raised for %s: %s", job_key, e
                    )
                    zi_email = {"ok": False}
                if zi_email.get("ok") and _has_usable_field(zi_email.get("fields") or {}):
                    fields = zi_email["fields"]
                    logger.info("contact_enrichment: zoominfo-by-email hit for %s", job_key)
                    return {
                        "workEmail": fields.get("workEmail", ""),
                        "personalEmail": fields.get("personalEmail", ""),
                        "mobilePhone": fields.get("mobilePhone", ""),
                        "workPhone": fields.get("workPhone", ""),
                        "provider_used": "zoominfo_email",
                    }

        # Exa Agent fallback — outside the provider semaphore (it has its own
        # slow polling loop and per-job budget; holding a ZoomInfo/Apollo slot
        # for up to EXA_CONTACT_ENRICH_TIMEOUT_S would starve the cheap chain).
        if include_exa and lookup_provider_enabled("exa"):
            _sc = _sc_cfg

            if not getattr(_sc, "EXA_SOURCING_CONTACT_FALLBACK", True):
                return {}

            # Deprioritised: Exa only buys candidates we cannot otherwise reach.
            # A candidate who already has an email or a phone is contactable, so
            # spending ~$0.115 to complete the set is not worth it at sourcing time —
            # the cheap providers above (including the new ZoomInfo-by-email step)
            # get first refusal, and recruiter-initiated on-demand enrichment can
            # still reach for Exa because that is a deliberate click.
            if getattr(_sc, "EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT", True):
                if seed_email or seed_phone:
                    logger.info(
                        "contact_enrichment: skipping paid Exa for %s — candidate is "
                        "already reachable (email=%s phone=%s); cheap providers missed "
                        "only the remaining field",
                        job_key, bool(seed_email), bool(seed_phone),
                    )
                    return {}
            # Out of credits: skip BEFORE the per-job counters, which would
            # otherwise be used up by free 402s and starve the job after the
            # top-up. Nothing asked, so no miss is recorded either. A lookup
            # already counted and queued on the Exa semaphore when the
            # breaker trips is skipped inside exa_enrich_by_linkedin and gives
            # its slots back below.
            from core.vendor_limiter import EXA as _exa_limit

            if await _exa_limit.no_credits_left() > 0:
                exa_telemetry.record_exa_call("contact", "agent_run", "breaker_open")
                return {}
            exa_cap = max(0, int(getattr(_sc, "EXA_SOURCING_CONTACT_CAP", 25) or 0))
            exa_lifetime_cap = max(
                0, int(getattr(_sc, "EXA_SOURCING_CONTACT_LIFETIME_CAP", 100) or 0)
            )
            async with _JOB_ENRICH_LOCK:
                # Lifetime ceiling first — this one is not reset between runs, so it
                # is what actually bounds spend on a job the recruiter re-searches.
                exa_total = _JOB_EXA_LIFETIME.get(job_key, 0)
                if exa_total >= exa_lifetime_cap:
                    if exa_total == exa_lifetime_cap:
                        logger.warning(
                            "contact_enrichment: LIFETIME Exa cap (%d runs, ~$%.2f) reached "
                            "for %s — no further sourcing-time Exa lookups for this job; "
                            "on-demand enrichment still works",
                            exa_lifetime_cap, exa_lifetime_cap * 0.115, job_key,
                        )
                        _JOB_EXA_LIFETIME[job_key] = exa_total + 1
                    return {}
                exa_used = _JOB_EXA_COUNTERS.get(job_key, 0)
                if exa_used >= exa_cap:
                    if exa_used == exa_cap:
                        logger.info(
                            "contact_enrichment: per-run Exa cap (%d) reached for %s "
                            "(%d/%d lifetime)",
                            exa_cap, job_key, exa_total, exa_lifetime_cap,
                        )
                        _JOB_EXA_COUNTERS[job_key] = exa_used + 1
                    return {}
                _JOB_EXA_COUNTERS[job_key] = exa_used + 1
                _JOB_EXA_LIFETIME[job_key] = exa_total + 1

            # Label the run by the CANDIDATE, not the job. `exa_enrich_by_linkedin`
            # uses its first arg purely for logging, and passing job_key made every
            # line for a job identical — useless for answering "which candidate did
            # Exa resolve, and which timed out?".
            exa_label = (full_name or "").strip() or linkedin_url
            # Only what the candidate lacks — Exa bills per field it fills.
            # want_phone=False (every sourcing call) means an email is all we buy:
            # the phone is the dear half of an Exa run ($0.07 of ~$0.095 measured
            # 2026-09-28) and was being bought for every contactless row anyway.
            exa_fields = tuple(
                field for field, have in (("email", seed_email), ("phone", seed_phone))
                if not have and (want_phone or field != "phone")
            )
            # exa_step (single-flight per person across the workers): the
            # Exa slot is waited for where the caller's cancel still wins (a
            # lookup the closed Step-5 stream cancels while queued buys
            # nothing), the cache is looked at again with the slot in hand (a
            # lookup of the same person queued ahead may have bought it), and
            # the run itself is shielded and caches its own answer, so the
            # stream closing never loses a paid answer. Whenever this lookup
            # buys nothing itself (cancelled while queued, answered by the
            # cache or by another lookup's run, the out-of-credits breaker
            # opened while it waited), its cap slots are given back.
            exa_result = await exa_step(
                exa_label, linkedin_url, full_name or "", company or "", fields=exa_fields,
                semaphore=_exa_semaphore(), cached=cached, tasks=_SOURCING_EXA_TASKS,
                on_unused=lambda: _refund_exa_slots(job_key),
            )
            if exa_result.get("from_cache"):
                return _cached_contact_answer(
                    {f: v for f, v in zip(("email", "phone"), _asked_contact(
                        exa_fields, exa_result.get("fields") or {}))}, exa_fields,
                ) or {}
            if exa_result.get("skipped") or (exa_result.get("in_progress") and not exa_result.get("ok")):
                return {}   # nothing was sent (or not ours): no miss is recorded

            # A field another lookup's run is still after is not a miss.
            trace["exa_asked"] = tuple(
                f for f in exa_fields if f not in (exa_result.get("in_progress_fields") or ())
            )
            trace["exa_answered"] = bool(exa_result.get("ok"))
            if exa_result.get("ok") and _has_usable_field(exa_result.get("fields") or {}):
                fields = exa_result["fields"]
                logger.info(
                    "contact_enrichment: exa hit for %s (job %s, %d/%d lifetime)",
                    exa_label, job_key, _JOB_EXA_LIFETIME.get(job_key, 0), exa_lifetime_cap,
                )
                return {
                    "workEmail": fields.get("workEmail", ""),
                    "personalEmail": fields.get("personalEmail", ""),
                    "mobilePhone": fields.get("mobilePhone", ""),
                    "workPhone": fields.get("workPhone", ""),
                    "provider_used": "exa",
                }

        return {}

    with exa_telemetry.scope(path="sourcing"):
        result = await _providers()
    provider = (result or {}).get("provider_used", "")
    if provider == "cache":
        return result  # found in the cache once an Exa slot came up: nothing new
    await contact_cache.record(
        linkedin_url,
        email=(result.get("workEmail") or result.get("personalEmail") or "") if result else "",
        # Personal numbers only: the cache marks every phone it stores as personal.
        phone=(result.get("mobilePhone") or "") if result else "",
        email_provider=provider,
        phone_provider=provider,
        missed=trace.get("exa_asked", ()) if trace.get("exa_answered") else (),
    )
    return result
