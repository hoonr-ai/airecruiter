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

from services.zoominfo_auth import (  # noqa: E402
    ZoomInfoAuthFailed,
    ZoomInfoAuthNotConfigured,
    get_access_token,
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


def _fetch_recent_linkedin_rows(
    n: int,
    jobdiva_id: Optional[str],
    source: Optional[str],
    window_days: int,
) -> List[Dict[str, Any]]:
    """Pull the N most-recent sourced_candidates rows with a LinkedIn URL.

    Used by --batch-from-db. Mirrors the filter knobs of
    scripts/enrichment_hits_count.py so a batch can be scoped to a recent
    sourcing run.
    """
    from psycopg2.extras import RealDictCursor

    from routers._helpers import get_db_connection

    query = (
        "SELECT candidate_id, jobdiva_id, source, name, headline, "
        "profile_url, email, phone, data "
        "FROM sourced_candidates "
        "WHERE profile_url ILIKE '%%linkedin.com/in/%%' "
        "  AND updated_at >= NOW() - (%(window_days)s || ' days')::INTERVAL "
        "  AND (%(jobdiva_id)s IS NULL OR jobdiva_id = %(jobdiva_id)s) "
        "  AND (%(source)s     IS NULL OR source     = %(source)s) "
        "ORDER BY updated_at DESC NULLS LAST "
        "LIMIT %(n)s"
    )
    params = {
        "n": int(n),
        "jobdiva_id": jobdiva_id,
        "source": source,
        "window_days": str(int(window_days)),
    }
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
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


async def _zi_auth_headers() -> Optional[Dict[str, str]]:
    """Build the standard Data API headers using the OAuth auth module.

    Returns None when OAuth isn't configured or token mint fails — caller
    should record a `skipped` outcome.
    """
    try:
        token = await get_access_token()
    except (ZoomInfoAuthNotConfigured, ZoomInfoAuthFailed):
        return None
    return {
        "Authorization": f"Bearer {token}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
    }


async def probe_zoominfo_new(
    client: httpx.AsyncClient,
    *,
    full_name: str,
    company_name: str,
    email: str,
    phone: str,
) -> Dict[str, Any]:
    """Probe the ZoomInfo Data API end-to-end (ContactSearch → ContactEnrich).

    Build matchPersonInput in this order:
      email > phone > firstName+lastName+companyName > personId from ContactSearch.
    The legacy `/enrich/contact` endpoint has been retired (returns 401 even
    with a fresh OAuth-minted token); only the new Data API is probed.
    """
    headers = await _zi_auth_headers()
    if headers is None:
        return {
            "label": "zoominfo_new",
            "skipped": True,
            "skip_reason": "ZoomInfo OAuth not configured or token mint failed",
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
    print(f"[probe] ZOOMINFO auth   : OAuth client_credentials (auto-mint)")
    print(f"[probe] APOLLO key     : {'env' if os.getenv('APOLLO_API_KEY') else 'hardcoded fallback'}")
    if getattr(args, "zoominfo_only", False):
        print("[probe] --zoominfo-only : Apollo will NOT be called")

    if args.dry_run:
        print("[probe] --dry-run set — exiting without sending any requests.")
        return 0, {}

    records: List[Dict[str, Any]] = []
    started_at_utc = _utc_iso()
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Legacy `/enrich/contact` endpoint retired — it 401s even with a
        # fresh OAuth-minted token. Only the new Data API is probed.
        new_record = await probe_zoominfo_new(
            client,
            full_name=full_name,
            company_name=company_name,
            email=seed_email,
            phone=seed_phone,
        )
        records.append(new_record)

        if getattr(args, "zoominfo_only", False):
            records.append({
                "label": "apollo",
                "skipped": True,
                "skip_reason": "--zoominfo-only flag",
            })
        else:
            apollo_record = await probe_apollo(client, linkedin_url)
            records.append(apollo_record)
    ended_at_utc = _utc_iso()

    _print_table(records)

    out_doc: Dict[str, Any] = {
        "probe": {
            "client": "airecruiter enrichment-provider probe v2 (OAuth)",
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
            "apollo_key_source": "env" if os.getenv("APOLLO_API_KEY") else "hardcoded_fallback",
            "zoominfo_only": bool(getattr(args, "zoominfo_only", False)),
        },
        "records": records,
    }

    if not getattr(args, "_batch", False):
        out_dir = APPS_API_DIR / "tmp"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"enrichment_probe_{stamp}.json"
        out_path.write_text(json.dumps(out_doc, indent=2, default=str))
        print(f"\n[probe] wrote evidence file: {out_path}")
    return 0, out_doc


def _classify_record(rec: Dict[str, Any]) -> str:
    """Bucket one probe record for the aggregate summary.

    Returns one of: 'skipped', '2xx_fields', '2xx_empty', '4xx', '5xx',
    'error', 'other'.
    """
    if rec.get("skipped"):
        return "skipped"
    if rec.get("error"):
        return "error"
    status = rec.get("response_status")
    if not isinstance(status, int):
        return "other"
    if 200 <= status < 300:
        ef = rec.get("extracted_fields") or {}
        has_field = any(
            str(ef.get(k) or "").strip()
            for k in ("mobilePhone", "workPhone", "workEmail", "personalEmail")
        )
        return "2xx_fields" if has_field else "2xx_empty"
    if 400 <= status < 500:
        # 401 is the one we most care about — break it out separately so the
        # aggregate line tells us "token rejected" at a glance.
        return "401" if status == 401 else "4xx"
    if 500 <= status < 600:
        return "5xx"
    return "other"


def _print_aggregate(per_label_buckets: Dict[str, Dict[str, int]]) -> None:
    """Print one line per provider summarising N probes."""
    order = ["zoominfo_new_search", "zoominfo_new", "apollo"]
    print("\n[probe] === AGGREGATE ACROSS BATCH ===")
    for label in order:
        buckets = per_label_buckets.get(label)
        if not buckets:
            continue
        n = sum(buckets.values())
        parts: List[str] = [f"N={n}"]
        for key in ("2xx_fields", "2xx_empty", "401", "4xx", "5xx", "error", "skipped", "other"):
            v = buckets.get(key, 0)
            if v:
                parts.append(f"{key}={v}")
        print(f"  {label:<18} {'  '.join(parts)}")


async def run_batch(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    """Loop run_probe over the N most-recent linkedin rows."""
    rows = _fetch_recent_linkedin_rows(
        args.batch_from_db, args.jobdiva_id, args.source, args.window_days
    )
    if not rows:
        print(
            f"[probe] No sourced_candidates rows match (n={args.batch_from_db}, "
            f"jobdiva_id={args.jobdiva_id!r}, source={args.source!r}, "
            f"window_days={args.window_days}).",
            file=sys.stderr,
        )
        return 2, {}

    print(f"[probe] batch_from_db   : {len(rows)} row(s)")
    print(f"[probe] jobdiva-id      : {args.jobdiva_id!r}")
    print(f"[probe] source          : {args.source!r}")
    print(f"[probe] window-days     : {args.window_days}")
    print(f"[probe] ZOOMINFO auth   : OAuth client_credentials (auto-mint)")
    if getattr(args, "zoominfo_only", False):
        print("[probe] --zoominfo-only  : Apollo will NOT be called")
    if args.dry_run:
        print("[probe] --dry-run set — exiting without sending any requests.")
        return 0, {}

    started_at_utc = _utc_iso()
    runs: List[Dict[str, Any]] = []
    per_label_buckets: Dict[str, Dict[str, int]] = {}

    # Reuse the same arg shape per row by mutating a copy of args so run_probe
    # can read seed_email / seed_phone / company_name from the row.
    for idx, row in enumerate(rows, start=1):
        per_args = argparse.Namespace(**vars(args))
        per_args.linkedin_url = (row.get("profile_url") or "").strip()
        per_args.candidate_id = row.get("candidate_id")
        # Don't override the user's --jobdiva-id / --source filters; just
        # forward the row's own values so the JSON output records context.
        # The run_probe path doesn't actually use jobdiva_id/source as
        # request inputs — it uses them for the candidate-id lookup only.
        per_args.full_name = (row.get("name") or "").strip() or None
        per_args.email = (row.get("email") or "").strip() or None
        per_args.phone = (row.get("phone") or "").strip() or None
        data_blob = row.get("data") or {}
        if isinstance(data_blob, str):
            try:
                data_blob = json.loads(data_blob)
            except Exception:
                data_blob = {}
        enhanced = data_blob.get("enhanced_info") if isinstance(data_blob, dict) else {}
        enhanced = enhanced if isinstance(enhanced, dict) else {}
        per_args.company_name = str(
            data_blob.get("company_name")
            or data_blob.get("company")
            or enhanced.get("current_company")
            or enhanced.get("company")
            or ""
        ).strip() or None

        if not per_args.linkedin_url:
            print(f"[probe] skip row {idx}: no profile_url", file=sys.stderr)
            continue
        print(f"\n[probe] ── batch row {idx}/{len(rows)} ── candidate_id={per_args.candidate_id} ──")
        # Suppress per-row file writes; we'll write one combined file at the
        # end. Easiest way: pass a marker on the namespace.
        per_args._batch = True
        rc, doc = await run_probe(per_args)
        if rc != 0 or not doc:
            continue
        runs.append(doc)
        for rec in doc.get("records") or []:
            label = rec.get("label", "?")
            bucket = _classify_record(rec)
            per_label_buckets.setdefault(label, {})
            per_label_buckets[label][bucket] = per_label_buckets[label].get(bucket, 0) + 1

    ended_at_utc = _utc_iso()
    _print_aggregate(per_label_buckets)

    out_doc: Dict[str, Any] = {
        "probe": {
            "client": "airecruiter enrichment-provider probe v2 (OAuth batch)",
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "batch_size_requested": args.batch_from_db,
            "batch_size_executed": len(runs),
            "jobdiva_id": args.jobdiva_id,
            "source": args.source,
            "window_days": args.window_days,
            "zoominfo_only": bool(getattr(args, "zoominfo_only", False)),
            "zoominfo_auth_mode": "oauth_client_credentials",
        },
        "aggregate": per_label_buckets,
        "runs": runs,
    }
    out_dir = APPS_API_DIR / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"enrichment_probe_batch_{stamp}.json"
    out_path.write_text(json.dumps(out_doc, indent=2, default=str))
    print(f"\n[probe] wrote batch evidence file: {out_path}")
    return 0, out_doc


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--linkedin-url", help="LinkedIn profile URL to probe.")
    src.add_argument("--candidate-id", help="Look up the row in sourced_candidates.")
    src.add_argument(
        "--batch-from-db",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Probe the N most-recent sourced_candidates rows that have a "
            "linkedin.com/in/ profile URL. Combine with --jobdiva-id and/or "
            "--source to scope the sample to a recent run."
        ),
    )
    p.add_argument("--jobdiva-id", default=None)
    p.add_argument("--source", default=None)
    p.add_argument("--full-name", default=None, help="First Last name (for ZoomInfo new path).")
    p.add_argument("--company-name", default=None, help="Current employer (for ZoomInfo new path).")
    p.add_argument("--email", default=None, help="Seed email (for ZoomInfo new matchPersonInput).")
    p.add_argument("--phone", default=None, help="Seed phone (for ZoomInfo new matchPersonInput).")
    p.add_argument(
        "--zoominfo-only",
        action="store_true",
        help="Skip the Apollo probe entirely. Useful when verifying the ZoomInfo path in isolation.",
    )
    p.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Recency window for --batch-from-db (default 7).",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan and exit without sending.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    if args.batch_from_db is not None:
        rc, _ = asyncio.run(run_batch(args))
    else:
        rc, _ = asyncio.run(run_probe(args))
    return rc


if __name__ == "__main__":
    sys.exit(main())
