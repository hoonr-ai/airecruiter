#!/usr/bin/env python3
"""Seed contact_enrichment_cache from past paid lookups (services/contact_cache_backfill.py).

The API runs this once per database by itself (first cache use after deploy).
Use the script to preview it, or to re-run it after the marker exists.

    cd apps/api
    venv/bin/python -m scripts.backfill_contact_cache            # dry run: counts only
    venv/bin/python -m scripts.backfill_contact_cache --apply    # write
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APPS_API_DIR = Path(__file__).resolve().parent.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write to the cache (default: dry run)")
    args = ap.parse_args()

    from core.db import get_db_connection
    from services import contact_cache, contact_cache_backfill

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(contact_cache.CREATE_SQL)
        summary = contact_cache_backfill.run(conn, apply=args.apply)
        if args.apply:
            with conn.cursor() as cur:
                cur.execute(contact_cache_backfill.MARK_DONE_SQL, {"slug": contact_cache_backfill.DONE_SLUG})
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
