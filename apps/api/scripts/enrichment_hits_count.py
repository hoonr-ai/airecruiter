"""Count candidates that received phone/email via enrichment, over a recent window.

Read-only stats script. Hits ``sourced_candidates`` with a single aggregate
query and prints a one-shot table:

    Total candidates updated in window:        N
    With phone:                                X (% of total)
    With email:                                Y (% of total)
    With both:                                 Z (% of total)
    --
    Went through /enrich-contact (data has key): M
      Provider zoominfo:                       a (% of attempted)
      Provider apollo:                         b (% of attempted)
      Provider none:                           c (% of attempted)
    Phone present on enriched rows:            p (% of attempted)
    Email present on enriched rows:            e (% of attempted)

The "went through enrich-contact" cohort is identified by
``data ? 'zoominfo_contact_enrichment'``, which the enrich endpoint sets
on every persisted enrichment attempt
(see ``routers/candidates.py:_enrich_candidate_contact_impl``).

Run:
    cd apps/api
    venv/bin/python -m scripts.enrichment_hits_count
    venv/bin/python -m scripts.enrichment_hits_count --window-days 14 --source linkedin
    venv/bin/python -m scripts.enrichment_hits_count --jobdiva-id 26-05172
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))

from routers._helpers import get_db_connection  # noqa: E402


QUERY = """
SELECT
  COUNT(*)                                                                                      AS total,
  COUNT(*) FILTER (WHERE phone IS NOT NULL AND phone <> '')                                     AS with_phone,
  COUNT(*) FILTER (WHERE email IS NOT NULL AND email <> '')                                     AS with_email,
  COUNT(*) FILTER (
        WHERE phone IS NOT NULL AND phone <> ''
          AND email IS NOT NULL AND email <> ''
  )                                                                                             AS with_both,
  COUNT(*) FILTER (WHERE data ? 'zoominfo_contact_enrichment')                                  AS attempted_enrich,
  COUNT(*) FILTER (WHERE data->'zoominfo_contact_enrichment'->>'provider' = 'zoominfo')         AS provider_zoominfo,
  COUNT(*) FILTER (WHERE data->'zoominfo_contact_enrichment'->>'provider' = 'apollo')           AS provider_apollo,
  COUNT(*) FILTER (
        WHERE data ? 'zoominfo_contact_enrichment'
          AND COALESCE(data->'zoominfo_contact_enrichment'->>'provider', '') NOT IN ('zoominfo', 'apollo')
  )                                                                                             AS provider_other,
  COUNT(*) FILTER (
        WHERE data ? 'zoominfo_contact_enrichment'
          AND phone IS NOT NULL AND phone <> ''
  )                                                                                             AS enriched_with_phone,
  COUNT(*) FILTER (
        WHERE data ? 'zoominfo_contact_enrichment'
          AND email IS NOT NULL AND email <> ''
  )                                                                                             AS enriched_with_email
FROM sourced_candidates
WHERE updated_at >= NOW() - (%(window_days)s || ' days')::INTERVAL
  AND (%(source)s IS NULL OR source = %(source)s)
  AND (%(jobdiva_id)s IS NULL OR jobdiva_id = %(jobdiva_id)s);
"""


def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "  n/a"
    return f"{(numerator / denominator) * 100:5.1f}%"


def _print_row(label: str, value: int, denom: int, denom_label: str) -> None:
    print(f"  {label:<38} {value:>7,}   ({_pct(value, denom)} of {denom_label})")


def run(args: argparse.Namespace) -> int:
    params: Dict[str, Any] = {
        "window_days": str(args.window_days),
        "source": args.source,
        "jobdiva_id": args.jobdiva_id,
    }

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY, params)
            row = cur.fetchone() or ()
    finally:
        conn.close()

    if not row:
        print("[hits] no rows returned (unexpected)")
        return 1

    (
        total,
        with_phone,
        with_email,
        with_both,
        attempted_enrich,
        provider_zoominfo,
        provider_apollo,
        provider_other,
        enriched_with_phone,
        enriched_with_email,
    ) = row

    filters: List[str] = [f"updated_at within last {args.window_days} days"]
    if args.source:
        filters.append(f"source = {args.source!r}")
    if args.jobdiva_id:
        filters.append(f"jobdiva_id = {args.jobdiva_id!r}")

    print("\n[hits] sourced_candidates enrichment counts")
    print(f"[hits] filters: {' AND '.join(filters)}")
    print()
    print(f"  {'Total candidates in window':<38} {total:>7,}")
    _print_row("With phone", with_phone, total, "total")
    _print_row("With email", with_email, total, "total")
    _print_row("With both phone + email", with_both, total, "total")
    print()
    print(f"  {'Went through /enrich-contact':<38} {attempted_enrich:>7,}   ({_pct(attempted_enrich, total)} of total)")
    _print_row("  provider = zoominfo", provider_zoominfo, attempted_enrich, "attempted")
    _print_row("  provider = apollo",   provider_apollo,   attempted_enrich, "attempted")
    _print_row("  provider = none/other", provider_other,  attempted_enrich, "attempted")
    _print_row("  phone present after enrich", enriched_with_phone, attempted_enrich, "attempted")
    _print_row("  email present after enrich", enriched_with_email, attempted_enrich, "attempted")
    print()
    return 0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--window-days", type=int, default=7,
                   help="How many days back from now to include (default: 7).")
    p.add_argument("--source", default=None,
                   help="Restrict to a single source value, e.g. 'linkedin'. Default: all sources.")
    p.add_argument("--jobdiva-id", default=None,
                   help="Restrict to a single jobdiva_id. Default: all jobs.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
