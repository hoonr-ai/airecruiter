"""Staged Launch PAIR smoke test.

Exercises the four-stage Launch PAIR flow that the new progress modal in
apps/web/app/jobs/new/page.tsx drives:

  Stage 1 (Ready)    — candidates with full phone+email already, launched immediately.
  Stage 2 (ZoomInfo) — POST /candidates/enrich-contact { provider: "zoominfo" }
                       for candidates missing contact, then launch newly-complete.
  Stage 3 (Apollo)   — same as stage 2 with provider: "apollo" for the rest.
  Stage 4 (Manual)   — PATCH /candidates/{id}/phone with operator-supplied
                       phone+email, then launch the candidate.

At the end, the script re-fires send-bulk-interview for the stage-1 batch to
verify the backend's engage_status='sent' idempotency: those candidates should
come back in `skipped_already_sent`.

Run:
    cd apps/api
    venv/bin/python -m scripts.smoke_launch_pair_staged --jobdiva-id 26-05172

Default is --dry-run (skips the external PAIR phone-call dispatch). Pass
--no-dry-run only when you intentionally want the live API engaged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

import httpx


# Four mock candidates exercising each stage. linkedin_url is required for
# enrichment to attempt anything against a real provider — it is set so the
# script structurally exercises the provider-scoped enrich path even when no
# real ZoomInfo/Apollo token is configured (the endpoint returns
# phone_source="none" in that case, which is still a valid stage result).
MOCK_CANDIDATES: List[Dict[str, Any]] = [
    {
        "stage_hint": "ready",
        "candidate_id": "smoke-staged-1",
        "name": "Ready Rachel",
        "email": "rachel.ready@example.com",
        "phone": "+15555550001",
        "linkedin_url": "https://www.linkedin.com/in/rachel-ready",
    },
    {
        "stage_hint": "zoominfo",
        "candidate_id": "smoke-staged-2",
        "name": "Zach Zoominfo",
        "email": "",
        "phone": "",
        "linkedin_url": "https://www.linkedin.com/in/zach-zoominfo",
    },
    {
        "stage_hint": "apollo",
        "candidate_id": "smoke-staged-3",
        "name": "Andy Apollo",
        "email": "",
        "phone": "",
        "linkedin_url": "https://www.linkedin.com/in/andy-apollo",
    },
    {
        "stage_hint": "manual",
        "candidate_id": "smoke-staged-4",
        "name": "Manny Manual",
        "email": "",
        "phone": "",
        "linkedin_url": "https://www.linkedin.com/in/manny-manual",
    },
]


def to_save_record(c: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a mock candidate as a CandidateSaveRecord for /candidates/save."""
    return {
        "candidate_id": c["candidate_id"],
        "name": c["name"],
        "email": c.get("email") or None,
        "phone": c.get("phone") or None,
        "headline": "Smoke Staged Test",
        "location": "",
        "profile_url": c.get("linkedin_url"),
        "image_url": None,
        "resume_id": "",
        "resume_text": "",
        "skills": [],
        "experience_years": 0,
        "source": "smoke-staged-test",
        "match_score": 0,
        "is_selected": True,
        "education": [],
        "certifications": [],
        "company_experience": [],
        "urls": {"linkedin": c.get("linkedin_url")} if c.get("linkedin_url") else {},
        "enhanced_info": None,
    }


def pretty(label: str, data: Any) -> None:
    print(f"\n── {label} " + "─" * max(2, 60 - len(label)))
    try:
        print(json.dumps(data, indent=2, default=str))
    except TypeError:
        print(repr(data))


def post(client: httpx.Client, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = client.post(url, json=body, timeout=60)
    try:
        data = resp.json() if resp.content else {}
    except json.JSONDecodeError:
        data = {"_raw": resp.text}
    if not resp.is_success:
        pretty(f"HTTP {resp.status_code} from {url}", data)
        resp.raise_for_status()
    return data


def patch(client: httpx.Client, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    resp = client.patch(url, json=body, timeout=60)
    try:
        data = resp.json() if resp.content else {}
    except json.JSONDecodeError:
        data = {"_raw": resp.text}
    if not resp.is_success:
        pretty(f"HTTP {resp.status_code} from {url}", data)
        resp.raise_for_status()
    return data


def launch_batch(
    client: httpx.Client,
    api_base: str,
    job_id: str,
    candidates: List[Dict[str, Any]],
    dry_run: bool,
    label: str,
) -> Dict[str, Any]:
    """Save + generate-payload + send-bulk-interview for one batch."""
    if not candidates:
        print(f"\n[{label}] empty batch — skipping.")
        return {}
    candidate_ids = [c["candidate_id"] for c in candidates]
    save_resp = post(client, f"{api_base}/candidates/save", {
        "jobdiva_id": job_id,
        "candidates": [to_save_record(c) for c in candidates],
    })
    pretty(f"[{label}] POST /candidates/save", save_resp)
    if save_resp.get("status") != "success":
        print(f"✗ /candidates/save failed in stage [{label}].", file=sys.stderr)
        return save_resp

    gen_resp = post(client, f"{api_base}/engage/generate-payload", {
        "candidate_ids": candidate_ids,
        "job_id": job_id,
    })
    payload_str = gen_resp.get("payload")
    if not payload_str:
        print(f"✗ generate-payload returned no payload in stage [{label}].", file=sys.stderr)
        return gen_resp

    send_resp = post(client, f"{api_base}/engage/send-bulk-interview", {
        "payload": payload_str,
        "real_candidate_ids": candidate_ids,
        "is_initial_launch": label == "ready",
        "dry_run": dry_run,
    })
    pretty(f"[{label}] POST /engage/send-bulk-interview", send_resp)
    return send_resp


def enrich_one(
    client: httpx.Client,
    api_base: str,
    job_id: str,
    candidate: Dict[str, Any],
    provider: str,
) -> Dict[str, Any]:
    body = {
        "candidate_id": candidate["candidate_id"],
        "jobdiva_id": job_id,
        "source": candidate.get("source"),
        "linkedin_url": candidate.get("linkedin_url"),
        "provider": provider,
    }
    resp = post(client, f"{api_base}/candidates/enrich-contact", body)
    pretty(
        f"[{provider}] POST /candidates/enrich-contact ({candidate['candidate_id']})",
        resp,
    )
    return resp


def main() -> int:
    parser = argparse.ArgumentParser(description="Staged Launch PAIR smoke test")
    parser.add_argument("--jobdiva-id", required=True,
                        help="Alphanumeric jobdiva_id (or numeric job_id) the batch attaches to.")
    parser.add_argument("--api-base", default=os.environ.get("API_BASE", "http://localhost:8000"),
                        help="Backend base URL. Default: $API_BASE or http://localhost:8000.")
    parser.add_argument("--manual-phone", default="+15555550044",
                        help="Phone the script types into the manual stage for smoke-staged-4.")
    parser.add_argument("--manual-email", default="manny.manual@example.com",
                        help="Email the script types into the manual stage for smoke-staged-4.")
    dry_group = parser.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", dest="dry_run", action="store_true",
                           help="Skip external PAIR phone-call dispatch (default).")
    dry_group.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                           help="Engage the live external PAIR API.")
    parser.set_defaults(dry_run=True)
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")
    job_id = str(args.jobdiva_id)

    print(f"▶ Staged smoke launch against {api_base}")
    print(f"   jobdiva_id : {job_id}")
    print(f"   dry_run    : {args.dry_run}")
    print(f"   candidates : {[c['candidate_id'] for c in MOCK_CANDIDATES]}")

    by_id: Dict[str, Dict[str, Any]] = {c["candidate_id"]: dict(c) for c in MOCK_CANDIDATES}

    with httpx.Client() as client:
        # ─── Stage 1: Ready ────────────────────────────────────────────────
        ready_batch = [c for c in by_id.values() if c.get("phone") and c.get("email")]
        ready_resp = launch_batch(client, api_base, job_id, ready_batch, args.dry_run, label="ready")
        ready_ids = [c["candidate_id"] for c in ready_batch]

        # ─── Stage 2: ZoomInfo enrichment ──────────────────────────────────
        zoominfo_batch: List[Dict[str, Any]] = []
        for c in by_id.values():
            if c["stage_hint"] != "zoominfo":
                continue
            enriched = enrich_one(client, api_base, job_id, c, provider="zoominfo")
            phone = enriched.get("phone") or ""
            email = enriched.get("email") or ""
            if phone and email:
                c["phone"] = phone
                c["email"] = email
                zoominfo_batch.append(c)
        launch_batch(client, api_base, job_id, zoominfo_batch, args.dry_run, label="zoominfo")

        # ─── Stage 3: Apollo enrichment ────────────────────────────────────
        apollo_batch: List[Dict[str, Any]] = []
        for c in by_id.values():
            if c["stage_hint"] != "apollo":
                continue
            enriched = enrich_one(client, api_base, job_id, c, provider="apollo")
            phone = enriched.get("phone") or ""
            email = enriched.get("email") or ""
            if phone and email:
                c["phone"] = phone
                c["email"] = email
                apollo_batch.append(c)
        launch_batch(client, api_base, job_id, apollo_batch, args.dry_run, label="apollo")

        # ─── Stage 4: Manual ───────────────────────────────────────────────
        manual_batch: List[Dict[str, Any]] = []
        for c in by_id.values():
            if c["stage_hint"] != "manual":
                continue
            patch_resp = patch(
                client,
                f"{api_base}/candidates/{c['candidate_id']}/phone",
                {
                    "candidate_id": c["candidate_id"],
                    "jobdiva_id": job_id,
                    "phone": args.manual_phone,
                    "email": args.manual_email,
                },
            )
            pretty(f"[manual] PATCH /candidates/{c['candidate_id']}/phone", patch_resp)
            c["phone"] = args.manual_phone
            c["email"] = args.manual_email
            manual_batch.append(c)
        launch_batch(client, api_base, job_id, manual_batch, args.dry_run, label="manual")

        # ─── Idempotency check ─────────────────────────────────────────────
        # Re-fire stage 1 to confirm the backend now skips the already-sent
        # candidates instead of double-dispatching.
        if ready_ids:
            print("\n▶ Idempotency check: re-firing ready batch — expect skipped_already_sent populated")
            retry_resp = launch_batch(client, api_base, job_id, ready_batch, args.dry_run, label="ready-retry")
            skipped = retry_resp.get("skipped_already_sent") or []
            if set(skipped) >= set(ready_ids):
                print(f"✓ Idempotency held: {skipped} reported as already-sent.")
            else:
                print(
                    f"✗ Idempotency check unexpected: skipped={skipped} vs ready_ids={ready_ids}",
                    file=sys.stderr,
                )

    print("\n✓ Staged smoke completed.")
    if args.dry_run:
        print("  External PAIR phone-call dispatch was skipped (dry-run).")
    else:
        print("  Live external PAIR API was engaged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
