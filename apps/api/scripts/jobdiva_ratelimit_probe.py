"""One-shot probe to capture proof of JobDiva API rate limiting.

Fires a controlled burst of JobDiva calls until it sees a 429 or 503, then
dumps the full captured response (status, all headers, full body, URL,
timings) to ``apps/api/tmp/jobdiva_ratelimit_evidence_<utc>.json`` ready to
attach to a JobDiva support ticket.

Why this exists: every JobDiva call site in ``services/jobdiva.py`` logs only
``status + response.text[:200]`` and throws the response headers away — so we
have no way to prove a 429 in past logs (we never captured ``Retry-After``).

Run:
    cd apps/api
    source .env
    venv/bin/python -m scripts.jobdiva_ratelimit_probe --dry-run
    venv/bin/python -m scripts.jobdiva_ratelimit_probe

Bails on the FIRST 429/503 captured. Hard caps on --max-requests and
--max-seconds regardless. Use --dry-run to inspect the plan without sending.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from core import (  # noqa: E402
    JOBDIVA_API_URL,
    JOBDIVA_CLIENT_ID,
    JOBDIVA_USERNAME,
)
from services.jobdiva import JobDivaService  # noqa: E402


RATE_LIMIT_STATUSES = {429, 503}
PROBE_CLIENT_TAG = "airecruiter rate-limit probe v1"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    redacted = dict(headers)
    if "Authorization" in redacted:
        redacted["Authorization"] = "Bearer <redacted>"
    if "authorization" in redacted:
        redacted["authorization"] = "Bearer <redacted>"
    return redacted


def _build_talent_search_request(
    api_url: str, token: str, skills: str
) -> Dict[str, Any]:
    return {
        "method": "POST",
        "url": f"{api_url}/apiv2/jobdiva/TalentSearch",
        "headers": {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        "json": {
            "talentSearchDef": {
                "skills": skills,
                "countries": "USA",
                "pageNumber": 0,
                "pageSize": 50,
            }
        },
    }


def _build_job_applicants_request(
    api_url: str, token: str, job_id: str
) -> Dict[str, Any]:
    return {
        "method": "GET",
        "url": f"{api_url}/apiv2/bi/JobApplicantsDetail",
        "headers": {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        "params": {"jobId": job_id},
    }


async def _send_one(
    client: httpx.AsyncClient,
    req: Dict[str, Any],
    request_index: int,
) -> Dict[str, Any]:
    probe_request_id = uuid.uuid4().hex
    started_monotonic = time.monotonic()
    started_at = _utc_iso()
    error: Optional[str] = None
    response: Optional[httpx.Response] = None
    try:
        response = await client.request(
            req["method"],
            req["url"],
            headers=req["headers"],
            params=req.get("params"),
            json=req.get("json"),
        )
    except Exception as e:  # network / timeout — NOT what we're trying to prove
        error = f"{type(e).__name__}: {e}"

    ended_monotonic = time.monotonic()
    ended_at = _utc_iso()
    latency_ms = int((ended_monotonic - started_monotonic) * 1000)

    record: Dict[str, Any] = {
        "i": request_index,
        "probe_request_id": probe_request_id,
        "request_started_at_utc": started_at,
        "request_ended_at_utc": ended_at,
        "latency_ms": latency_ms,
        "method": req["method"],
        "url": req["url"],
        "request_headers": _redact_headers(req["headers"]),
        "request_params": req.get("params"),
        "request_body": req.get("json"),
    }
    if error is not None:
        record["error"] = error
        record["response_status"] = None
    else:
        assert response is not None
        record["response_status"] = response.status_code
        record["response_headers"] = dict(response.headers)
        record["response_body"] = response.text
    return record


async def run_probe(args: argparse.Namespace) -> int:
    print(f"[probe] endpoint            : {args.endpoint}")
    print(f"[probe] JOBDIVA_API_URL     : {JOBDIVA_API_URL}")
    print(f"[probe] JOBDIVA_USERNAME    : {JOBDIVA_USERNAME}")
    print(f"[probe] JOBDIVA_CLIENT_ID   : {JOBDIVA_CLIENT_ID}")
    print(f"[probe] concurrency         : {args.concurrency}")
    print(f"[probe] max_requests        : {args.max_requests}")
    print(f"[probe] max_seconds         : {args.max_seconds}")
    if args.endpoint == "talent-search":
        print(f"[probe] payload skills      : {args.skills!r}")
    else:
        print(f"[probe] job_id              : {args.job_id!r}")

    if args.dry_run:
        print("[probe] --dry-run set — exiting without sending any requests.")
        return 0

    if args.endpoint == "job-applicants" and not args.job_id:
        print("[probe] ERROR: --endpoint job-applicants requires --job-id", file=sys.stderr)
        return 2

    print(
        "\n[probe] This will deliberately stress the JobDiva API on your "
        "account.\n[probe] Ctrl-C within 3 seconds to abort..."
    )
    for n in (3, 2, 1):
        print(f"[probe]   {n}...")
        await asyncio.sleep(1)

    # Auth via the same path prod uses, so we get the same token shape.
    svc = JobDivaService()
    token = await svc.authenticate()
    if not token:
        print("[probe] ERROR: JobDiva authentication failed; nothing to probe.", file=sys.stderr)
        return 3

    if args.endpoint == "talent-search":
        request_template = _build_talent_search_request(JOBDIVA_API_URL, token, args.skills)
    else:
        request_template = _build_job_applicants_request(JOBDIVA_API_URL, token, args.job_id)

    semaphore = asyncio.Semaphore(args.concurrency)
    stop_event = asyncio.Event()
    records: List[Dict[str, Any]] = []
    rate_limited_record: Optional[Dict[str, Any]] = None
    started_monotonic = time.monotonic()
    started_at_utc = _utc_iso()

    async def fire(i: int, client: httpx.AsyncClient) -> None:
        nonlocal rate_limited_record
        if stop_event.is_set():
            return
        if time.monotonic() - started_monotonic >= args.max_seconds:
            return
        async with semaphore:
            if stop_event.is_set():
                return
            record = await _send_one(client, request_template, i)
        records.append(record)
        status = record.get("response_status")
        suffix = ""
        if status in RATE_LIMIT_STATUSES:
            suffix = "  <-- RATE LIMITED"
        elif record.get("error"):
            suffix = f"  ({record['error']})"
        print(f"[probe] req #{i:>3}  status={status}  latency_ms={record['latency_ms']}{suffix}")
        if status in RATE_LIMIT_STATUSES and rate_limited_record is None:
            rate_limited_record = record
            stop_event.set()

    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [asyncio.create_task(fire(i + 1, client)) for i in range(args.max_requests)]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=args.max_seconds + 5,
            )
        except asyncio.TimeoutError:
            stop_event.set()
            for t in tasks:
                if not t.done():
                    t.cancel()

    ended_at_utc = _utc_iso()
    records.sort(key=lambda r: r["i"])

    out_dir = APPS_API_DIR / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"jobdiva_ratelimit_evidence_{stamp}.json"

    preceding_summary = [
        {"i": r["i"], "status": r.get("response_status"), "latency_ms": r["latency_ms"],
         **({"error": r["error"]} if r.get("error") else {})}
        for r in records
        if rate_limited_record is None or r["i"] != rate_limited_record["i"]
    ]

    out_doc: Dict[str, Any] = {
        "probe": {
            "client": PROBE_CLIENT_TAG,
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "endpoint": (
                f"{request_template['method']} {request_template['url']}"
            ),
            "concurrency": args.concurrency,
            "max_requests": args.max_requests,
            "max_seconds": args.max_seconds,
            "requests_sent": len(records),
            "first_rate_limited_at_request": (
                rate_limited_record["i"] if rate_limited_record else None
            ),
            "rate_limit_captured": rate_limited_record is not None,
        },
        "rate_limited_response": rate_limited_record,
        "preceding_responses_summary": preceding_summary,
    }

    out_path.write_text(json.dumps(out_doc, indent=2, default=str))
    print(f"\n[probe] wrote evidence file: {out_path}")

    if rate_limited_record is None:
        print(
            "[probe] No 429/503 captured within the cap. Try larger "
            "--max-requests / --concurrency, or run again later."
        )
        return 1

    rh = rate_limited_record.get("response_headers", {}) or {}
    retry_after = rh.get("Retry-After") or rh.get("retry-after")
    rl_limit = rh.get("X-RateLimit-Limit") or rh.get("X-Rate-Limit-Limit")
    rl_remaining = (
        rh.get("X-RateLimit-Remaining") or rh.get("X-Rate-Limit-Remaining")
    )
    rl_reset = rh.get("X-RateLimit-Reset") or rh.get("X-Rate-Limit-Reset")
    body = rate_limited_record.get("response_body") or ""
    print("\n[probe] === CAPTURED RATE LIMIT RESPONSE ===")
    print(f"[probe] request_id            : {rate_limited_record['probe_request_id']}")
    print(f"[probe] request_index         : {rate_limited_record['i']}")
    print(f"[probe] timestamp (utc)       : {rate_limited_record['request_started_at_utc']}")
    print(f"[probe] url                   : {rate_limited_record['url']}")
    print(f"[probe] status                : {rate_limited_record['response_status']}")
    print(f"[probe] Retry-After header    : {retry_after!r}")
    print(f"[probe] X-RateLimit-Limit     : {rl_limit!r}")
    print(f"[probe] X-RateLimit-Remaining : {rl_remaining!r}")
    print(f"[probe] X-RateLimit-Reset     : {rl_reset!r}")
    print(f"[probe] body (first 500c)     : {body[:500]!r}")
    return 0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--endpoint",
        choices=["talent-search", "job-applicants"],
        default="talent-search",
        help="Which JobDiva endpoint to probe (default: talent-search).",
    )
    p.add_argument("--concurrency", type=int, default=20)
    p.add_argument("--max-requests", type=int, default=200)
    p.add_argument("--max-seconds", type=int, default=120)
    p.add_argument("--skills", default="python", help="Skills string for talent-search payload.")
    p.add_argument("--job-id", default=None, help="Required when --endpoint job-applicants.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and exit without sending.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run_probe(args))


if __name__ == "__main__":
    sys.exit(main())
