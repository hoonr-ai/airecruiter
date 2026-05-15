"""End-to-end production verification — hit the real /candidates/search endpoint
and check whether the 3 target candidate IDs/emails reach the UI stream.

The endpoint streams NDJSON: each line is a JSON event of shape
  {"type": "candidate", "data": {candidate_id, email, name, source, ...}}
plus a few status / summary events. We parse the stream as it arrives,
collect every `candidate` event, and compare the result set against the
target emails + candidate IDs we already know JobDiva has.

Run:
    cd apps/api
    source .env
    # API must be running on 127.0.0.1:8765 (or pass --api-base)

    # 1. Verify with the production sourcing_config (strict defaults):
    venv/bin/python -m scripts.sourcing_debug_e2e \\
        --criteria scripts/sourcing_debug_26-11245.json \\
        --target-emails adarshkt2025@gmail.com,sohitha716@gmail.com,vsne1519@gmail.com \\
        --target-ids 19768619487946,20302354911856,20088668732918 \\
        --out tmp/sourcing_debug/26-11245.e2e.strict

    # 2. Then flip core/sourcing_config.py toggles → restart API → re-run with
    #    --out tmp/sourcing_debug/26-11245.e2e.lenient
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


def _stream_candidates(
    api_base: str,
    payload: Dict[str, Any],
    out_dir: Path,
    timeout_seconds: float,
) -> Dict[str, Any]:
    """POST the criteria to /candidates/search, save the raw NDJSON,
    and return a structured summary of the events received."""
    url = f"{api_base.rstrip('/')}/candidates/search"
    out_dir.mkdir(parents=True, exist_ok=True)

    events: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []
    raw_lines_path = out_dir / "stream.ndjson"
    raw_lines = raw_lines_path.open("w")

    t0 = time.time()
    print(f"▶ POST {url}")
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    body = response.read().decode(errors="replace")
                    raw_lines.close()
                    raise RuntimeError(
                        f"HTTP {response.status_code} from {url}: {body[:500]}"
                    )
                for line in response.iter_lines():
                    if not line:
                        continue
                    raw_lines.write(line + "\n")
                    raw_lines.flush()
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events.append(evt)
                    if evt.get("type") == "candidate":
                        candidates.append(evt.get("data") or {})
                        # Print progress every 25 candidates
                        if len(candidates) % 25 == 0:
                            print(f"   …received {len(candidates)} candidates so far")
    finally:
        raw_lines.close()

    elapsed_s = round(time.time() - t0, 2)
    print(f"✓ stream done: {len(candidates)} candidates in {elapsed_s}s")

    summary = {
        "url": url,
        "elapsed_seconds": elapsed_s,
        "event_count": len(events),
        "candidate_count": len(candidates),
        "event_types": _count_types(events),
        "candidate_summaries": [_summarize(c) for c in candidates],
    }
    return summary


def _count_types(events: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in events:
        t = e.get("type") or "(none)"
        counts[t] = counts.get(t, 0) + 1
    return counts


def _summarize(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidate_id": str(c.get("candidate_id") or c.get("id") or ""),
        "email": str(c.get("email") or "").strip().lower(),
        "name": c.get("name") or "",
        "state": c.get("state") or "",
        "title": (c.get("title") or "")[:80],
        "source": c.get("source") or "",
        "match_score": c.get("match_score"),
    }


def _check_targets(
    summary: Dict[str, Any],
    target_emails: List[str],
    target_ids: List[str],
) -> Dict[str, Any]:
    by_email = {c["email"]: c for c in summary["candidate_summaries"] if c["email"]}
    by_id = {c["candidate_id"]: c for c in summary["candidate_summaries"] if c["candidate_id"]}

    result = {
        "by_email": {e: by_email.get(e) for e in target_emails},
        "by_id": {i: by_id.get(i) for i in target_ids},
        "found_count": 0,
        "missing_emails": [],
        "missing_ids": [],
    }
    for e in target_emails:
        if by_email.get(e):
            result["found_count"] += 1
        else:
            result["missing_emails"].append(e)
    for i in target_ids:
        if by_id.get(i):
            pass  # count once via email
        else:
            result["missing_ids"].append(i)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--criteria", required=True)
    ap.add_argument("--target-emails", required=True, help="Comma-separated.")
    ap.add_argument("--target-ids", default="", help="Comma-separated JobDiva candidate_ids (optional).")
    ap.add_argument("--api-base", default="http://127.0.0.1:8765")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=900.0, help="Stream timeout seconds; default 900 (15 min).")
    args = ap.parse_args()

    payload = json.loads(Path(args.criteria).expanduser().read_text())
    target_emails = [e.strip().lower() for e in args.target_emails.split(",") if e.strip()]
    target_ids = [i.strip() for i in args.target_ids.split(",") if i.strip()]

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = _stream_candidates(args.api_base, payload, out_dir, args.timeout)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    check = _check_targets(summary, target_emails, target_ids)
    (out_dir / "target_check.json").write_text(json.dumps(check, indent=2, default=str))

    print(f"\nTarget check:")
    print(f"  emails found:  {check['found_count']}/{len(target_emails)}")
    for e in target_emails:
        c = check["by_email"].get(e)
        if c:
            print(f"    ✓ {e} → id={c['candidate_id']} name={c['name']!r} source={c['source']!r} score={c['match_score']}")
        else:
            print(f"    ✗ {e} NOT in stream")
    if target_ids:
        print(f"  IDs found:     {len(target_ids) - len(check['missing_ids'])}/{len(target_ids)}")
        for i in target_ids:
            c = check["by_id"].get(i)
            if c:
                print(f"    ✓ id={i} → email={c['email']!r} source={c['source']!r}")
            else:
                print(f"    ✗ id={i} NOT in stream")
    print(f"\nWrote {out_dir / 'summary.json'} + {out_dir / 'target_check.json'} + {out_dir / 'stream.ndjson'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
