#!/usr/bin/env python3
"""Measure what Apollo actually returns, before vs after a credit top-up.

Answers three questions that decide whether topping up Apollo credits helps the
sourcing contact chain at all:

  1. Is the key blocked on CREDITS (422 "insufficient credits") or on something
     else (401 auth, 403 plan, 404 no-match)? Only the first is fixed by paying.
  2. Once credits exist, does `people/enrich` return REAL contact data, or the
     masked `email_not_unlocked@domain.com` placeholder? Apollo masks contact
     unless the request opts into revealing it, so credits alone may change
     nothing that the pipeline can use.
  3. Do we get PHONES specifically, or only emails? The chain's expensive
     fallback exists mostly to fill phones, so email-only relief still leaves
     Exa carrying the phone load.

Usage
-----
    # baseline, before topping up
    python3 scripts/apollo_credit_probe.py --out /tmp/apollo_before.json

    # after topping up
    python3 scripts/apollo_credit_probe.py --out /tmp/apollo_after.json

    # optional: ask Apollo to reveal, to see whether reveal flags are the gap.
    # NOTE: reveal consumes credits per record — that is the point of the flag.
    python3 scripts/apollo_credit_probe.py --reveal-emails --out /tmp/apollo_reveal.json

    # compare
    python3 scripts/apollo_credit_probe.py --compare /tmp/apollo_before.json /tmp/apollo_after.json

Reads APOLLO_API_KEY from the environment, falling back to whatever
contact_enrichment resolved (which may be the legacy in-repo key — see the WARN
it logs at import). Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

# Public figures with stable LinkedIn URLs — Apollo should have records for
# these, so a miss points at the plan/credits rather than at obscurity.
DEFAULT_URLS = [
    "https://www.linkedin.com/in/satyanadella",
    "https://www.linkedin.com/in/williamhgates",
    "https://www.linkedin.com/in/reidhoffman",
    "https://www.linkedin.com/in/jeffweiner08",
    "https://www.linkedin.com/in/andrewyng",
]


def _classify(status: int, body: str) -> str:
    low = (body or "").lower()
    if status == 200:
        return "ok"
    if "insufficient credits" in low or "lead credits" in low:
        return "NO_CREDITS (a top-up fixes this)"
    if status in (401, 403):
        return "AUTH_OR_PLAN (a top-up will NOT fix this — check the key/plan)"
    if status == 404:
        return "NO_MATCH (Apollo has no record for this URL)"
    if status == 429:
        return "RATE_LIMITED (retry later)"
    return f"OTHER_{status}"


async def _probe_one(client, url: str, key: str, reveal_emails: bool, reveal_phones: bool) -> Dict[str, Any]:
    from services.contact_enrichment import (
        APOLLO_ENRICH_URL,
        extract_apollo_contact_fields,
    )

    payload: Dict[str, Any] = {"linkedin_url": url}
    if reveal_emails:
        payload["reveal_personal_emails"] = True
    if reveal_phones:
        # Apollo delivers revealed phones asynchronously to a webhook on most
        # plans; without one this may return nothing even with credits. Included
        # so the probe can prove whether that is the blocker.
        payload["reveal_phone_number"] = True

    row: Dict[str, Any] = {"url": url}
    try:
        res = await client.post(
            APOLLO_ENRICH_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-API-Key": key,
            },
            json=payload,
        )
    except Exception as exc:
        row.update(status=None, verdict=f"REQUEST_FAILED: {exc!r}")
        return row

    body = res.text or ""
    row["status"] = res.status_code
    row["verdict"] = _classify(res.status_code, body)
    if res.status_code != 200:
        row["body_head"] = body[:200]
        return row

    try:
        data = res.json()
    except ValueError:
        row["body_head"] = body[:200]
        return row

    person = data.get("person") if isinstance(data, dict) else {}
    person = person if isinstance(person, dict) else {}
    raw_email = str(person.get("email") or "")
    fields = extract_apollo_contact_fields(data)

    # Apollo returns an empty person shell that just echoes the URL back when it
    # cannot match — treat identity presence, not dict presence, as a match.
    row["matched"] = bool(str(person.get("name") or "").strip())
    row["raw_email_masked"] = "not_unlocked" in raw_email.lower()
    # Report only presence, never the actual contact values.
    row["has_email_after_filter"] = bool(fields.get("workEmail") or fields.get("personalEmail"))
    row["has_phone_after_filter"] = bool(fields.get("mobilePhone") or fields.get("workPhone"))
    row["phone_candidate_count"] = len(fields.get("phoneCandidates") or [])
    # A record whose only number is the employer switchboard is NOT a phone win:
    # extract_apollo_contact_fields keeps org lines out of the candidate slots.
    row["org_line_only"] = (
        not row["has_phone_after_filter"] and row["phone_candidate_count"] > 0
    )
    return row


async def _run(args: argparse.Namespace) -> int:
    import httpx
    from services import contact_enrichment as ce

    key = (ce.APOLLO_API_KEY or "").strip()
    if not key:
        print("✗ No Apollo API key resolved (set APOLLO_API_KEY).", file=sys.stderr)
        return 2
    print(f"key source: {ce.APOLLO_KEY_SOURCE}   (value never printed)")
    print(f"reveal_emails={args.reveal_emails}  reveal_phones={args.reveal_phones}")
    print(f"probing {len(args.urls)} LinkedIn URLs against {ce.APOLLO_ENRICH_URL}\n")

    rows: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for url in args.urls:
            row = await _probe_one(client, url, key, args.reveal_emails, args.reveal_phones)
            rows.append(row)
            print(
                f"  {row['verdict']:<48s} "
                f"email={row.get('has_email_after_filter')} "
                f"phone={row.get('has_phone_after_filter')} "
                f"masked={row.get('raw_email_masked')}  {url}"
            )

    ok = [r for r in rows if r.get("status") == 200]
    summary = {
        "probed": len(rows),
        "http_200": len(ok),
        "matched": sum(1 for r in ok if r.get("matched")),
        "with_email": sum(1 for r in ok if r.get("has_email_after_filter")),
        "with_phone": sum(1 for r in ok if r.get("has_phone_after_filter")),
        "masked_email_returned": sum(1 for r in ok if r.get("raw_email_masked")),
        "org_line_only": sum(1 for r in ok if r.get("org_line_only")),
        "verdicts": sorted({r["verdict"] for r in rows}),
        "reveal_emails": args.reveal_emails,
        "reveal_phones": args.reveal_phones,
        "key_source": ce.APOLLO_KEY_SOURCE,
    }
    print("\nsummary:", json.dumps(summary, indent=2))

    if summary["masked_email_returned"]:
        print(
            "\n→ Apollo MATCHED but masked the contact. Credits alone will not help;\n"
            "  the request has to opt into revealing (and reveal consumes credits)."
        )
    if summary["http_200"] and not summary["with_phone"]:
        print(
            "\n→ No phones came back. The expensive Exa fallback exists mainly to fill\n"
            "  phones, so this top-up would not reduce Exa spend much on its own."
        )

    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
        print(f"\nwrote {args.out}")
    return 0


def _compare(before_path: str, after_path: str) -> int:
    before = json.loads(Path(before_path).read_text())["summary"]
    after = json.loads(Path(after_path).read_text())["summary"]
    keys = ("http_200", "matched", "with_email", "with_phone", "masked_email_returned")
    width = max(len(k) for k in keys)
    print(f"{'metric':<{width}}  before  after")
    for k in keys:
        print(f"{k:<{width}}  {before.get(k, 0):>6}  {after.get(k, 0):>5}")
    gained_phone = after.get("with_phone", 0) - before.get("with_phone", 0)
    gained_email = after.get("with_email", 0) - before.get("with_email", 0)
    print(f"\nphones gained: {gained_phone}   emails gained: {gained_email}")
    if gained_phone <= 0:
        print("→ No phone improvement: Exa would still carry the phone load.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--urls", nargs="*", default=DEFAULT_URLS, help="LinkedIn profile URLs to probe")
    ap.add_argument("--reveal-emails", action="store_true", dest="reveal_emails",
                    help="send reveal_personal_emails=true (CONSUMES CREDITS per record)")
    ap.add_argument("--reveal-phones", action="store_true", dest="reveal_phones",
                    help="send reveal_phone_number=true (CONSUMES CREDITS; usually needs a webhook)")
    ap.add_argument("--out", help="write the JSON result here")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="compare two result files instead of probing")
    args = ap.parse_args()
    if args.compare:
        return _compare(*args.compare)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
