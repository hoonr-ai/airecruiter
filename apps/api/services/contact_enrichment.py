"""Contact enrichment via ZoomInfo + Apollo.

Two surfaces:

1. Provider helpers (`extract_zoominfo_contact_fields`, `extract_apollo_contact_fields`,
   `apollo_enrich_by_linkedin`) — moved here from `routers/candidates.py` so both
   the on-demand enrichment endpoint and the in-line sourcing path share the
   same parsers and HTTP shapes. The on-demand path still owns the
   ZoomInfo legacy → new-OAuth 401 fallback + cross-fill + name-search chain;
   none of that is reused here.

2. `enrich_contact_for_sourcing(linkedin_url, jobdiva_id)` — first-hit-wins
   helper for the sourcing pipeline. ZoomInfo legacy enrich first; on miss,
   Apollo by LinkedIn URL. Capped at `PER_JOB_CAP` enrichments per job to
   bound provider cost. Failures are logged and swallowed — sourcing must
   not fail on enrichment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from core.config import (
    APOLLO_API_KEY as _APOLLO_ENV_KEY,
    ZOOMINFO_BEARER_TOKEN,
    ZOOMINFO_CLIENT_ID,
    ZOOMINFO_ENRICH_URL,
)

logger = logging.getLogger(__name__)

APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/enrich"

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

    work_email = _first_non_empty(person.get("email"), person.get("work_email"))

    personal_email = ""
    personal_emails = person.get("personal_emails")
    if isinstance(personal_emails, list):
        for item in personal_emails:
            candidate = _first_non_empty(
                item.get("email") if isinstance(item, dict) else None,
                item,
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

    person_organization = person.get("organization")
    if isinstance(person_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_phone_candidate(person_organization.get(key))

    payload_organization = payload.get("organization") if isinstance(payload, dict) else None
    if isinstance(payload_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_phone_candidate(payload_organization.get(key))

    if not mobile_phone:
        mobile_phone = _extract_phone_value(payload.get("mobile_phone"))
    if not work_phone:
        work_phone = _extract_phone_value(payload.get("phone"))

    _add_phone_candidate(payload.get("mobile_phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone_number") if isinstance(payload, dict) else "")

    if not mobile_phone and phone_candidates:
        mobile_phone = phone_candidates[0]
    if not work_phone and len(phone_candidates) > 1:
        work_phone = phone_candidates[1]
    elif not work_phone and phone_candidates:
        work_phone = phone_candidates[0]

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


async def _zoominfo_enrich_by_linkedin(linkedin_url: str) -> Dict[str, str]:
    """Call the ZoomInfo legacy enrich endpoint by LinkedIn URL.

    Returns the canonical four-field dict (possibly all empty). Empty dict on
    HTTP / parse failure. Does NOT do the 401 → new-OAuth fallback that the
    on-demand path does — sourcing-time enrichment intentionally trades that
    extra latency for simplicity and bounded HTTP work.
    """
    if not ZOOMINFO_BEARER_TOKEN:
        return {}

    headers = {
        "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    if ZOOMINFO_CLIENT_ID:
        headers["X-Client-Id"] = ZOOMINFO_CLIENT_ID

    payload = {
        "inputFields": [
            {
                "fieldName": "linkedinUrl",
                "fieldType": "String",
                "value": linkedin_url,
            }
        ],
        "outputFields": ["workPhone", "mobilePhone", "workEmail", "personalEmail"],
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            res = await client.post(ZOOMINFO_ENRICH_URL, headers=headers, json=payload)
    except Exception as e:
        logger.warning("ZoomInfo request failed: %s", e)
        return {}

    if res.status_code >= 400:
        # 401 here means the legacy endpoint rejected the token; the on-demand
        # path retries with the new OAuth Data API, but sourcing skips that
        # chain (covered by the Apollo fallback in the caller).
        logger.info("ZoomInfo non-2xx: %s", res.status_code)
        return {}

    try:
        zoominfo_data = res.json()
    except Exception:
        return {}

    return _extract_enrichment_fields_legacy(zoominfo_data)


def _has_usable_field(fields: Dict[str, Any]) -> bool:
    return any(str(fields.get(k) or "").strip() for k in ("mobilePhone", "workPhone", "workEmail", "personalEmail"))


def reset_job_counter(jobdiva_id: str) -> None:
    """Tests / explicit job-restart can clear the per-job cap counter."""
    _JOB_ENRICH_COUNTERS.pop(jobdiva_id, None)


async def enrich_contact_for_sourcing(
    linkedin_url: str, jobdiva_id: Optional[str] = None
) -> Dict[str, Any]:
    """First-hit-wins sourcing-time enrichment.

    Returns {workEmail, personalEmail, mobilePhone, workPhone, provider_used}
    on success, or `{}` when:
      - the kill switch CONTACT_ENRICHMENT_INLINE_ENABLED is "false"
      - the per-job cap has been reached
      - linkedin_url is not a LinkedIn profile URL
      - both ZoomInfo and Apollo returned no usable fields
      - either provider call raised (logged at WARN, swallowed)
    """
    if os.getenv("CONTACT_ENRICHMENT_INLINE_ENABLED", "true").strip().lower() != "true":
        return {}

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
        try:
            zi_fields = await _zoominfo_enrich_by_linkedin(linkedin_url)
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

    return {}
