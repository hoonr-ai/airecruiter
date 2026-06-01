"""One-off: dump JobDiva CandidatesDetail for a single candidate and verify phone extraction.

Usage:
    cd apps/api
    set -a && source .env && set +a
    venv/bin/python -m scripts.probe_candidate_phone 12789425834216
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


async def main(candidate_id: str) -> int:
    from services.jobdiva import JobDivaService, _get_candidate_phone, _get_candidate_email

    jd = JobDivaService()
    token = await jd.authenticate()
    if not token:
        print("✗ JobDiva auth failed", file=sys.stderr)
        return 1

    detail_map = await jd._fetch_candidate_details_batch(token, [candidate_id])
    record = detail_map.get(str(candidate_id))
    if not record:
        print(f"✗ no CandidatesDetail record returned for {candidate_id}", file=sys.stderr)
        return 1

    print("── phone-shaped fields in raw response ──")
    for k, v in record.items():
        if "phone" in str(k).lower() and v:
            print(f"  {k!r} = {v!r}")

    print()
    print(f"_get_candidate_phone(record) → {_get_candidate_phone(record)!r}")
    print(f"_get_candidate_email(record) → {_get_candidate_email(record)!r}")

    return 0


if __name__ == "__main__":
    cid = sys.argv[1] if len(sys.argv) > 1 else "12789425834216"
    sys.exit(asyncio.run(main(cid)))
