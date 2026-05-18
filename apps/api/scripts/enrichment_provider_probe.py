"""Probe ZoomInfo + Apollo contact-enrichment providers for a single profile.

Reproduces the exact provider calls that ``_enrich_candidate_contact_impl``
makes in ``routers/candidates.py`` so we can see, end-to-end, what each
provider returns for a given LinkedIn URL (or persisted candidate row).

Use when the Launch PAIR flow shows "no phone/email" for a candidate and we
need to decide whether it's a provider coverage gap, an auth/key problem, or
a malformed input URL. The endpoint's own logs only emit the final summary;
this script captures every request + every full response so the failure mode
is explicit.

Inputs (one required):
    --linkedin-url <url>        Use this LinkedIn URL directly.
    --candidate-id <id>         Look up profile_url / name / jobdiva_id from
                                sourced_candidates (mirrors what the endpoint
                                does when linkedin_url is omitted).

Optional context (helps the ZoomInfo OAuth Data API path):
    --jobdiva-id <id>           Disambiguate sourced_candidates row.
    --source <s>                Same.
    --full-name "<First Last>"  Used by ContactSearch + name+company match.
    --company-name "<Acme>"     Same.

Run:
    cd apps/api
    venv/bin/python -m scripts.enrichment_provider_probe \
        --linkedin-url https://www.linkedin.com/in/example
    venv/bin/python -m scripts.enrichment_provider_probe \
        --candidate-id <id> --jobdiva-id <jid>

Writes the full evidence document (every request + every response, with
secrets redacted) to ``apps/api/tmp/enrichment_probe_<utc>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from core.config import (  # noqa: E402
    ZOOMINFO_ENRICH_URL,
    ZOOMINFO_BEARER_TOKEN,
    ZOOMINFO_CLIENT_ID,
)

ZOOMINFO_NEW_ENRICH_URL = "https://api.zoominfo.com/gtm/data/v1/contacts/enrich"
ZOOMINFO_NEW_SEARCH_URL = "https://api.zoominfo.com/gtm/data/v1/contacts/search"
APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/enrich"


def _apollo_api_key() -> str:
    """Mirror the endpoint's resolution order: env var, then legacy hardcoded value."""
    env_value = (os.getenv("APOLLO_API_KEY") or "").strip()
    if env_value:
        return env_value
    return "cB7rogHZj4XRrhnTEqTlXQ"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    redacted = dict(headers)
    for key in list(redacted.keys()):
        lk = key.lower()
        if lk == "authorization":
            redacted[key] = "Bearer <redacted>"
        elif lk in {"x-api-key", "x-client-id"}:
            redacted[key] = "<redacted>"
    return redacted


def _split_name(raw: str) -> Dict[str, str]:
    parts = [p for p in str(raw or "").strip().split() if p]
    if not parts:
        return {"first": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0], "last": ""}
    return {"first": parts[0], "last": " ".join(parts[1:])}


def _lookup_candidate(
    candidate_id: str,
    jobdiva_id: Optional[str],
    source: Optional[str],
) -> Dict[str, Any]:
    """Mirror the endpoint's sourced_candidates lookup."""
    from psycopg2.extras import RealDictCursor

    from routers._helpers import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = (
                "SELECT id, candidate_id, jobdiva_id, source, name, headline, "
                "profile_url, email, phone, data "
                "FROM sourced_candidates WHERE candidate_id = %s"
            )
            params: List[Any] = [candidate_id]
            if jobdiva_id:
                query += " AND jobdiva_id = %s"
                params.append(jobdiva_id)
            if source:
                query += " AND source = %s"
                params.append(source)
            query += " ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST"
            cur.execute(query, tuple(params))
            row = cur.fetchone()
            return dict(row) if row else {}
    finally:
        conn.close()


async def _send(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    label: str,
) -> Dict[str, Any]:
    probe_request_id = uuid.uuid4().hex
    started_monotonic = time.monotonic()
    started_at = _utc_iso()
    response: Optional[httpx.Response] = None
    error: Optional[str] = None
    try:
        response = await client.request(method, url, headers=headers, json=json_body)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    latency_ms = int((time.monotonic() - started_monotonic) * 1000)

    record: Dict[str, Any] = {
        "label": label,
        "probe_request_id": probe_request_id,
        "request_started_at_utc": started_at,
        "request_ended_at_utc": _utc_iso(),
        "latency_ms": latency_ms,
        "method": method,
        "url": url,
        "request_headers": _redact_headers(headers),
        "request_body": json_body,
    }
    if error is not None:
        record["error"] = error
        record["response_status"] = None
        return record

    assert response is not None
    record["response_status"] = response.status_code
    record["response_headers"] = dict(response.headers)
    body_text = response.text or ""
    record["response_body_text"] = body_text
    try:
        record["response_body_json"] = response.json() if body_text else None
    except Exception:
        record["response_body_json"] = None
    return record


def _summarise_zoominfo_legacy(record: Dict[str, Any]) -> Dict[str, Any]:
    """Walk the legacy enrich response to capture target fields."""
    body = record.get("response_body_json")
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

    walk(body)
    return found


def _summarise_zoominfo_new(record: Dict[str, Any]) -> Dict[str, Any]:
    body = record.get("response_body_json") or {}
    data = body.get("data") if isinstance(body, dict) else []
    first = data[0] if isinstance(data, list) and data else {}
    attrs = first.get("attributes") if isinstance(first, dict) else {}
    if not isinstance(attrs, dict):
        attrs = {}
    return {
        "mobilePhone": str(attrs.get("mobilePhone") or attrs.get("mobilePhoneAlt") or "").strip(),
        "workPhone": str(attrs.get("phone") or attrs.get("directPhone") or attrs.get("directPhoneAlt") or "").strip(),
        "workEmail": str(attrs.get("email") or "").strip(),
        "personalEmail": "",
    }


def _summarise_apollo(record: Dict[str, Any]) -> Dict[str, Any]:
    body = record.get("response_body_json") or {}
    person = body.get("person") if isinstance(body, dict) else {}
    if not isinstance(person, dict):
        person = {}
    work_email = str(person.get("email") or person.get("work_email") or "").strip()
    personal_emails = person.get("personal_emails")
    personal_email = ""
    if isinstance(personal_emails, list):
        for item in personal_emails:
            value = item.get("email") if isinstance(item, dict) else item
            if isinstance(value, str) and value.strip():
                personal_email = value.strip()
                break
    return {
        "mobilePhone": str(person.get("mobile_phone") or person.get("mobile") or "").strip(),
        "workPhone": str(person.get("sanitized_phone") or person.get("work_phone") or person.get("phone") or "").strip(),
        "workEmail": work_email,
        "personalEmail": personal_email,
        "person_present": bool(person),
    }


async def probe_zoominfo_legacy(
    client: httpx.AsyncClient, linkedin_url: str
) -> Dict[str, Any]:
    if not ZOOMINFO_BEARER_TOKEN:
        return {
            "label": "zoominfo_legacy",
            "skipped": True,
            "skip_reason": "ZOOMINFO_BEARER_TOKEN not set",
        }
    headers = {
        "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    if ZOOMINFO_CLIENT_ID:
        headers["X-Client-Id"] = ZOOMINFO_CLIENT_ID
    payload = {
        "inputFields": [
            {"fieldName": "linkedinUrl", "fieldType": "String", "value": linkedin_url}
        ],
        "outputFields": ["workPhone", "mobilePhone", "workEmail", "personalEmail"],
    }
    record = await _send(
        client,
        "POST",
        ZOOMINFO_ENRICH_URL,
        headers=headers,
        json_body=payload,
        label="zoominfo_legacy",
    )
    record["extracted_fields"] = _summarise_zoominfo_legacy(record)
    return record


async def probe_zoominfo_new(
    client: httpx.AsyncClient,
    *,
    full_name: str,
    company_name: str,
    email: str,
    phone: str,
) -> Dict[str, Any]:
    """Mirror the endpoint's 401-fallback path: build a matchPersonInput.

    Order: email > phone > firstName+lastName+companyName > ContactSearch by name.
    """
    if not ZOOMINFO_BEARER_TOKEN:
        return {
            "label": "zoominfo_new",
            "skipped": True,
            "skip_reason": "ZOOMINFO_BEARER_TOKEN not set",
        }

    match_input: Dict[str, Any] = {}
    used_search = False
    search_record: Optional[Dict[str, Any]] = None

    email = (email or "").strip().lower()
    phone_digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    phone_clean = phone_digits if len(phone_digits) >= 7 else ""

    if email:
        match_input["emailAddress"] = email
    elif phone_clean:
        match_input["phone"] = phone_clean
    elif full_name and company_name:
        split = _split_name(full_name)
        if split["first"] and split["last"]:
            match_input["firstName"] = split["first"]
            match_input["lastName"] = split["last"]
            match_input["companyName"] = company_name

    if not match_input and full_name:
        # ContactSearch by name to resolve personId — the endpoint's last-resort path.
        split = _split_name(full_name)
        if split["first"] and split["last"]:
            used_search = True
            headers = {
                "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
                "accept": "application/vnd.api+json",
                "content-type": "application/vnd.api+json",
            }
            search_payload = {
                "data": {
                    "type": "ContactSearch",
                    "attributes": {
                        "firstName": split["first"],
                        "lastName": split["last"],
                    },
                }
            }
            search_record = await _send(
                client,
                "POST",
                ZOOMINFO_NEW_SEARCH_URL,
                headers=headers,
                json_body=search_payload,
                label="zoominfo_new_search",
            )
            sjson = search_record.get("response_body_json") or {}
            sdata = sjson.get("data") if isinstance(sjson, dict) else []
            if isinstance(sdata, list) and sdata:
                person_id = sdata[0].get("id")
                if person_id:
                    match_input["personId"] = str(person_id)

    if not match_input:
        record = {
            "label": "zoominfo_new",
            "skipped": True,
            "skip_reason": "insufficient match inputs (no email/phone/name+company/personId)",
        }
        if search_record is not None:
            record["search_record"] = search_record
        return record

    headers = {
        "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
    }
    payload = {
        "data": {
            "type": "ContactEnrich",
            "attributes": {
                "matchPersonInput": [match_input],
                "outputFields": ["mobilePhone", "phone", "email", "emailAlt"],
            },
        }
    }
    record = await _send(
        client,
        "POST",
        ZOOMINFO_NEW_ENRICH_URL,
        headers=headers,
        json_body=payload,
        label="zoominfo_new",
    )
    record["match_input"] = match_input
    record["used_contact_search"] = used_search
    if search_record is not None:
        record["search_record"] = search_record
    record["extracted_fields"] = _summarise_zoominfo_new(record)
    return record


async def probe_apollo(
    client: httpx.AsyncClient, linkedin_url: str
) -> Dict[str, Any]:
    apollo_key = _apollo_api_key()
    if not apollo_key:
        return {
            "label": "apollo",
            "skipped": True,
            "skip_reason": "APOLLO_API_KEY not configured",
        }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": apollo_key,
    }
    payload = {"linkedin_url": linkedin_url}
    record = await _send(
        client,
        "POST",
        APOLLO_ENRICH_URL,
        headers=headers,
        json_body=payload,
        label="apollo",
    )
    record["extracted_fields"] = _summarise_apollo(record)
    return record


def _print_table(records: List[Dict[str, Any]]) -> None:
    print("\n[probe] === PROVIDER SUMMARY ===")
    header = f"{'provider':<22} {'status':>6}  mobile  work  workEmail  personalEmail"
    print(header)
    print("-" * len(header))
    for r in records:
        label = r.get("label", "?")
        if r.get("skipped"):
            print(f"{label:<22} {'-':>6}  (skipped: {r.get('skip_reason')})")
            continue
        status = r.get("response_status")
        ef = r.get("extracted_fields") or {}
        mobile = "Y" if ef.get("mobilePhone") else "n"
        work = "Y" if ef.get("workPhone") else "n"
        we = "Y" if ef.get("workEmail") else "n"
        pe = "Y" if ef.get("personalEmail") else "n"
        print(f"{label:<22} {status!s:>6}    {mobile}      {work}      {we}          {pe}")


async def run_probe(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    linkedin_url = (args.linkedin_url or "").strip()
    candidate_row: Dict[str, Any] = {}
    full_name = (args.full_name or "").strip()
    company_name = (args.company_name or "").strip()
    seed_email = (args.email or "").strip()
    seed_phone = (args.phone or "").strip()

    if args.candidate_id:
        candidate_row = _lookup_candidate(
            args.candidate_id, args.jobdiva_id, args.source
        ) or {}
        if not candidate_row:
            print(
                f"[probe] No sourced_candidates row found for candidate_id={args.candidate_id} "
                f"(jobdiva_id={args.jobdiva_id!r}, source={args.source!r})",
                file=sys.stderr,
            )
        if not linkedin_url:
            linkedin_url = (candidate_row.get("profile_url") or "").strip()
        if not full_name:
            full_name = (candidate_row.get("name") or "").strip()
        if not seed_email:
            seed_email = (candidate_row.get("email") or "").strip()
        if not seed_phone:
            seed_phone = (candidate_row.get("phone") or "").strip()
        data_blob = candidate_row.get("data") or {}
        if isinstance(data_blob, str):
            try:
                data_blob = json.loads(data_blob)
            except Exception:
                data_blob = {}
        if not company_name and isinstance(data_blob, dict):
            enhanced = data_blob.get("enhanced_info") if isinstance(data_blob.get("enhanced_info"), dict) else {}
            company_name = str(
                data_blob.get("company_name")
                or data_blob.get("company")
                or enhanced.get("current_company")
                or enhanced.get("company")
                or ""
            ).strip()

    if not linkedin_url:
        print(
            "[probe] ERROR: no LinkedIn URL available — pass --linkedin-url or "
            "--candidate-id with a row that has profile_url set.",
            file=sys.stderr,
        )
        return 2, {}

    print(f"[probe] linkedin_url   : {linkedin_url}")
    print(f"[probe] full_name      : {full_name!r}")
    print(f"[probe] company_name   : {company_name!r}")
    print(f"[probe] seed_email     : {bool(seed_email)}")
    print(f"[probe] seed_phone     : {bool(seed_phone)}")
    print(f"[probe] ZOOMINFO token : {'set' if ZOOMINFO_BEARER_TOKEN else 'MISSING'}")
    print(f"[probe] APOLLO key     : {'env' if os.getenv('APOLLO_API_KEY') else 'hardcoded fallback'}")

    if args.dry_run:
        print("[probe] --dry-run set — exiting without sending any requests.")
        return 0, {}

    records: List[Dict[str, Any]] = []
    started_at_utc = _utc_iso()
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        legacy_record = await probe_zoominfo_legacy(client, linkedin_url)
        records.append(legacy_record)

        new_record = await probe_zoominfo_new(
            client,
            full_name=full_name,
            company_name=company_name,
            email=seed_email,
            phone=seed_phone,
        )
        records.append(new_record)

        apollo_record = await probe_apollo(client, linkedin_url)
        records.append(apollo_record)
    ended_at_utc = _utc_iso()

    _print_table(records)

    out_doc: Dict[str, Any] = {
        "probe": {
            "client": "airecruiter enrichment-provider probe v1",
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "linkedin_url": linkedin_url,
            "candidate_id": args.candidate_id,
            "jobdiva_id": args.jobdiva_id,
            "source": args.source,
            "full_name": full_name,
            "company_name": company_name,
            "had_seed_email": bool(seed_email),
            "had_seed_phone": bool(seed_phone),
            "zoominfo_token_present": bool(ZOOMINFO_BEARER_TOKEN),
            "apollo_key_source": "env" if os.getenv("APOLLO_API_KEY") else "hardcoded_fallback",
        },
        "records": records,
    }

    out_dir = APPS_API_DIR / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"enrichment_probe_{stamp}.json"
    out_path.write_text(json.dumps(out_doc, indent=2, default=str))
    print(f"\n[probe] wrote evidence file: {out_path}")
    return 0, out_doc


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--linkedin-url", help="LinkedIn profile URL to probe.")
    src.add_argument("--candidate-id", help="Look up the row in sourced_candidates.")
    p.add_argument("--jobdiva-id", default=None)
    p.add_argument("--source", default=None)
    p.add_argument("--full-name", default=None, help="First Last name (for ZoomInfo new path).")
    p.add_argument("--company-name", default=None, help="Current employer (for ZoomInfo new path).")
    p.add_argument("--email", default=None, help="Seed email (for ZoomInfo new matchPersonInput).")
    p.add_argument("--phone", default=None, help="Seed phone (for ZoomInfo new matchPersonInput).")
    p.add_argument("--dry-run", action="store_true", help="Print plan and exit without sending.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    rc, _ = asyncio.run(run_probe(args))
    return rc


if __name__ == "__main__":
    sys.exit(main())
