"""
ingest_master_taxonomy.py
-------------------------
One-shot CLI to load the role + skill hierarchical taxonomies into the
flat master tables that taxonomy_service.py reads (roles_master,
skills_master).

Distinct from ingest_ontology.py — that script writes to the
skill_nodes / skill_edges graph schema, used by GraphRAG. This one
populates the K-level columns that the per-process cache pulls.

Run:
    python ingest_master_taxonomy.py --roles data/role_taxonomy.csv \\
                                     --skills data/skill_taxonomy.csv

Either flag is optional — pass whichever CSVs you want to (re)load.
Idempotent via ON CONFLICT DO UPDATE.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from typing import List, Sequence

import psycopg2
import psycopg2.extras

from core.config import DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest_master_taxonomy")

ROLE_COLUMNS = [
    "role_k17000", "role_k10000", "role_k5000", "role_k1500",
    "role_k1000", "role_k500", "role_k150", "role_k50", "role_k10",
]
SKILL_COLUMNS = [
    "skill_mapped", "skill_k15000", "skill_k5000", "skill_k1500",
    "skill_k500", "skill_k150", "skill_k50", "skill_k15",
]

CHUNK_SIZE = 1000


def _connect():
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        sys.exit(2)
    return psycopg2.connect(DATABASE_URL)


def _read_rows(csv_path: str, expected_cols: Sequence[str]) -> List[List[str]]:
    """Read CSV into a list of normalized rows.

    The CSV header is matched case-insensitively against `expected_cols`.
    Rows are returned in the same column order as `expected_cols`.
    Missing leaf cell skips the row.
    """
    if not os.path.exists(csv_path):
        logger.error("CSV not found: %s", csv_path)
        sys.exit(2)

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        norm_header = [h.strip().lower() for h in header]
        expected_lower = [c.lower() for c in expected_cols]

        idx_map = []
        for col in expected_lower:
            if col not in norm_header:
                logger.error("CSV %s missing expected column %s. Header: %s", csv_path, col, header)
                sys.exit(2)
            idx_map.append(norm_header.index(col))

        rows: List[List[str]] = []
        for raw in reader:
            if not raw:
                continue
            try:
                cells = [(raw[i].strip() if i < len(raw) else "") for i in idx_map]
            except IndexError:
                continue
            if not cells[0]:  # leaf required
                continue
            rows.append(cells)
    return rows


def _ingest(table: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> int:
    if not rows:
        return 0
    conn = _connect()
    conn.autocommit = False
    inserted = 0
    try:
        with conn.cursor() as cur:
            cols_sql = ", ".join(columns)
            placeholders = ", ".join(["%s"] * len(columns))
            update_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns[1:])
            stmt = (
                f"INSERT INTO public.{table} ({cols_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT ({columns[0]}) DO UPDATE SET {update_sql}"
            )
            for start in range(0, len(rows), CHUNK_SIZE):
                chunk = rows[start:start + CHUNK_SIZE]
                psycopg2.extras.execute_batch(cur, stmt, chunk, page_size=CHUNK_SIZE)
                inserted += len(chunk)
                logger.info("  %s: upserted %d / %d", table, inserted, len(rows))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roles", type=str, default=None, help="Path to role taxonomy CSV")
    parser.add_argument("--skills", type=str, default=None, help="Path to skill taxonomy CSV")
    args = parser.parse_args()

    if not args.roles and not args.skills:
        parser.print_help()
        logger.error("Pass --roles and/or --skills")
        return 2

    if args.roles:
        logger.info("Reading role taxonomy from %s", args.roles)
        rows = _read_rows(args.roles, ROLE_COLUMNS)
        logger.info("Parsed %d role rows. Upserting into roles_master ...", len(rows))
        _ingest("roles_master", ROLE_COLUMNS, rows)
        logger.info("✅ Roles ingestion complete.")

    if args.skills:
        logger.info("Reading skill taxonomy from %s", args.skills)
        rows = _read_rows(args.skills, SKILL_COLUMNS)
        logger.info("Parsed %d skill rows. Upserting into skills_master ...", len(rows))
        _ingest("skills_master", SKILL_COLUMNS, rows)
        logger.info("✅ Skills ingestion complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
