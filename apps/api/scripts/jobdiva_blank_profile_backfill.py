"""Repair JobDiva profiles Launch PAIR created blank (services/jobdiva_profile_backfill.py).

DRY RUN by default: reads JobDiva and prints what each profile would get (a
résumé upload and/or which blank fields), writing nothing. ``--apply`` performs
the writes -- start with ``--jobdiva-ids`` for one or two profiles and check
them in JobDiva before a wider run.

Candidates come from ``sourced_candidates`` in DATABASE_URL (the rows PAIR still
holds for the people it created in JobDiva). Output carries ids, statuses and
field NAMES only -- never a name, email or phone.

Run (from apps/api, with JobDiva credentials + DATABASE_URL in the environment):
    python -m scripts.jobdiva_blank_profile_backfill --jobdiva-ids 20873925397812 20873935786476
    python -m scripts.jobdiva_blank_profile_backfill --jobdiva-ids 20873925397812 --apply
    python -m scripts.jobdiva_blank_profile_backfill --limit 25 [--job-id 26-15314] [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from psycopg2.extras import RealDictCursor  # noqa: E402

from routers._helpers import get_db_connection  # noqa: E402
from services.jobdiva_profile_backfill import (  # noqa: E402
    BACKFILL_CANDIDATES_SQL,
    backfill_blank_profile,
)


def load_rows(jobdiva_ids: Optional[List[str]], job_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(BACKFILL_CANDIDATES_SQL, {
                "jobdiva_ids": jobdiva_ids or None, "job_ids": [job_id] if job_id else None,
                "include_checked": True, "limit": limit,
            })
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


async def run(rows: List[Dict[str, Any]], apply: bool) -> List[Dict[str, Any]]:
    reports = []
    for row in rows:  # sequential: JobDiva's BI résumé endpoints rate-limit hard
        reports.append(await backfill_blank_profile(row, apply=apply))
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobdiva-ids", nargs="*", help="only these JobDiva candidate ids")
    parser.add_argument("--job-id", help="only rows of this job (sourced_candidates.jobdiva_id)")
    parser.add_argument("--limit", type=int, default=2, help="max profiles (default 2)")
    parser.add_argument("--apply", action="store_true", help="perform the writes (default: dry run)")
    args = parser.parse_args()

    rows = load_rows(args.jobdiva_ids, args.job_id, max(1, args.limit))
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(rows)} profile(s)")
    reports = asyncio.run(run(rows, args.apply))
    for report in reports:
        # candidate_id (a LinkedIn slug) and the résumé filename carry the
        # person's name: keep both off stdout.
        shown = {k: v for k, v in report.items() if k != "candidate_id"}
        if isinstance(shown.get("upload"), dict):
            shown["upload"] = {k: v for k, v in shown["upload"].items() if k != "filename"}
        print(json.dumps(shown, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
