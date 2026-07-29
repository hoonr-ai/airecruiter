"""Contact enrichment via ZoomInfo + Apollo.

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
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import (
    APOLLO_API_KEY as _APOLLO_ENV_KEY,
    EXA_API_KEY,
    EXA_CONTACT_ENRICH_EFFORT,
    EXA_CONTACT_ENRICH_ENABLED,
    EXA_CONTACT_ENRICH_TIMEOUT_S,
)
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
                        "The person's best direct phone number "
                        "(mobile preferred), including country code."
                    ),
                },
            },
        }
    },
}
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

# Exa enforces ~1/5-of-QPS concurrency on Agent runs (≥3 simultaneous runs
# start 429ing on the default account). The sourcing fallback runs outside
# _PROVIDER_SEMAPHORE (its slow polling would starve the cheap chain), so it
# gets its own bound, sized from EXA_AGENT_CONCURRENCY.
def _exa_semaphore() -> asyncio.Semaphore:
    global _EXA_SEMAPHORE
    if _EXA_SEMAPHORE is None:
        try:
            from core import sourcing_config as _sc
            limit = max(1, int(getattr(_sc, "EXA_AGENT_CONCURRENCY", 1) or 1))
        except Exception:
            limit = 1
        _EXA_SEMAPHORE = asyncio.Semaphore(limit)
    return _EXA_SEMAPHORE

_EXA_SEMAPHORE: Optional[asyncio.Semaphore] = None

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

    mobile_phone = _first_non_empty(
        person.get("mobile_phone"),
        person.get("mobile"),
        person.get("cell_phone"),
        person.get("cell"),
    )
    work_phone = _first_non_empty(
        person.get("sanitized_phone"),
        person.get("work_phone"),
        person.get("organization_phone"),
        person.get("direct_phone"),
        person.get("office_phone"),
        person.get("home_phone"),
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
            if not mobile_phone and ptype in {"mobile", "cell", "cellphone"}:
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

    # Promote only person-level numbers into the candidate's phone slots.
    personal_candidates = [p for p in phone_candidates if p not in org_phone_keys]
    if not mobile_phone and personal_candidates:
        mobile_phone = personal_candidates[0]
    if not work_phone and len(personal_candidates) > 1:
        work_phone = personal_candidates[1]
    elif not work_phone and personal_candidates:
        work_phone = personal_candidates[0]

    return {
        "mobilePhone": mobile_phone,
        "workPhone": work_phone,
        "workEmail": work_email,
        "personalEmail": personal_email,
        "phoneCandidates": phone_candidates,
    }


async def apollo_enrich_by_linkedin(candidate_id: str, linkedin_url: str) -> Dict[str, Any]:
    """Call Apollo's people/enrich by LinkedIn URL. Pure async, no DB writes.

    Returns {"ok": bool, "fields"|"message": ...}.
    """
    if not APOLLO_API_KEY or APOLLO_API_KEY == "PASTE_APOLLO_API_KEY_HERE":
        logger.warning("Apollo enrichment skipped for %s: API key not configured", candidate_id)
        return {"ok": False, "message": "Apollo API key not configured"}

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
    return {"ok": True, "fields": extracted}


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


def _build_exa_contact_query(full_name: str, company: str, linkedin_url: str) -> str:
    """Natural-language query for the Exa Agent contact-enrichment run."""
    who = (full_name or "").strip() or "this person"
    parts = [f"Find the work email and phone number for {who}"]
    company = (company or "").strip()
    if company:
        parts.append(f"at {company}")
    url = (linkedin_url or "").strip()
    if url:
        parts.append(f". LinkedIn: {url}")
    return " ".join(parts).replace(" .", ".")


async def exa_enrich_by_linkedin(
    candidate_id: str,
    linkedin_url: str,
    full_name: str = "",
    company: str = "",
) -> Dict[str, Any]:
    """Enrich one person via the Exa Agent API (by LinkedIn URL). Pure async.

    Returns ``{"ok": bool, "fields"|"message": ...}`` mirroring
    ``apollo_enrich_by_linkedin``. No-op (``ok=False``) when
    ``EXA_CONTACT_ENRICH_ENABLED`` is off or ``EXA_API_KEY`` is missing. Bounded
    by ``EXA_CONTACT_ENRICH_TIMEOUT_S``; all failures logged and swallowed.
    """
    if not EXA_CONTACT_ENRICH_ENABLED:
        return {"ok": False, "message": "Exa contact enrichment disabled"}
    if not EXA_API_KEY:
        logger.warning("Exa enrichment skipped for %s: EXA_API_KEY not configured", candidate_id)
        return {"ok": False, "message": "EXA_API_KEY not configured"}

    query = _build_exa_contact_query(full_name, company, linkedin_url)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": EXA_API_KEY,
        "Exa-Beta": EXA_AGENT_BETA,
    }
    body = {
        "query": query,
        "outputSchema": _EXA_CONTACT_SCHEMA,
        "effort": EXA_CONTACT_ENRICH_EFFORT,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(EXA_AGENT_RUNS_URL, headers=headers, json=body)
            if r.status_code >= 400:
                logger.warning(
                    "Exa agent create non-2xx for %s: %s %s",
                    candidate_id, r.status_code, r.text[:200],
                )
                return {"ok": False, "message": f"Exa create error ({r.status_code})"}

            run = r.json()
            run_id = run.get("id")
            status = run.get("status")
            final = run if status in _EXA_TERMINAL_STATES else None

            loop = asyncio.get_event_loop()
            deadline = loop.time() + max(1, EXA_CONTACT_ENRICH_TIMEOUT_S)
            while final is None and loop.time() < deadline:
                await asyncio.sleep(_EXA_POLL_INTERVAL_S)
                g = await client.get(f"{EXA_AGENT_RUNS_URL}/{run_id}", headers=headers)
                if g.status_code >= 400:
                    logger.warning("Exa agent poll non-2xx for %s: %s", candidate_id, g.status_code)
                    return {"ok": False, "message": f"Exa poll error ({g.status_code})"}
                run = g.json()
                status = run.get("status")
                if status in _EXA_TERMINAL_STATES:
                    final = run
    except Exception as e:
        logger.warning("Exa agent request failed for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"Exa request failed: {e}"}

    if final is None:
        logger.info("Exa agent run timed out for %s (>%ss)", candidate_id, EXA_CONTACT_ENRICH_TIMEOUT_S)
        return {"ok": False, "message": "Exa run timed out"}
    if status != "completed":
        logger.info("Exa agent run %s for %s: %s", status, candidate_id, final.get("stopReason"))
        return {"ok": False, "message": f"Exa run {status}"}

    output = final.get("output") or {}
    extracted = extract_exa_contact_fields(output.get("structured"))
    if not _has_usable_field(extracted):
        logger.info("Exa returned no usable contact fields for %s", candidate_id)
    return {"ok": True, "fields": extracted}


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


async def _zoominfo_resolve_person_id(full_name: str) -> Optional[str]:
    """ContactSearch by firstName + lastName, return best-match personId.

    ZoomInfo doesn't accept `linkedinUrl` as a search filter (tested:
    `PFAPI0005 / Invalid field requested`). Name-based search is the only
    sourcing-time entry point. Returns None if we don't have both names, if
    search returns no hits, or if the top hit's accuracy score is below
    ``_MIN_CONTACT_ACCURACY_SCORE``.
    """
    parts = _split_name(full_name)
    if not parts["first"] or not parts["last"]:
        return None

    body = {
        "data": {
            "type": "ContactSearch",
            "attributes": {
                "firstName": parts["first"],
                "lastName": parts["last"],
            },
        }
    }
    res = await _zoominfo_authed_post(ZOOMINFO_NEW_SEARCH_URL, body)
    if res is None or res.status_code >= 400:
        if res is not None:
            logger.info(
                "zoominfo ContactSearch non-2xx for %s: %s",
                full_name,
                res.status_code,
            )
        return None

    try:
        body_json = res.json()
    except ValueError:
        return None

    data = body_json.get("data") if isinstance(body_json, dict) else None
    if not isinstance(data, list) or not data:
        return None

    top = data[0]
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


async def _zoominfo_enrich_for_sourcing(full_name: str) -> Dict[str, str]:
    """Two-call ZoomInfo lookup for the sourcing pipeline.

    Returns the canonical four-field dict (possibly all empty). Empty dict on
    any miss — caller falls through to Apollo.
    """
    person_id = await _zoominfo_resolve_person_id(full_name)
    if not person_id:
        return {}
    return await _zoominfo_enrich_by_person_id(person_id)


def _has_usable_field(fields: Dict[str, Any]) -> bool:
    return any(str(fields.get(k) or "").strip() for k in ("mobilePhone", "workPhone", "workEmail", "personalEmail"))


async def zoominfo_enrich_by_name(candidate_id: str, full_name: str) -> Dict[str, Any]:
    """Enrich a contact via the ZoomInfo OAuth Data API, matching by NAME.

    ZoomInfo cannot match by LinkedIn URL, and URL-only candidates (e.g.
    Exa-sourced) arrive with no seed email, so neither the by-URL nor the
    by-email path can reach them. Name-based ContactSearch -> personId ->
    ContactEnrich is the only ZoomInfo entry point for these. Requires both a
    first and last name and is accuracy-gated inside ``_zoominfo_resolve_person_id``
    (``_MIN_CONTACT_ACCURACY_SCORE``) so a name collision never enriches the
    wrong person. Returns ``{"ok": bool, "fields"|"message": ...}`` like the
    other provider helpers; all failures are logged and swallowed.
    """
    parts = _split_name(full_name)
    if not parts["first"] or not parts["last"]:
        return {"ok": False, "message": "need first + last name to match"}

    try:
        fields = await _zoominfo_enrich_for_sourcing((full_name or "").strip())
    except Exception as e:
        logger.warning("ZoomInfo enrich-by-name raised for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"ZoomInfo by-name failed: {e}"}

    if not _has_usable_field(fields):
        logger.info("ZoomInfo enrich-by-name returned no usable fields for %s", candidate_id)
    return {"ok": True, "fields": fields}


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


async def enrich_contact_for_sourcing(
    linkedin_url: str,
    jobdiva_id: Optional[str] = None,
    full_name: Optional[str] = None,
    include_exa: bool = False,
    company: str = "",
    seed_email: str = "",
    seed_phone: str = "",
) -> Dict[str, Any]:
    """First-hit-wins sourcing-time enrichment.

    Provider order is cheapest-useful-first, with the paid provider last:
    ZoomInfo-by-name -> Apollo-by-URL -> ZoomInfo-by-email -> Exa Agent.

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
            and capped per job at EXA_SOURCING_CONTACT_CAP.
        company: candidate's current company, sharpens the Exa Agent query.
        seed_email / seed_phone: contact the candidate ALREADY has. Two uses:
            they seed the ZoomInfo match-by-email lookup, and they decide
            whether the paid Exa fallback is warranted at all — see
            EXA_SOURCING_CONTACT_ONLY_WHEN_NO_CONTACT. Exa should buy a
            candidate we otherwise cannot reach, not top up one we can.
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
        from services.jobdiva import _is_placeholder_email as _is_placeholder
    except Exception:  # pragma: no cover - defensive
        def _is_placeholder(_email: str) -> bool:
            return False
    seed_email = (seed_email or "").strip()
    if seed_email and (_is_placeholder(seed_email) or "@" not in seed_email):
        seed_email = ""
    seed_phone = (seed_phone or "").strip()
    if sum(1 for ch in _normalise_phone(seed_phone) if ch.isdigit()) < 7:
        seed_phone = ""

    linkedin_url = (linkedin_url or "").strip()
    if not linkedin_url or not _LINKEDIN_PROFILE_RE.search(linkedin_url):
        return {}

    job_key = (jobdiva_id or "sourcing").strip() or "sourcing"

    async with _JOB_ENRICH_LOCK:
        used = _JOB_ENRICH_COUNTERS.get(job_key, 0)
        if used >= PER_JOB_CAP:
            if used == PER_JOB_CAP:
                logger.info("contact_enrichment: per-job cap (%d) reached for %s", PER_JOB_CAP, job_key)
                _JOB_ENRICH_COUNTERS[job_key] = used + 1  # bump once so we don't re-log every call
            return {}
        _JOB_ENRICH_COUNTERS[job_key] = used + 1

    async with _PROVIDER_SEMAPHORE:
        # ZoomInfo requires a name (new Data API doesn't accept linkedinUrl as
        # a match input). If we don't have one, skip straight to Apollo.
        zi_fields: Dict[str, str] = {}
        if (full_name or "").strip():
            try:
                zi_fields = await _zoominfo_enrich_for_sourcing(full_name.strip())
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
        seed_email_clean = seed_email  # already normalised / placeholder-stripped
        if seed_email_clean and getattr(_sc_cfg, "ZOOMINFO_SOURCING_EMAIL_LOOKUP", True):
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
    if include_exa:
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
        try:
            async with _exa_semaphore():
                exa_result = await exa_enrich_by_linkedin(
                    exa_label, linkedin_url, full_name or "", company or ""
                )
        except Exception as e:
            logger.warning(
                "contact_enrichment Exa path raised for %s (job %s): %s",
                exa_label, job_key, e,
            )
            exa_result = {"ok": False}

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
