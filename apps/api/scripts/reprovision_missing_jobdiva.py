#!/usr/bin/env python3
"""Re-provision launched candidates that never got a JobDiva profile.

CreateJobApplicationWithResume used to 500 whenever résumé text held a bare
``%`` ("grew revenue 30% a year"), so those candidates were launched in PAIR
but never created in JobDiva. Once the escaping fix is deployed, this finds
every job with launched candidates still missing ``jobdiva_candidate_id``
and re-runs the same provisioning as POST /engage/re-provision.

    cd apps/api
    venv/bin/python -m scripts.reprovision_missing_jobdiva              # dry run: jobs + counts
    venv/bin/python -m scripts.reprovision_missing_jobdiva --apply      # re-provision
    venv/bin/python -m scripts.reprovision_missing_jobdiva --days 14 --job 26-06182 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

_SQL = """
    SELECT eia.jobdiva_id AS job_id, COUNT(DISTINCT sc.candidate_id) AS missing
    FROM sourced_candidates sc
    JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
    WHERE eia.created_at >= NOW() - (%(days)s || ' days')::interval
      AND sc.jobdiva_id = eia.jobdiva_id
      AND COALESCE(sc.data->>'jobdiva_candidate_id', '') = ''
      AND (%(job)s IS NULL OR eia.jobdiva_id = %(job)s)
    GROUP BY eia.jobdiva_id
    ORDER BY missing DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7, help="launches in the last N days (default 7)")
    ap.add_argument("--job", default=None, help="limit to one JobDiva job id / reference")
    ap.add_argument("--apply", action="store_true", help="re-provision (default: dry run)")
    args = ap.parse_args()

    from core.db import get_db_connection

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL, {"days": str(args.days), "job": args.job})
            jobs = cur.fetchall()
    finally:
        conn.close()

    total = sum(n for _, n in jobs)
    print(f"{len(jobs)} job(s), {total} candidate(s) missing a JobDiva profile (last {args.days} d)")
    for job_id, n in jobs:
        print(f"  {job_id}: {n}")
    if not args.apply or not jobs:
        return 0

    from routers.engagement import ReProvisionRequest, re_provision_candidates

    for job_id, _ in jobs:
        try:
            res = asyncio.run(re_provision_candidates(ReProvisionRequest(job_id=str(job_id))))
            print(f"  {job_id}: {res}")
        except Exception as exc:
            print(f"  {job_id}: FAILED {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
