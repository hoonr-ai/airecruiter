"""Run a JobDiva TalentSearch boolean and print top candidates.

Default query (passed verbatim — JobDiva resolves the geo and IN clauses):

    (MAINFRAME) AND IN (CA, US) Within 30 miles of 75019

Optionally also runs JobAgentSearch for a given --job-id and prints a
TalentSearch-vs-JobAgent overlap summary (mirrors apps/api/scripts/sourcing_poc.py).

Run:
    cd apps/api
    source .env
    venv/bin/python -m scripts.jobdiva_mainframe_search --dry-run
    venv/bin/python -m scripts.jobdiva_mainframe_search
    venv/bin/python -m scripts.jobdiva_mainframe_search --limit 50
    venv/bin/python -m scripts.jobdiva_mainframe_search --job-id 26-12345
    venv/bin/python -m scripts.jobdiva_mainframe_search --query "(COBOL) Within 30 miles of 75019" --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from core import (  # noqa: E402
    JOBDIVA_API_URL,
    JOBDIVA_CLIENT_ID,
    JOBDIVA_USERNAME,
)
from services.jobdiva import jobdiva_service  # noqa: E402


DEFAULT_QUERY = "(MAINFRAME) AND IN (CA, US) Within 30 miles of 75019"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: Any, width: int) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\n", " ").replace("\t", " ").strip()
    if len(s) <= width:
        return s.ljust(width)
    return (s[: max(0, width - 1)] + "…").ljust(width)


def _skills_preview(skills: Any, n: int = 4) -> str:
    if not skills:
        return ""
    if isinstance(skills, str):
        items = [skills]
    elif isinstance(skills, list):
        items = [str(x) for x in skills if x is not None]
    else:
        items = [str(skills)]
    head = items[:n]
    extra = len(items) - len(head)
    s = ", ".join(head)
    if extra > 0:
        s += f" (+{extra})"
    return s


def _cid(c: Dict[str, Any]) -> str:
    return str(c.get("candidate_id") or c.get("id") or "")


def _score(c: Dict[str, Any]) -> int:
    raw = c.get("match_score") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def print_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[search] No candidates returned.")
        return
    # Column widths chosen to keep total ~160 cols.
    header = (
        f"{'#':>3}  "
        f"{'cand_id':<12}  "
        f"{'score':>5}  "
        f"{'yrs':>3}  "
        f"{'name':<24}  "
        f"{'location':<28}  "
        f"{'title':<32}  "
        f"{'email':<5}  "
        f"{'skills':<40}"
    )
    print(header)
    print("-" * len(header))
    for i, c in enumerate(rows, start=1):
        line = (
            f"{i:>3}  "
            f"{_truncate(_cid(c), 12)}  "
            f"{_score(c):>5}  "
            f"{_truncate(c.get('experience_years'), 3)}  "
            f"{_truncate(c.get('name'), 24)}  "
            f"{_truncate(c.get('location'), 28)}  "
            f"{_truncate(c.get('title'), 32)}  "
            f"{'yes' if c.get('email') else 'no':<5}  "
            f"{_truncate(_skills_preview(c.get('skills')), 40)}"
        )
        print(line)


async def run_search(args: argparse.Namespace) -> int:
    print(f"[search] query              : {args.query!r}")
    print(f"[search] limit              : {args.limit}")
    print(f"[search] page_number        : {args.page_number}")
    print(f"[search] require_resume     : {args.require_resume}")
    print(f"[search] post-filter state  : {args.state!r}")
    print(f"[search] post-filter city   : {args.city_contains!r}")
    print(f"[search] JOBDIVA_API_URL    : {JOBDIVA_API_URL}")
    print(f"[search] JOBDIVA_USERNAME   : {JOBDIVA_USERNAME}")
    print(f"[search] JOBDIVA_CLIENT_ID  : {JOBDIVA_CLIENT_ID}")
    print(f"[search] job_id (JobAgent)  : {args.job_id!r}" if args.job_id else "[search] job_id (JobAgent)  : (none — comparison skipped)")

    if args.dry_run:
        print("[search] --dry-run set — exiting without sending any requests.")
        return 0

    started_at = _utc_iso()

    # 1. TalentSearch — boolean string verbatim, no structured state/country filter.
    print("\n[search] calling TalentSearch (boolean) …")
    talent = await jobdiva_service.search_candidates(
        skills=[],
        location="",
        boolean_string=args.query,
        countries=[],
        states=[],
        limit=args.limit,
        page_number=args.page_number,
        require_resume=args.require_resume,
    )
    print(f"[search] TalentSearch returned {len(talent)} candidates")

    # Client-side post-filter — JobDiva's TalentSearch endpoint silently ignores
    # structured countries/states/zip/radius, so anything narrower than keyword
    # has to happen here.
    pre_filter_count = len(talent)
    if args.state:
        want_state = args.state.strip().upper()
        talent = [
            c for c in talent
            if (str(c.get("state") or "").strip().upper() == want_state)
            or (str(c.get("work_state") or "").strip().upper() == want_state)
        ]
        print(f"[search] after --state {want_state}: {len(talent)} (was {pre_filter_count})")
    if args.city_contains:
        wanted_cities = [s.strip().lower() for s in args.city_contains.split(",") if s.strip()]
        before = len(talent)
        talent = [
            c for c in talent
            if any(
                w in (str(c.get("city") or "") + " " + str(c.get("work_city") or "") + " " + str(c.get("location") or "")).lower()
                for w in wanted_cities
            )
        ]
        print(f"[search] after --city-contains {wanted_cities!r}: {len(talent)} (was {before})")

    talent.sort(key=lambda c: (-_score(c), _cid(c)))
    talent_top = talent[: args.limit]

    # 2. Optional JobAgent comparison.
    jobagent_block: Optional[Dict[str, Any]] = None
    if args.job_id:
        print(f"\n[search] calling JobAgentSearch for job_id={args.job_id!r} …")
        ja = await jobdiva_service.search_via_job_agent(
            job_id=args.job_id,
            resume_count=max(args.limit, 50),
            require_resume=args.require_resume,
        )
        ja_candidates = (ja or {}).get("candidates") or []
        print(
            f"[search] JobAgent returned {len(ja_candidates)} candidates "
            f"(criteria_unconfigured={(ja or {}).get('criteria_unconfigured')}, "
            f"resolved_jobdiva_id={(ja or {}).get('resolved_jobdiva_id')})"
        )
        talent_ids = {_cid(c) for c in talent if _cid(c)}
        ja_ids = {_cid(c) for c in ja_candidates if _cid(c)}
        overlap = sorted(talent_ids & ja_ids)
        talent_only = sorted(talent_ids - ja_ids)
        jobagent_only = sorted(ja_ids - talent_ids)
        jobagent_block = {
            "job_id": args.job_id,
            "resolved_jobdiva_id": (ja or {}).get("resolved_jobdiva_id"),
            "criteria_unconfigured": (ja or {}).get("criteria_unconfigured"),
            "talent_count": len(talent_ids),
            "jobagent_count": len(ja_ids),
            "overlap_count": len(overlap),
            "talent_only_ids": talent_only,
            "jobagent_only_ids": jobagent_only,
            "overlap_ids": overlap,
            "candidates": ja_candidates,
        }

    # 3. Print table.
    print(f"\n[search] === TOP {min(args.limit, len(talent_top))} candidates (sorted by match_score desc) ===\n")
    print_table(talent_top)

    if jobagent_block is not None:
        print("\n[search] === TalentSearch vs JobAgent overlap ===")
        print(f"[search]   talent_only_ids  ({len(jobagent_block['talent_only_ids'])}): {jobagent_block['talent_only_ids'][:25]}{' …' if len(jobagent_block['talent_only_ids']) > 25 else ''}")
        print(f"[search]   jobagent_only_ids ({len(jobagent_block['jobagent_only_ids'])}): {jobagent_block['jobagent_only_ids'][:25]}{' …' if len(jobagent_block['jobagent_only_ids']) > 25 else ''}")
        print(f"[search]   overlap_ids       ({len(jobagent_block['overlap_ids'])}): {jobagent_block['overlap_ids'][:25]}{' …' if len(jobagent_block['overlap_ids']) > 25 else ''}")

    # 4. Persist full payload as evidence.
    ended_at = _utc_iso()
    out_dir = APPS_API_DIR / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"jobdiva_mainframe_{stamp}.json"
    out_doc: Dict[str, Any] = {
        "run": {
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "jobdiva_api_url": JOBDIVA_API_URL,
            "jobdiva_username": JOBDIVA_USERNAME,
            "jobdiva_client_id": JOBDIVA_CLIENT_ID,
            "query": args.query,
            "limit": args.limit,
            "page_number": args.page_number,
            "require_resume": args.require_resume,
            "job_id": args.job_id,
        },
        "talent_search": {
            "returned": len(talent),
            "candidates": talent,
        },
        "jobagent": jobagent_block,
    }
    out_path.write_text(json.dumps(out_doc, indent=2, default=str))
    print(f"\n[search] wrote evidence file: {out_path}")
    return 0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--query", default=DEFAULT_QUERY, help=f"Boolean string passed verbatim to JobDiva TalentSearch. Default: {DEFAULT_QUERY!r}")
    p.add_argument("--limit", type=int, default=25, help="Max candidates to return (default: 25).")
    p.add_argument("--page-number", type=int, default=0, help="0-indexed page (default: 0).")
    p.add_argument("--require-resume", dest="require_resume", action="store_true", default=True, help="Drop results without a resume (default: True).")
    p.add_argument("--allow-no-resume", dest="require_resume", action="store_false", help="Include candidates without resumes.")
    p.add_argument("--job-id", default=None, help="Optional JobDiva job_id; if given, also run JobAgentSearch and print overlap.")
    p.add_argument("--state", default=None, help="Post-filter results to candidates in this state code (e.g. TX). JobDiva's TalentSearch endpoint ignores structured state filters, so we filter client-side.")
    p.add_argument("--city-contains", dest="city_contains", default=None, help="Post-filter to candidates whose city contains this substring (case-insensitive). Use commas for OR, e.g. 'dallas,plano,frisco,irving,coppell,lewisville'.")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and exit without sending requests.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(run_search(args))


if __name__ == "__main__":
    sys.exit(main())
