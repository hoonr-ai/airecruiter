"""Spike: probe the Exa **Agent API** for contact enrichment (email + phone).

Phase-1 go/no-go for adding Exa as a third contact-enrichment source alongside
ZoomInfo + Apollo (see plan: exa-agent-has-contact). The Exa *Agent* API
researches a known person and returns structured contact fields with citations.
For each candidate we already know (name / company / LinkedIn URL), this:

  1. POSTs an agent run (https://api.exa.ai/agent/runs) with a contact query
     and an outputSchema requesting {contact: {email, phone}},
  2. polls GET /agent/runs/{id} with a BOUNDED timeout until terminal,
  3. reads back output.structured + costDollars, and
  4. compares to expected values (if provided) and times the whole call.

Output is a per-candidate table plus an aggregate hit-rate / latency / cost
summary so we can judge whether Exa is worth wiring into the Step-5 enrichment
path. Nothing here touches the app or the DB (unless you pass --candidate-id,
which only *reads* sourced_candidates for seed data).

Why raw HTTP and not the SDK: the pinned exa_py in this venv (2.12.0) predates
exa.beta.agent, and the existing enrichment providers (ZoomInfo/Apollo in
services/contact_enrichment.py) already call their HTTP APIs via httpx — so the
Agent API fits the same pattern with no dependency bump.

Inputs (at least one candidate required):
    --linkedin-url <url>     Repeatable. Probe these URLs directly.
    --candidate-id <id>      Repeatable. Read profile_url/name/company from
                             sourced_candidates (mirrors the endpoint's lookup).
    --input <path.json>      JSON list of objects:
                             {"linkedin_url","full_name","company_name",
                              "expected_email","expected_phone"}

Options:
    --full-name / --company-name   Context for a single --linkedin-url run.
    --effort <low|medium|high|xhigh|auto>   Agent effort (default low).
    --timeout <s>            Bounded poll timeout (default 120).
    --poll-interval <s>      Poll interval (default 4, per Exa docs).
    --dry-run                Print the plan and exit without calling Exa.

Run:
    cd apps/api
    venv/bin/python -m scripts.exa_contact_spike \
        --linkedin-url https://www.linkedin.com/in/example \
        --full-name "Jane Smith" --company-name "Acme"
    venv/bin/python -m scripts.exa_contact_spike --input /tmp/candidates.json
    venv/bin/python -m scripts.exa_contact_spike --candidate-id <id> --candidate-id <id2>

Writes a full evidence document to apps/api/tmp/exa_contact_spike_<utc>.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from core.config import EXA_API_KEY  # noqa: E402

AGENT_RUNS_URL = "https://api.exa.ai/agent/runs"
AGENT_BETA = "agent-2026-05-07"
MIN_PHONE_DIGITS = 7
TERMINAL = {"completed", "failed", "cancelled"}

# Schema we ask the agent to fill. Kept to the two billable contact fields
# (email $0.02 / phone $0.07 per run) — richer schemas just cost more.
CONTACT_SCHEMA = {
    "type": "object",
    "properties": {
        "contact": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "format": "email"},
                "phone": {"type": "string", "format": "phone"},
            },
        }
    },
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    plus = "+" if raw.startswith("+") else ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{plus}{digits}" if digits else ""


def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": EXA_API_KEY,
        "Exa-Beta": AGENT_BETA,
    }


def _lookup_candidate(candidate_id: str) -> Dict[str, Any]:
    """Read seed data from sourced_candidates (read-only), like the endpoint does."""
    from psycopg2.extras import RealDictCursor

    from routers._helpers import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT candidate_id, name, profile_url, email, phone, data "
                "FROM sourced_candidates WHERE candidate_id = %s "
                "ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST",
                (candidate_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}
    finally:
        conn.close()


def _candidate_from_row(candidate_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    data_blob = row.get("data") or {}
    if isinstance(data_blob, str):
        try:
            data_blob = json.loads(data_blob)
        except Exception:
            data_blob = {}
    enhanced = data_blob.get("enhanced_info") if isinstance(data_blob, dict) else {}
    enhanced = enhanced if isinstance(enhanced, dict) else {}
    company = str(
        (data_blob.get("company_name") if isinstance(data_blob, dict) else "")
        or (data_blob.get("company") if isinstance(data_blob, dict) else "")
        or enhanced.get("current_company")
        or enhanced.get("company")
        or ""
    ).strip()
    return {
        "candidate_id": candidate_id,
        "linkedin_url": (row.get("profile_url") or "").strip(),
        "full_name": (row.get("name") or "").strip(),
        "company_name": company,
        # The persisted email/phone act as ground truth for the comparison.
        "expected_email": (row.get("email") or "").strip(),
        "expected_phone": (row.get("phone") or "").strip(),
    }


def _build_query(c: Dict[str, Any]) -> str:
    name = str(c.get("full_name") or "").strip()
    company = str(c.get("company_name") or "").strip()
    url = str(c.get("linkedin_url") or "").strip()
    who = name or "this person"
    parts = [f"Find the work email and phone number for {who}"]
    if company:
        parts.append(f"at {company}")
    if url:
        parts.append(f". LinkedIn: {url}")
    return " ".join(parts).replace(" .", ".")


def _extract_contact(structured: Any) -> Dict[str, str]:
    """Pull email/phone out of the agent's structured output (best-effort on shape)."""
    contact = {}
    if isinstance(structured, dict):
        contact = structured.get("contact") if isinstance(structured.get("contact"), dict) else structured
    email = ""
    phone = ""
    if isinstance(contact, dict):
        email = str(contact.get("email") or "").strip()
        phone = _normalise_phone(str(contact.get("phone") or ""))
    if sum(ch.isdigit() for ch in phone) < MIN_PHONE_DIGITS:
        phone = ""
    return {"email": email, "phone": phone}


def probe_one(client: httpx.Client, c: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """Create an agent run, poll to terminal, extract contact. Timed."""
    query = _build_query(c)
    rec: Dict[str, Any] = {
        "candidate_id": c.get("candidate_id"),
        "linkedin_url": c.get("linkedin_url"),
        "full_name": c.get("full_name"),
        "company_name": c.get("company_name"),
        "query": query,
        "expected_email": c.get("expected_email"),
        "expected_phone": c.get("expected_phone"),
    }
    started = time.monotonic()
    try:
        body = {"query": query, "outputSchema": CONTACT_SCHEMA, "effort": args.effort}
        r = client.post(AGENT_RUNS_URL, headers=_headers(), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"create {r.status_code}: {r.text[:200]}")
        run = r.json()
        run_id = run.get("id")
        rec["run_id"] = run_id

        status = run.get("status")
        final = run if status in TERMINAL else None
        deadline = started + args.timeout
        while final is None and time.monotonic() < deadline:
            time.sleep(args.poll_interval)
            g = client.get(f"{AGENT_RUNS_URL}/{run_id}", headers=_headers())
            if g.status_code >= 400:
                raise RuntimeError(f"poll {g.status_code}: {g.text[:200]}")
            run = g.json()
            status = run.get("status")
            if status in TERMINAL:
                final = run

        if final is None:
            rec["ok"] = False
            rec["error"] = f"timeout after {args.timeout}s (last status={status})"
            rec["email_hit"] = rec["phone_hit"] = False
            return rec

        rec["status"] = status
        output = final.get("output") or {}
        contact = _extract_contact(output.get("structured"))
        rec["email"] = contact["email"]
        rec["phone"] = contact["phone"]
        rec["email_hit"] = bool(contact["email"])
        rec["phone_hit"] = bool(contact["phone"])
        rec["text"] = str(output.get("text") or "")[:500]
        rec["cost_dollars"] = (final.get("costDollars") or {}).get("total")
        rec["grounding_count"] = len(output.get("grounding") or [])
        rec["ok"] = status == "completed"
        if status != "completed":
            rec["error"] = f"run {status}: {final.get('stopReason')}"
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["email_hit"] = rec.get("email_hit", False)
        rec["phone_hit"] = rec.get("phone_hit", False)
    finally:
        rec["latency_s"] = round(time.monotonic() - started, 2)
    return rec


def _print_table(records: List[Dict[str, Any]]) -> None:
    print("\n[spike] === PER-CANDIDATE RESULTS ===")
    header = f"{'candidate':<26} {'email':>5} {'phone':>5} {'lat(s)':>7} {'cost$':>6}  status"
    print(header)
    print("-" * len(header))
    for r in records:
        who = (r.get("full_name") or r.get("candidate_id") or r.get("linkedin_url") or "?")
        who = str(who)[:25]
        email = "Y" if r.get("email_hit") else "n"
        phone = "Y" if r.get("phone_hit") else "n"
        lat = r.get("latency_s", "-")
        cost = r.get("cost_dollars", "-")
        status = "ok" if r.get("ok") else f"ERR {str(r.get('error', ''))[:38]}"
        print(f"{who:<26} {email:>5} {phone:>5} {lat!s:>7} {cost!s:>6}  {status}")


def _print_aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(records)
    ok = [r for r in records if r.get("ok")]
    email_hits = sum(1 for r in records if r.get("email_hit"))
    phone_hits = sum(1 for r in records if r.get("phone_hit"))
    both = sum(1 for r in records if r.get("email_hit") and r.get("phone_hit"))
    lats = sorted(float(r["latency_s"]) for r in records if isinstance(r.get("latency_s"), (int, float)))
    costs = [float(r["cost_dollars"]) for r in records if isinstance(r.get("cost_dollars"), (int, float))]

    def pct(x: int) -> str:
        return f"{(100.0 * x / n):.0f}%" if n else "-"

    median = round(statistics.median(lats), 1) if lats else None
    p95 = round(lats[min(len(lats) - 1, int(0.95 * len(lats)))], 1) if lats else None

    summary = {
        "candidates": n,
        "ok": len(ok),
        "errors": n - len(ok),
        "email_hit_rate": pct(email_hits),
        "phone_hit_rate": pct(phone_hits),
        "both_hit_rate": pct(both),
        "latency_median_s": median,
        "latency_p95_s": p95,
        "latency_max_s": round(lats[-1], 1) if lats else None,
        "cost_total_dollars": round(sum(costs), 3) if costs else None,
        "cost_avg_dollars": round(sum(costs) / len(costs), 3) if costs else None,
    }
    print("\n[spike] === AGGREGATE ===")
    for k, v in summary.items():
        print(f"  {k:<20}: {v}")
    print(
        "\n[spike] GATE: proceed to Phase 2 only if email/phone hit-rate is "
        "meaningful AND latency is acceptable for a user-initiated Step-5 call."
    )
    return summary


def _collect_candidates(args: argparse.Namespace) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    if args.input:
        raw = json.loads(Path(args.input).read_text())
        if not isinstance(raw, list):
            raise SystemExit("--input JSON must be a list of candidate objects")
        for obj in raw:
            candidates.append(
                {
                    "candidate_id": obj.get("candidate_id"),
                    "linkedin_url": (obj.get("linkedin_url") or "").strip(),
                    "full_name": (obj.get("full_name") or "").strip(),
                    "company_name": (obj.get("company_name") or "").strip(),
                    "expected_email": (obj.get("expected_email") or "").strip(),
                    "expected_phone": (obj.get("expected_phone") or "").strip(),
                }
            )

    for cid in (args.candidate_id or []):
        row = _lookup_candidate(cid)
        if not row:
            print(f"[spike] WARN: no sourced_candidates row for candidate_id={cid}", file=sys.stderr)
            continue
        candidates.append(_candidate_from_row(cid, row))

    for url in (args.linkedin_url or []):
        candidates.append(
            {
                "candidate_id": None,
                "linkedin_url": (url or "").strip(),
                "full_name": (args.full_name or "").strip(),
                "company_name": (args.company_name or "").strip(),
                "expected_email": "",
                "expected_phone": "",
            }
        )

    # Need at least a URL or a name to build a meaningful query.
    return [c for c in candidates if c.get("linkedin_url") or c.get("full_name")]


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--linkedin-url", action="append", help="Repeatable LinkedIn URL.")
    p.add_argument("--candidate-id", action="append", help="Repeatable sourced_candidates id.")
    p.add_argument("--input", help="JSON file: list of candidate objects.")
    p.add_argument("--full-name", default=None, help="Name for a single --linkedin-url.")
    p.add_argument("--company-name", default=None, help="Company for a single --linkedin-url.")
    p.add_argument(
        "--effort", default="low",
        choices=["low", "medium", "high", "xhigh", "auto"],
        help="Agent effort (default low).",
    )
    p.add_argument("--timeout", type=int, default=120, help="Poll timeout s (default 120).")
    p.add_argument("--poll-interval", type=int, default=4, help="Poll interval s (default 4).")
    p.add_argument("--dry-run", action="store_true", help="Print plan and exit.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)

    if not EXA_API_KEY:
        print("[spike] ERROR: EXA_API_KEY is not set in the environment/.env", file=sys.stderr)
        return 2

    candidates = _collect_candidates(args)
    if not candidates:
        print(
            "[spike] ERROR: no candidates. Pass --linkedin-url, --candidate-id, or --input.",
            file=sys.stderr,
        )
        return 2

    print(f"[spike] EXA_API_KEY      : set")
    print(f"[spike] engine           : Exa Agent API ({AGENT_BETA})")
    print(f"[spike] candidates       : {len(candidates)}")
    print(f"[spike] effort           : {args.effort}")
    print(f"[spike] timeout/poll     : {args.timeout}s / {args.poll_interval}s")
    for c in candidates:
        print(f"[spike]   - {c.get('full_name') or c.get('candidate_id') or ''} :: {c.get('linkedin_url')}")

    if args.dry_run:
        print("[spike] --dry-run set — exiting without calling Exa.")
        return 0

    records: List[Dict[str, Any]] = []
    started_at_utc = _utc_iso()
    with httpx.Client(timeout=30.0) as client:
        for c in candidates:
            print(f"\n[spike] probing: {c.get('full_name') or c.get('linkedin_url')}")
            rec = probe_one(client, c, args)
            records.append(rec)
            print(
                f"[spike]   -> email_hit={rec.get('email_hit')} phone_hit={rec.get('phone_hit')} "
                f"latency={rec.get('latency_s')}s cost=${rec.get('cost_dollars')}"
            )
            if rec.get("email"):
                print(f"[spike]      email: {rec['email']}")
            if rec.get("phone"):
                print(f"[spike]      phone: {rec['phone']}")
            if not rec.get("ok"):
                print(f"[spike]      ERROR: {rec.get('error')}")
    ended_at_utc = _utc_iso()

    _print_table(records)
    summary = _print_aggregate(records)

    out_doc = {
        "spike": {
            "client": "airecruiter exa-agent contact-enrichment spike v2",
            "engine": f"agent-api {AGENT_BETA}",
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "effort": args.effort,
            "timeout_s": args.timeout,
            "poll_interval_s": args.poll_interval,
        },
        "summary": summary,
        "records": records,
    }
    out_dir = APPS_API_DIR / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"exa_contact_spike_{stamp}.json"
    out_path.write_text(json.dumps(out_doc, indent=2, default=str))
    print(f"\n[spike] wrote evidence file: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
