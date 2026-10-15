"""Weekly, read-only reconcile of Exa Agent spend against the free run list.

Run from ``apps/api/`` (EXA_API_KEY from the environment or apps/api/.env):

    python scripts/exa_runs_ledger.py                     # trailing 8 days, grouped
    python scripts/exa_runs_ledger.py --seen seen.txt     # + runs no ExaCall event saw
    python scripts/exa_runs_ledger.py --json              # machine-readable

``GET https://api.exa.ai/agent/runs`` costs nothing and lists every run of
the Exa TEAM (every key: PROD, QA, dev, scripts). It is read with raw httpx,
not the SDK list: exa_py 2.13's ListAgentRunsResponse validates every run,
and one fractional ``agentComputeUnits`` (0.4) loses the whole page.

Privacy: only ``id / status / createdAt / costDollars / usage / metadata``
are kept from each run. ``request.query`` (names, LinkedIn URLs) and
``output`` (emails, phones) are dropped as soon as a page arrives and are
never printed.

Runs are grouped by their ``metadata`` (env / surface / tier / trigger / arm,
set by services/exa_telemetry.agent_metadata). Runs without metadata are
"untagged": Pass B through the SDK, scripts, anything before metadata shipped,
or a worker that latched metadata off after a 400.

``--seen FILE``: one id per line, run ids and/or ``call_id``s of runs New
Relic saw finish, exported with

    SELECT uniques(exa_run_id, 10000), uniques(call_id, 10000) FROM ExaCall
      WHERE endpoint = 'agent_run' AND outcome = 'completed' SINCE 8 days ago

A billed, finished run whose id and call_id are both missing from it is an
ORPHAN: paid for, result never kept (a sourcing timeout, a late watch killed
by a restart, a lost create). Runs younger than ``--grace-min`` are skipped.

Exit code 0; 2 when the key is missing or the API refuses the listing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

import httpx

RUNS_URL = "https://api.exa.ai/agent/runs"
BETA = "agent-2026-05-07"
TERMINAL = {"completed", "failed", "cancelled"}
GROUP_KEYS = ("env", "surface", "tier", "trigger", "arm")
_KEEP = ("id", "status", "createdAt", "costDollars", "usage", "metadata")


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _cost(run: Dict[str, Any]) -> float:
    try:
        return float((run.get("costDollars") or {}).get("total") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def slim(run: Dict[str, Any]) -> Dict[str, Any]:
    """The fields this script may keep; everything else (query, output) is dropped."""
    out = {k: run.get(k) for k in _KEEP if k in run}
    meta = out.get("metadata")
    out["metadata"] = {str(k): str(v)[:64] for k, v in meta.items()} if isinstance(meta, dict) else {}
    return out


def fetch_runs(
    client: Any,
    api_key: str,
    *,
    days: float = 8.0,
    max_pages: int = 600,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Slimmed runs created in the trailing ``days`` (the list is newest first)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    headers = {"x-api-key": api_key, "Exa-Beta": BETA, "Accept": "application/json"}
    runs: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: Dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(RUNS_URL, headers=headers, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"Exa run list answered {resp.status_code}")
        page = resp.json() or {}
        reached_cutoff = False
        for raw in page.get("data") or []:
            run = slim(raw)
            created = _parse_ts(run.get("createdAt"))
            if created is not None and created < cutoff:
                reached_cutoff = True
                continue
            runs.append(run)
        cursor = page.get("nextCursor")
        if reached_cutoff or not page.get("hasMore") or not cursor:
            break
    return runs


def _group(run: Dict[str, Any]) -> tuple:
    meta = run.get("metadata") or {}
    if not meta:
        return ("untagged",)
    return tuple(meta.get(k, "-") for k in GROUP_KEYS)


def summarize(
    runs: Iterable[Dict[str, Any]],
    seen: Optional[Set[str]] = None,
    *,
    grace_min: float = 30.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Totals per metadata group, and (with ``seen``) the orphaned runs."""
    now = now or datetime.now(timezone.utc)
    groups: Dict[tuple, Dict[str, Any]] = defaultdict(lambda: {"runs": 0, "usd": 0.0, "by_status": defaultdict(int)})
    orphans: List[Dict[str, Any]] = []
    total = {"runs": 0, "usd": 0.0}
    for run in runs:
        cost = _cost(run)
        g = groups[_group(run)]
        g["runs"] += 1
        g["usd"] += cost
        g["by_status"][str(run.get("status") or "?")] += 1
        total["runs"] += 1
        total["usd"] += cost
        if seen is None or cost <= 0 or run.get("status") not in TERMINAL:
            continue
        created = _parse_ts(run.get("createdAt"))
        if created is not None and now - created < timedelta(minutes=grace_min):
            continue
        call_id = (run.get("metadata") or {}).get("call_id")
        if run.get("id") in seen or (call_id and call_id in seen):
            continue
        orphans.append({
            "id": run.get("id"), "createdAt": run.get("createdAt"), "status": run.get("status"),
            "usd": round(cost, 4), "call_id": call_id, "group": "/".join(_group(run)),
        })
    rows = sorted(
        ({"group": "/".join(k), "runs": v["runs"], "usd": round(v["usd"], 4), "by_status": dict(v["by_status"])}
         for k, v in groups.items()),
        key=lambda r: -r["usd"],
    )
    out: Dict[str, Any] = {
        "runs": total["runs"], "usd": round(total["usd"], 4), "group_keys": "/".join(GROUP_KEYS), "groups": rows,
    }
    if seen is not None:
        out["orphans"] = {
            "runs": len(orphans), "usd": round(sum(o["usd"] for o in orphans), 4),
            "by_group": dict(sorted(_count_by(orphans, "group").items(), key=lambda kv: -kv[1])),
            "sample": orphans[:25],
        }
    return out


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for row in rows:
        out[str(row.get(key))] += 1
    return dict(out)


def _read_seen(path: str) -> Set[str]:
    with open(path, encoding="utf-8") as fh:
        return {line.strip().strip('",') for line in fh if line.strip()}


def _print_report(report: Dict[str, Any], days: float) -> None:
    print(f"Exa Agent runs, trailing {days:g} days: {report['runs']} runs, ${report['usd']:.2f}")
    print(f"\n{'group (' + report['group_keys'] + ')':<60} {'runs':>7} {'$':>10}  status")
    for row in report["groups"]:
        print(f"{row['group']:<60} {row['runs']:>7} {row['usd']:>10.2f}  {row['by_status']}")
    orphans = report.get("orphans")
    if orphans is not None:
        print(f"\nOrphans (billed, finished, no completed ExaCall): {orphans['runs']} runs, ${orphans['usd']:.2f}")
        for group, n in orphans["by_group"].items():
            print(f"  {group:<58} {n:>7}")
        for o in orphans["sample"]:
            print(f"  {o['id']}  {o['createdAt']}  {o['status']:<9} ${o['usd']:.4f}  call_id={o['call_id'] or '-'}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=float, default=8.0, help="trailing window (default 8)")
    parser.add_argument("--seen", help="file of run ids / call_ids New Relic saw complete (see module doc)")
    parser.add_argument("--grace-min", type=float, default=30.0, help="skip runs younger than this in the orphan check")
    parser.add_argument("--max-pages", type=int, default=600)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    except ImportError:
        pass
    api_key = (os.getenv("EXA_API_KEY") or "").strip()
    if not api_key:
        print("EXA_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=30.0) as client:
            runs = fetch_runs(client, api_key, days=args.days, max_pages=args.max_pages)
    except Exception as exc:  # noqa: BLE001 - report and stop; nothing to undo
        print(f"Could not list Exa runs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    report = summarize(runs, _read_seen(args.seen) if args.seen else None, grace_min=args.grace_min)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report, args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
