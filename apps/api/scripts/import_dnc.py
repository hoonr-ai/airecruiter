"""DNC list importer.

Reads a DNC xlsx (column A = phone numbers; other columns are ignored),
normalizes each phone via utils.phone.normalize_phone, upserts into
``dnc_list``, then retroactively flags any rows in ``sourced_candidates``
whose phone matches by setting ``dnc_stopped_at = NOW()``. The outreach
path filters on ``dnc_stopped_at IS NOT NULL`` so flagged candidates stop
receiving emails / interview invites without inactivating the rest of the
PAIR.

Run:
    cd apps/api
    venv/bin/python -m scripts.import_dnc /path/to/Zoom\\ DNC\\ list.xlsx

Re-runnable: existing phones are skipped via ON CONFLICT DO NOTHING; the
retroactive UPDATE only touches candidates whose dnc_stopped_at is still
NULL, so subsequent runs are no-ops for already-flagged rows.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

# Allow running as `python -m scripts.import_dnc` from apps/api with no
# PYTHONPATH gymnastics.
_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from sqlalchemy import text  # noqa: E402

from services.dnc_storage import (  # noqa: E402
    _ensure_dnc_schema,
    _get_engine,
    invalidate_dnc_cache,
)
from utils.phone import normalize_phone  # noqa: E402


def _read_phones_from_xlsx(path: Path) -> List[str]:
    try:
        import openpyxl  # type: ignore
    except ImportError as e:
        print(
            "ERROR: openpyxl is not installed. Add it to apps/api/requirements.txt "
            "and install via: venv/bin/pip install openpyxl",
            file=sys.stderr,
        )
        raise SystemExit(1) from e

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active  # first sheet
    phones: List[str] = []
    skipped_unparseable = 0
    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not row:
            continue
        cell = row[0]
        if cell is None or cell == "":
            continue
        # Header detection: row 1 with non-numeric value → skip
        if row_idx == 1 and not isinstance(cell, (int, float)) and not str(cell).isdigit():
            continue
        normalized = normalize_phone(str(cell))
        if normalized:
            phones.append(normalized)
        else:
            skipped_unparseable += 1
    if skipped_unparseable:
        print(f"  (skipped {skipped_unparseable} unparseable cell(s) in column A)")
    return phones


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a DNC xlsx into dnc_list and retroactively stop matching candidates."
    )
    parser.add_argument("xlsx_path", help="Path to the DNC xlsx file")
    parser.add_argument(
        "--source",
        default="zoom",
        help="Source label stored alongside each phone (default: zoom)",
    )
    args = parser.parse_args()

    xlsx_path = Path(os.path.expanduser(args.xlsx_path)).resolve()
    if not xlsx_path.exists():
        print(f"ERROR: file not found: {xlsx_path}", file=sys.stderr)
        return 1

    print(f"Reading DNC xlsx: {xlsx_path}")
    phones = _read_phones_from_xlsx(xlsx_path)
    if not phones:
        print("No phones found in column A. Aborting.", file=sys.stderr)
        return 1
    unique_phones = sorted(set(phones))
    print(f"Parsed {len(phones)} phone cells → {len(unique_phones)} unique normalized numbers")

    print("Ensuring DNC schema (idempotent)...")
    _ensure_dnc_schema()

    engine = _get_engine()
    inserted = 0
    skipped_existing = 0
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for phone in unique_phones:
            res = conn.execute(
                text(
                    "INSERT INTO dnc_list (phone, source) VALUES (:phone, :source) "
                    "ON CONFLICT (phone) DO NOTHING"
                ),
                {"phone": phone, "source": args.source},
            )
            if res.rowcount and res.rowcount > 0:
                inserted += 1
            else:
                skipped_existing += 1

        # Retroactively flag already-launched candidates whose phone matches
        # any DNC entry. We strip non-digits from sourced_candidates.phone in
        # SQL because that column may hold formatted values like
        # "+1 (440) 840-5137" while dnc_list.phone is always 11-digit raw.
        retro = conn.execute(text(
            """
            UPDATE sourced_candidates
            SET dnc_stopped_at = NOW()
            WHERE dnc_stopped_at IS NULL
              AND phone IS NOT NULL
              AND phone <> ''
              AND (
                  CASE
                      WHEN length(regexp_replace(phone, '\\D', '', 'g')) = 10
                          THEN '1' || regexp_replace(phone, '\\D', '', 'g')
                      WHEN length(regexp_replace(phone, '\\D', '', 'g')) = 11
                           AND regexp_replace(phone, '\\D', '', 'g') LIKE '1%'
                          THEN regexp_replace(phone, '\\D', '', 'g')
                      ELSE NULL
                  END
              ) IN (SELECT phone FROM dnc_list)
            """
        ))
        retro_count = retro.rowcount or 0

    invalidate_dnc_cache()

    print()
    print("Summary:")
    print(f"  Inserted:                   {inserted}")
    print(f"  Existing (skipped):         {skipped_existing}")
    print(f"  Retroactively dnc_stopped:  {retro_count}")
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
