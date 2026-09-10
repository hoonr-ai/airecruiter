#!/usr/bin/env python3
"""Populate the person-level candidate pool from existing data.

Four passes, each idempotent and independently resumable:

  1. persons   — resolve every `sourced_candidates` row to a `candidate_person`
                 via the identity ladder (LinkedIn URL -> JobDiva id -> email ->
                 phone -> resume_hash).
  2. outreach  — replay `engage_interview_audit` into `candidate_outreach`.
  3. facts     — extract reusable logistics answers from the transcripts already
                 stored in `sourced_candidates.data.engage_last_response`.
  4. state     — compute `candidate_pool_state` for every person touched.

Usage
-----
    # See what it would do, change nothing:
    python scripts/backfill_candidate_pool.py --dry-run

    # Full run:
    python scripts/backfill_candidate_pool.py

    # Resume after an interruption (watermarks are printed as it goes):
    python scripts/backfill_candidate_pool.py --since-candidate-row 41200

    # One pass at a time:
    python scripts/backfill_candidate_pool.py --only outreach

Run it from `apps/api` with the API's environment loaded.

Safe to re-run: identity keys upsert with `ON CONFLICT DO NOTHING`, outreach rows
key on `interview_id`, facts key on `(person_id, fact_key)`, and pool state keys
on `person_id`.

Cost note: pass 1 is the expensive one (one resolution per sourced row, ~8k rows
today). It commits per batch, so an interrupted run loses at most one batch and
`--since-candidate-row` picks up where it stopped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import get_db_connection  # noqa: E402
from services import candidate_identity, candidate_pool, screen_facts  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
)
logger = logging.getLogger("backfill_pool")

BATCH = 500


# ---------------------------------------------------------------------------
# Pass 1: persons
# ---------------------------------------------------------------------------
def backfill_persons(*, dry_run: bool, since_row_id: int) -> Tuple[int, int, int]:
    """Resolve every sourced_candidates row to a person.

    Joins `candidate_enhanced_info` for `resume_hash` so the résumé rung of the
    ladder is available — that is the rung that catches the same human re-ingested
    under a fresh JobDiva id with no URL and no real email.
    """
    seen_rows = 0
    resolved = 0
    unresolvable = 0
    last_id = since_row_id

    conn = get_db_connection()
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sc.id, sc.candidate_id, sc.source, sc.name, sc.email,
                           sc.phone, sc.headline, sc.location, sc.profile_url,
                           sc.data, cei.resume_hash
                    FROM sourced_candidates sc
                    LEFT JOIN candidate_enhanced_info cei
                           ON cei.candidate_id = sc.candidate_id
                    WHERE sc.id > %s
                    ORDER BY sc.id
                    LIMIT %s
                    """,
                    (last_id, BATCH),
                )
                rows = cur.fetchall() or []

            if not rows:
                break

            candidates: List[Dict[str, Any]] = []
            for row in rows:
                last_id = row[0]
                data = row[9]
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except Exception:  # noqa: BLE001
                        data = {}
                candidates.append({
                    "candidate_id": row[1],
                    "source": row[2],
                    "name": row[3],
                    "email": row[4],
                    "phone": row[5],
                    "headline": row[6],
                    "location": row[7],
                    "profile_url": row[8],
                    "data": data if isinstance(data, dict) else {},
                    "resume_hash": row[10],
                })
            seen_rows += len(rows)

            if dry_run:
                # Exercise the ladder without writing, so a dry run still reports
                # how many rows have no usable identity key at all.
                for c in candidates:
                    if candidate_identity.extract_identity_keys(c):
                        resolved += 1
                    else:
                        unresolvable += 1
            else:
                with conn.cursor() as cur:
                    mapping = candidate_identity.resolve_persons_bulk(candidates, cur=cur)
                conn.commit()
                resolved += sum(1 for v in mapping.values() if v)
                unresolvable += sum(1 for v in mapping.values() if not v)

            logger.info(
                "persons: %s rows scanned, %s resolved, %s unresolvable "
                "(watermark --since-candidate-row %s)",
                seen_rows,
                resolved,
                unresolvable,
                last_id,
            )
    finally:
        conn.close()

    return seen_rows, resolved, unresolvable


# ---------------------------------------------------------------------------
# Pass 2: outreach ledger
# ---------------------------------------------------------------------------
def _classify_audit_status(
    audit_status: str, interview_id: Optional[str]
) -> Tuple[str, Optional[str]]:
    """Map a legacy `engage_interview_audit.status` to (ledger status, outcome).

    The tricky one is `'failed'`, which the old schema uses for two unrelated
    things:
      * a *dispatch* failure — PAIR accepted the batch but returned no
        interview_id (`audit_status = "Initiated" if interview_id else "failed"`
        in send-bulk-interview). Those stay retryable and were never a contact.
      * a *screen* failure — the webhook resolved the interview to
        `effective_status='failed'`.
    The presence of an interview_id is what separates them, so classify on that
    rather than on the string alone. Getting this wrong in either direction is
    costly: treating a dispatch failure as a contact would spend the person's
    90-day attempt budget on a call that never happened, while treating a screen
    failure as a dispatch error would drop a real result.
    """
    status = (audit_status or "").strip().lower()
    has_interview = bool(str(interview_id or "").strip())

    if not has_interview:
        return "error", None
    if status in ("passed", "hired", "pass"):
        return "completed", "pass"
    if status in ("failed", "fail", "rejected"):
        return "completed", "fail"
    if status == "completed":
        # Webhook resolves completed -> passed/failed; a bare 'completed' predates
        # that or had no hard filter. The webhook's own rule is "no hard filter =
        # passed", so mirror it.
        return "completed", "pass"
    if status in ("in_progress", "in progress", "inprogress"):
        return "in_progress", None
    # 'Initiated', 'sent', 'created', 'queued', anything unrecognized: dispatched,
    # no result yet. The reconcile job ages these into 'no_response'.
    return "sent", None


def backfill_outreach(*, dry_run: bool, since_row_id: int) -> Dict[str, int]:
    stats = {"scanned": 0, "written": 0, "no_person": 0, "errors": 0}
    last_id = since_row_id
    touched: Set[int] = set()

    conn = get_db_connection()
    try:
        while True:
            with conn.cursor() as cur:
                # created_at / updated_at are naive TIMESTAMP written via
                # CURRENT_TIMESTAMP; interpret them as UTC to compare against the
                # pool's TIMESTAMPTZ columns. See the migration header.
                cur.execute(
                    """
                    SELECT a.id, a.candidate_id, a.jobdiva_id, a.interview_id, a.status,
                           a.created_at AT TIME ZONE 'UTC' AS created_utc,
                           a.updated_at AT TIME ZONE 'UTC' AS updated_utc,
                           a.response
                    FROM engage_interview_audit a
                    WHERE a.id > %s
                    ORDER BY a.id
                    LIMIT %s
                    """,
                    (last_id, BATCH),
                )
                rows = cur.fetchall() or []

            if not rows:
                break

            candidate_ids = [r[1] for r in rows if r[1]]
            person_by_cid = candidate_identity.person_ids_for_candidate_ids(candidate_ids)

            for row in rows:
                last_id = row[0]
                stats["scanned"] += 1
                candidate_id = row[1]
                person_id = person_by_cid.get(str(candidate_id or ""))
                if not person_id:
                    # Pass 1 could not identify this candidate (no URL, no real
                    # email, no phone, no résumé). Nothing to attach the history
                    # to; the launch path will treat them as unknown and give them
                    # a full screen, which is the safe default.
                    stats["no_person"] += 1
                    continue

                ledger_status, outcome = _classify_audit_status(row[4], row[3])
                sent_at = row[5]
                updated_at = row[6]

                response = row[7]
                if isinstance(response, str):
                    try:
                        response = json.loads(response)
                    except Exception:  # noqa: BLE001
                        response = {}
                response = response if isinstance(response, dict) else {}
                score = response.get("candidate_score") or response.get("overall_score")
                total = response.get("total_score")

                # A finished or in-flight screen proves the candidate engaged, so
                # first_activity_at is set. An untouched 'sent' row must keep it
                # NULL — that is exactly what lets reconcile age it into
                # 'no_response' and land it in the page's bottom section.
                had_activity = ledger_status in ("in_progress", "completed")

                if dry_run:
                    stats["written"] += 1
                    touched.add(person_id)
                    continue

                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO candidate_outreach
                                (person_id, candidate_id, jobdiva_id, interview_id,
                                 tier, screening_level, status, sent_at,
                                 first_activity_at, completed_at, outcome, score,
                                 total_score)
                            VALUES (%s, %s, %s, %s, 'full', NULL, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (interview_id) WHERE interview_id IS NOT NULL
                            DO UPDATE SET
                                status       = EXCLUDED.status,
                                outcome      = COALESCE(EXCLUDED.outcome, candidate_outreach.outcome),
                                score        = COALESCE(EXCLUDED.score, candidate_outreach.score),
                                total_score  = COALESCE(EXCLUDED.total_score, candidate_outreach.total_score),
                                completed_at = COALESCE(EXCLUDED.completed_at, candidate_outreach.completed_at),
                                updated_at   = now()
                            """,
                            (
                                person_id,
                                str(candidate_id or ""),
                                str(row[2] or ""),
                                (str(row[3]).strip() or None) if row[3] else None,
                                ledger_status,
                                sent_at,
                                updated_at if had_activity else None,
                                updated_at if ledger_status == "completed" else None,
                                outcome,
                                float(score) if score is not None else None,
                                float(total) if total is not None else None,
                            ),
                        )
                    stats["written"] += 1
                    touched.add(person_id)
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    stats["errors"] += 1
                    logger.warning("outreach row %s failed: %s", row[0], e)
                    continue

            if not dry_run:
                conn.commit()
            logger.info(
                "outreach: %s scanned, %s written, %s without a person, %s errors "
                "(watermark --since-audit-row %s)",
                stats["scanned"],
                stats["written"],
                stats["no_person"],
                stats["errors"],
                last_id,
            )
    finally:
        conn.close()

    stats["persons_touched"] = len(touched)
    _remember_touched(touched)
    return stats


# ---------------------------------------------------------------------------
# Pass 3: facts
# ---------------------------------------------------------------------------
def backfill_facts(*, dry_run: bool) -> Dict[str, int]:
    """Extract reusable facts from transcripts already sitting in the data blob.

    Only rows that actually carry `engage_last_response` are scanned — a JSONB
    key-existence predicate, so this skips the ~95% of rows that were never
    screened without reading them.
    """
    stats = {"scanned": 0, "with_facts": 0, "facts_written": 0, "no_person": 0}
    touched: Set[int] = set()
    offset = 0

    conn = get_db_connection()
    try:
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (candidate_id)
                           candidate_id, jobdiva_id, data->'engage_last_response',
                           data->>'engage_interview_id',
                           data->>'engage_completed_at',
                           updated_at AT TIME ZONE 'UTC'
                    FROM sourced_candidates
                    WHERE data ? 'engage_last_response'
                    ORDER BY candidate_id, updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (BATCH, offset),
                )
                rows = cur.fetchall() or []

            if not rows:
                break
            offset += len(rows)

            person_by_cid = candidate_identity.person_ids_for_candidate_ids(
                [r[0] for r in rows if r[0]]
            )

            for row in rows:
                stats["scanned"] += 1
                person_id = person_by_cid.get(str(row[0] or ""))
                if not person_id:
                    stats["no_person"] += 1
                    continue

                payload = row[2]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:  # noqa: BLE001
                        payload = {}
                facts = screen_facts.facts_from_webhook_payload(payload)
                if not facts:
                    continue
                stats["with_facts"] += 1
                touched.add(person_id)

                if dry_run:
                    stats["facts_written"] += len(facts)
                    continue

                # captured_at drives TTL, so use the row's own timestamp rather
                # than now(). Backfilling with now() would make a two-year-old
                # answer look freshly confirmed and let it justify skipping
                # outreach — the single most damaging thing this script could get
                # wrong.
                captured = row[5] or datetime.now(timezone.utc)
                if isinstance(captured, datetime) and captured.tzinfo is None:
                    captured = captured.replace(tzinfo=timezone.utc)

                with conn.cursor() as cur:
                    stats["facts_written"] += screen_facts.write_facts(
                        person_id,
                        facts,
                        interview_id=row[3],
                        jobdiva_id=row[1],
                        captured_at=captured,
                        cur=cur,
                    )
                conn.commit()

            logger.info(
                "facts: %s rows scanned, %s with facts, %s facts written, "
                "%s without a person",
                stats["scanned"],
                stats["with_facts"],
                stats["facts_written"],
                stats["no_person"],
            )
    finally:
        conn.close()

    _remember_touched(touched)
    return stats


# ---------------------------------------------------------------------------
# Pass 4: pool state
# ---------------------------------------------------------------------------
_touched_persons: Set[int] = set()


def _remember_touched(persons: Iterable[int]) -> None:
    _touched_persons.update(int(p) for p in persons if p)


def backfill_pool_state(*, dry_run: bool, all_persons: bool) -> Dict[str, int]:
    """Compute the rollup.

    `--all-persons` recomputes every person (use after a schema change);
    otherwise only the ones passes 2 and 3 touched, plus any person with no
    rollup row yet.
    """
    stats = {"refreshed": 0}
    conn = get_db_connection()
    try:
        if all_persons:
            with conn.cursor() as cur:
                cur.execute("SELECT person_id FROM candidate_person ORDER BY person_id")
                targets = [int(r[0]) for r in cur.fetchall() or []]
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.person_id
                    FROM candidate_person p
                    LEFT JOIN candidate_pool_state s USING (person_id)
                    WHERE s.person_id IS NULL
                    """
                )
                targets = sorted(_touched_persons | {int(r[0]) for r in cur.fetchall() or []})

        logger.info("state: %s persons to refresh", len(targets))
        if dry_run:
            stats["refreshed"] = len(targets)
            return stats

        for idx, person_id in enumerate(targets, start=1):
            with conn.cursor() as cur:
                candidate_pool.refresh_pool_state(person_id, cur=cur)
            conn.commit()
            stats["refreshed"] += 1
            if idx % 500 == 0:
                logger.info("state: %s/%s refreshed", idx, len(targets))
    finally:
        conn.close()
    return stats


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument(
        "--only",
        choices=("persons", "outreach", "facts", "state"),
        action="append",
        help="run only these passes (repeatable); default runs all four in order",
    )
    parser.add_argument("--since-candidate-row", type=int, default=0,
                        help="resume pass 1 after this sourced_candidates.id")
    parser.add_argument("--since-audit-row", type=int, default=0,
                        help="resume pass 2 after this engage_interview_audit.id")
    parser.add_argument("--all-persons", action="store_true",
                        help="pass 4 recomputes every person, not just the touched ones")
    args = parser.parse_args()

    passes = args.only or ["persons", "outreach", "facts", "state"]
    if args.dry_run:
        logger.info("DRY RUN — no writes")

    if "persons" in passes:
        scanned, resolved, unresolvable = backfill_persons(
            dry_run=args.dry_run, since_row_id=args.since_candidate_row
        )
        logger.info(
            "pass 1 done: %s rows, %s resolved, %s with no usable identity key",
            scanned, resolved, unresolvable,
        )
        if unresolvable:
            logger.info(
                "  the %s unresolvable rows have no profile URL, JobDiva id, real "
                "email, usable phone or résumé — they stay 'unknown' and always get "
                "a full screen",
                unresolvable,
            )

    if "outreach" in passes:
        logger.info("pass 2 done: %s", backfill_outreach(
            dry_run=args.dry_run, since_row_id=args.since_audit_row
        ))

    if "facts" in passes:
        logger.info("pass 3 done: %s", backfill_facts(dry_run=args.dry_run))

    if "state" in passes:
        logger.info("pass 4 done: %s", backfill_pool_state(
            dry_run=args.dry_run, all_persons=args.all_persons
        ))

    logger.info("backfill complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
