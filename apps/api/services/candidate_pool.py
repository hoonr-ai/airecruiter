"""The outreach ledger and its per-person rollup.

Two readers with different needs
--------------------------------
* **The suppression gate** — "may we contact this person right now?" — always
  reads `candidate_outreach` live. It is a point lookup on
  `(person_id, sent_at DESC)`. A stale answer here means a duplicate reachout, so
  it never reads the rollup.
* **The Candidates page** reads only `candidate_pool_state`. That table is
  upserted inside the same transaction as the ledger write, so it is not
  actually stale either, and the page becomes a single keyset-paginated index
  scan instead of the double-`DISTINCT ON` full-table CTE that got the old
  master-pool page switched off.

Ledger status vocabulary
------------------------
    sent         dispatched to PAIR, no candidate activity yet
    in_progress  candidate has started answering
    completed    screen finished; `outcome` is 'pass' or 'fail'
    no_response  aged past POOL_NO_RESPONSE_DAYS with zero activity
    error        PAIR returned no interview_id — retryable, NOT a contact
    skipped      tier='none': we deliberately reused a prior screen

`error` and `skipped` rows are excluded from every recency, cooldown and
attempt-count computation. `error` because the candidate was never actually
reached (the existing engage flow deliberately keeps those retryable), and
`skipped` because Tier C means we chose *not* to contact them — counting it
would let a reuse decision suppress the next real attempt.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.config import (
    POOL_COOLDOWN_DAYS,
    POOL_MAX_ATTEMPTS_90D,
    POOL_NO_RESPONSE_DAYS,
)
from core.db import get_db_connection
from services import screen_facts

logger = logging.getLogger(__name__)


# Ledger statuses that represent a real attempt to reach the human.
CONTACT_STATUSES = ("sent", "in_progress", "completed", "no_response")
# Statuses that mean the attempt is still open (no result yet).
OPEN_STATUSES = ("sent", "in_progress")

# SQL fragment shared by every recency/attempt computation. Keeping it in one
# place is what stops the gate and the rollup from disagreeing about what counts
# as a contact.
_CONTACT_PREDICATE = "tier <> 'none' AND status = ANY(%(contact_statuses)s)"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> Optional[datetime]:
    """Coerce a DB timestamp to an aware UTC datetime.

    New pool tables are TIMESTAMPTZ, but values that travel through the backfill
    originate in legacy naive TIMESTAMP columns. Anything naive that reaches here
    is treated as UTC — the same assumption documented in the migration, and the
    reason schema init logs a warning when the server is not on UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# Reading: the suppression / reuse context
# ---------------------------------------------------------------------------
@dataclass
class PersonOutreachContext:
    """Everything the reuse policy needs about one person's history."""

    person_id: int
    last_sent_at: Optional[datetime] = None
    last_jobdiva_id: Optional[str] = None
    last_status: Optional[str] = None
    last_tier: Optional[str] = None
    # Most recent *completed* screen — may be older than the last attempt.
    last_completed_at: Optional[datetime] = None
    last_completed_jobdiva_id: Optional[str] = None
    last_outcome: Optional[str] = None
    last_score: Optional[float] = None
    # Sent timestamps of contact attempts inside the rolling 90-day window,
    # newest first. Length is the attempt count; the oldest one tells us when
    # the cap expires.
    recent_attempt_times: List[datetime] = field(default_factory=list)
    has_open_attempt: bool = False
    open_attempt_jobdiva_id: Optional[str] = None
    dnc_blocked: bool = False

    @property
    def attempts_90d(self) -> int:
        return len(self.recent_attempt_times)

    def cooldown_until(self) -> Optional[datetime]:
        """When an unanswered attempt stops blocking further contact.

        A *completed* screen does not impose a cooldown — the candidate engaged,
        so re-approaching them for a different role is legitimate (and is exactly
        what the re-check tier is for). Only silence earns a cooldown.
        """
        if not self.last_sent_at:
            return None
        if self.last_status in ("sent", "in_progress", "no_response"):
            return self.last_sent_at + timedelta(days=POOL_COOLDOWN_DAYS)
        return None

    def attempt_cap_until(self) -> Optional[datetime]:
        """When the rolling-90-day attempt count drops back below the cap."""
        if POOL_MAX_ATTEMPTS_90D <= 0:
            return None
        if len(self.recent_attempt_times) < POOL_MAX_ATTEMPTS_90D:
            return None
        # Sorted newest-first: the attempt at index (cap - 1) is the one whose
        # 90-day expiry frees a slot.
        blocking = self.recent_attempt_times[POOL_MAX_ATTEMPTS_90D - 1]
        return blocking + timedelta(days=90)

    def next_eligible_at(self) -> Optional[datetime]:
        candidates = [t for t in (self.cooldown_until(), self.attempt_cap_until()) if t]
        return max(candidates) if candidates else None


def get_outreach_contexts(
    person_ids: Sequence[int], *, cur=None
) -> Dict[int, PersonOutreachContext]:
    """Load outreach history for a batch of persons.

    Three queries for the whole batch regardless of size — the launch path
    resolves 75 candidates at a time and must not do 75 round-trips.
    """
    ids = sorted({int(p) for p in person_ids if p})
    if not ids:
        return {}

    def _run(active_cur) -> Dict[int, PersonOutreachContext]:
        out = {pid: PersonOutreachContext(person_id=pid) for pid in ids}
        window_start = _now() - timedelta(days=90)
        params = {
            "ids": ids,
            "contact_statuses": list(CONTACT_STATUSES),
            "open_statuses": list(OPEN_STATUSES),
            "window_start": window_start,
        }

        # 1. Latest attempt per person.
        active_cur.execute(
            f"""
            SELECT DISTINCT ON (person_id)
                   person_id, sent_at, jobdiva_id, status, tier
            FROM candidate_outreach
            WHERE person_id = ANY(%(ids)s) AND {_CONTACT_PREDICATE}
            ORDER BY person_id, sent_at DESC
            """,
            params,
        )
        for row in active_cur.fetchall() or []:
            ctx = out[int(row[0])]
            ctx.last_sent_at = _as_utc(row[1])
            ctx.last_jobdiva_id = row[2]
            ctx.last_status = row[3]
            ctx.last_tier = row[4]

        # 2. Latest *completed* screen per person — the reuse payload.
        active_cur.execute(
            """
            SELECT DISTINCT ON (person_id)
                   person_id, completed_at, jobdiva_id, outcome, score
            FROM candidate_outreach
            WHERE person_id = ANY(%(ids)s)
              AND status = 'completed'
              AND completed_at IS NOT NULL
            ORDER BY person_id, completed_at DESC
            """,
            params,
        )
        for row in active_cur.fetchall() or []:
            ctx = out[int(row[0])]
            ctx.last_completed_at = _as_utc(row[1])
            ctx.last_completed_jobdiva_id = row[2]
            ctx.last_outcome = row[3]
            ctx.last_score = float(row[4]) if row[4] is not None else None

        # 3. Attempts in the 90-day window + open-attempt flag, one row per
        #    attempt so the caller can compute both the count and the cap expiry.
        active_cur.execute(
            f"""
            SELECT person_id, sent_at, status, jobdiva_id
            FROM candidate_outreach
            WHERE person_id = ANY(%(ids)s)
              AND {_CONTACT_PREDICATE}
              AND sent_at >= %(window_start)s
            ORDER BY person_id, sent_at DESC
            """,
            params,
        )
        for row in active_cur.fetchall() or []:
            ctx = out[int(row[0])]
            stamp = _as_utc(row[1])
            if stamp:
                ctx.recent_attempt_times.append(stamp)
            if row[2] in OPEN_STATUSES and not ctx.has_open_attempt:
                ctx.has_open_attempt = True
                ctx.open_attempt_jobdiva_id = row[3]

        # 4. Do-Not-Contact: dnc_list is keyed on phone, and
        #    candidate_person.primary_phone is stored as the same normalized
        #    10-digit form, so this is a direct join. A person with no phone on
        #    file can't be DNC-matched — that is the pre-existing limitation of a
        #    phone-only DNC list, not something this rollup can fix.
        try:
            active_cur.execute(
                """
                SELECT p.person_id
                FROM candidate_person p
                JOIN dnc_list d
                  ON RIGHT(REGEXP_REPLACE(d.phone, '[^0-9]', '', 'g'), 10) = p.primary_phone
                WHERE p.person_id = ANY(%(ids)s)
                  AND p.primary_phone IS NOT NULL
                """,
                params,
            )
            for row in active_cur.fetchall() or []:
                out[int(row[0])].dnc_blocked = True
        except Exception as e:  # noqa: BLE001
            # A missing/renamed dnc_list must not take down the launch path. Fail
            # open here: the existing per-job DNC gate in _generate_payload_for
            # (dnc_stopped_at) still runs and is the authoritative block.
            logger.warning("pool dnc join failed (continuing without): %s", e)

        return out

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            return _run(own_cur)
    except Exception as e:  # noqa: BLE001
        logger.error("get_outreach_contexts failed: %s", e)
        return {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Writing: the ledger
# ---------------------------------------------------------------------------
def record_outreach(
    *,
    person_id: int,
    candidate_id: str,
    jobdiva_id: str,
    interview_id: Optional[str],
    tier: str = "full",
    screening_level: Optional[str] = None,
    status: str = "sent",
    sent_at: Optional[datetime] = None,
    cur=None,
) -> Optional[int]:
    """Append an attempt to the ledger and refresh the person's rollup.

    Idempotent on `interview_id` (unique partial index): a retried send that
    reuses an interview id updates the existing row instead of double-counting an
    attempt, which would otherwise burn the person's 90-day budget.
    """
    if not person_id:
        return None

    def _run(active_cur) -> Optional[int]:
        active_cur.execute(
            """
            INSERT INTO candidate_outreach
                (person_id, candidate_id, jobdiva_id, interview_id, tier,
                 screening_level, status, sent_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))
            ON CONFLICT (interview_id) WHERE interview_id IS NOT NULL
            DO UPDATE SET
                status          = EXCLUDED.status,
                tier            = EXCLUDED.tier,
                screening_level = EXCLUDED.screening_level,
                candidate_id    = EXCLUDED.candidate_id,
                jobdiva_id      = EXCLUDED.jobdiva_id,
                updated_at      = now()
            -- Only an attempt that is still open may be rewritten by a re-send.
            -- Without this guard a duplicate send arriving after the webhook
            -- would knock a 'completed' row back to 'sent', which both loses the
            -- result and makes the person look mid-screen forever.
            WHERE candidate_outreach.status = ANY(%s)
            RETURNING id
            """,
            (
                person_id,
                str(candidate_id or ""),
                str(jobdiva_id or ""),
                (str(interview_id).strip() or None) if interview_id else None,
                tier,
                screening_level,
                status,
                sent_at,
                list(OPEN_STATUSES),
            ),
        )
        row = active_cur.fetchone()
        # No row returned means the ON CONFLICT guard declined the update — the
        # attempt already has a result, so the ledger is already correct. Still
        # refresh the rollup: the caller thinks something changed.
        refresh_pool_state(person_id, cur=active_cur)
        return int(row[0]) if row else None

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            result = _run(own_cur)
        conn.commit()
        return result
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "record_outreach failed (person=%s job=%s): %s", person_id, jobdiva_id, e
        )
        return None
    finally:
        conn.close()


def record_reuse_decision(
    *,
    person_id: int,
    candidate_id: str,
    jobdiva_id: str,
    tier: str = "none",
    cur=None,
) -> Optional[int]:
    """Record that we deliberately did NOT contact someone for a job.

    Stored as a `tier='none'`, `status='skipped'` ledger row so the decision is
    auditable ("job X reused person P's screen from job Y") without counting as
    an attempt — every recency and cooldown computation filters `tier <> 'none'`.
    """
    if not person_id:
        return None

    def _run(active_cur) -> Optional[int]:
        active_cur.execute(
            """
            INSERT INTO candidate_outreach
                (person_id, candidate_id, jobdiva_id, interview_id, tier, status)
            VALUES (%s, %s, %s, NULL, %s, 'skipped')
            RETURNING id
            """,
            (person_id, str(candidate_id or ""), str(jobdiva_id or ""), tier),
        )
        row = active_cur.fetchone()
        refresh_pool_state(person_id, cur=active_cur)
        return int(row[0]) if row else None

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            result = _run(own_cur)
        conn.commit()
        return result
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("record_reuse_decision failed (person=%s): %s", person_id, e)
        return None
    finally:
        conn.close()


def update_outreach_from_webhook(
    *,
    interview_id: str,
    status: str,
    outcome: Optional[str] = None,
    score: Optional[float] = None,
    total_score: Optional[float] = None,
    completed_at: Optional[datetime] = None,
    has_activity: bool = False,
    cur=None,
) -> Optional[int]:
    """Fold a PAIR webhook into the ledger. Returns the affected `person_id`.

    `first_activity_at` is stamped on the first signal of candidate activity and
    never overwritten — that timestamp is the whole basis for telling "pending,
    hasn't answered yet" apart from "pending forever, never responded", so a
    later webhook must not reset it.
    """
    iid = str(interview_id or "").strip()
    if not iid:
        return None

    def _run(active_cur) -> Optional[int]:
        active_cur.execute(
            """
            UPDATE candidate_outreach SET
                status            = %(status)s,
                outcome           = COALESCE(%(outcome)s, outcome),
                score             = COALESCE(%(score)s, score),
                total_score       = COALESCE(%(total_score)s, total_score),
                completed_at      = COALESCE(%(completed_at)s, completed_at),
                first_activity_at = COALESCE(first_activity_at,
                                             CASE WHEN %(has_activity)s THEN now() END),
                updated_at        = now()
            WHERE interview_id = %(interview_id)s
            RETURNING person_id
            """,
            {
                "status": status,
                "outcome": outcome,
                "score": score,
                "total_score": total_score,
                "completed_at": completed_at,
                "has_activity": bool(has_activity),
                "interview_id": iid,
            },
        )
        row = active_cur.fetchone()
        if not row:
            # No ledger row for this interview: the send predates the pool, or
            # the person couldn't be identified at launch. Not an error — the
            # legacy audit table remains the authoritative record, and the
            # backfill will pick this interview up.
            logger.info("pool: no ledger row for interview %s (pre-pool send?)", iid)
            return None
        person_id = int(row[0])
        refresh_pool_state(person_id, cur=active_cur)
        return person_id

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            result = _run(own_cur)
        conn.commit()
        return result
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("update_outreach_from_webhook failed for %s: %s", iid, e)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The rollup
# ---------------------------------------------------------------------------
def compute_bucket(ctx: PersonOutreachContext, *, now: Optional[datetime] = None) -> str:
    """Which section of the Candidates page this person belongs in.

    Bucket follows the person's MOST RECENT attempt, because the page is
    organized by recency — "who did we last reach out to, for what, and how did
    it end". A person with a completed screen for job A and an open attempt for
    job B reads as in-progress; their completed screen is still surfaced through
    `last_completed_at`/`last_outcome`, which is what the reuse pool reads.
    """
    moment = now or _now()
    if ctx.dnc_blocked:
        return "suppressed"
    if not ctx.last_sent_at:
        return "never_contacted"
    if ctx.last_status in OPEN_STATUSES:
        age_days = (moment - ctx.last_sent_at).total_seconds() / 86400.0
        return "in_progress" if age_days <= POOL_NO_RESPONSE_DAYS else "no_response"
    if ctx.last_status == "no_response":
        return "no_response"
    if ctx.last_status == "completed":
        return "completed"
    # 'error'/'skipped' are filtered out of last_status by _CONTACT_PREDICATE, so
    # anything here is an unrecognized status — treat as contacted-but-unknown
    # rather than claiming we never reached them.
    return "in_progress"


def refresh_pool_state(person_id: int, *, cur=None) -> None:
    """Recompute one person's rollup row from the ledger, facts and DNC state."""
    if not person_id:
        return

    def _run(active_cur) -> None:
        ctx = get_outreach_contexts([person_id], cur=active_cur).get(person_id)
        if ctx is None:
            ctx = PersonOutreachContext(person_id=person_id)

        facts = screen_facts.load_facts_bulk([person_id], cur=active_cur).get(person_id, {})
        fresh_until = screen_facts.facts_fresh_until(facts)

        bucket = compute_bucket(ctx)
        next_eligible = ctx.next_eligible_at()

        suppressed_reason = None
        if ctx.dnc_blocked:
            suppressed_reason = "Do Not Contact list"

        # Job title for the last attempt. Scalar subquery with LIMIT 1 rather
        # than a join: monitored_jobs can hold two rows per job (job_id ==
        # jobdiva_id duplicates), and matching either spelling with an OR join
        # would fan out.
        active_cur.execute(
            """
            INSERT INTO candidate_pool_state (
                person_id, bucket, last_outreach_at, last_job_id, last_job_title,
                last_tier, last_completed_at, last_outcome, last_score,
                attempts_90d, attempts_total, next_eligible_at, suppressed_reason,
                facts_fresh_until, display_name, headline, location, has_contact,
                sort_at, updated_at
            )
            SELECT
                p.person_id,
                %(bucket)s,
                %(last_outreach_at)s,
                %(last_job_id)s,
                (SELECT COALESCE(mj.enhanced_title, mj.title)
                   FROM monitored_jobs mj
                  WHERE mj.jobdiva_id = %(last_job_id)s OR mj.job_id = %(last_job_id)s
                  ORDER BY (mj.job_id ~ '^[0-9]+$') DESC, mj.created_at DESC
                  LIMIT 1),
                %(last_tier)s,
                %(last_completed_at)s,
                %(last_outcome)s,
                %(last_score)s,
                %(attempts_90d)s,
                (SELECT COUNT(*) FROM candidate_outreach o
                  WHERE o.person_id = p.person_id
                    AND o.tier <> 'none'
                    AND o.status = ANY(%(contact_statuses)s)),
                %(next_eligible_at)s,
                %(suppressed_reason)s,
                %(facts_fresh_until)s,
                p.display_name,
                p.headline,
                p.location,
                (p.primary_email IS NOT NULL OR p.primary_phone IS NOT NULL),
                -- Keyset sort key. Falls back to the last completed screen so a
                -- suppressed-but-previously-screened person still sorts by real
                -- recency, then to the epoch so never-contacted rows sort last
                -- without needing NULLS handling.
                COALESCE(%(last_outreach_at)s, %(last_completed_at)s, to_timestamp(0)),
                now()
            FROM candidate_person p
            WHERE p.person_id = %(person_id)s
            ON CONFLICT (person_id) DO UPDATE SET
                bucket            = EXCLUDED.bucket,
                last_outreach_at  = EXCLUDED.last_outreach_at,
                last_job_id       = EXCLUDED.last_job_id,
                last_job_title    = EXCLUDED.last_job_title,
                last_tier         = EXCLUDED.last_tier,
                last_completed_at = EXCLUDED.last_completed_at,
                last_outcome      = EXCLUDED.last_outcome,
                last_score        = EXCLUDED.last_score,
                attempts_90d      = EXCLUDED.attempts_90d,
                attempts_total    = EXCLUDED.attempts_total,
                next_eligible_at  = EXCLUDED.next_eligible_at,
                suppressed_reason = EXCLUDED.suppressed_reason,
                facts_fresh_until = EXCLUDED.facts_fresh_until,
                display_name      = EXCLUDED.display_name,
                headline          = EXCLUDED.headline,
                location          = EXCLUDED.location,
                has_contact       = EXCLUDED.has_contact,
                sort_at           = EXCLUDED.sort_at,
                updated_at        = now()
            """,
            {
                "person_id": person_id,
                "bucket": bucket,
                "last_outreach_at": ctx.last_sent_at,
                "last_job_id": ctx.last_jobdiva_id,
                "last_tier": ctx.last_tier,
                "last_completed_at": ctx.last_completed_at,
                "last_outcome": ctx.last_outcome,
                "last_score": ctx.last_score,
                "attempts_90d": ctx.attempts_90d,
                "next_eligible_at": next_eligible,
                "suppressed_reason": suppressed_reason,
                "facts_fresh_until": fresh_until,
                "contact_statuses": list(CONTACT_STATUSES),
            },
        )

    if cur is not None:
        _run(cur)
        return

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            _run(own_cur)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("refresh_pool_state failed for person %s: %s", person_id, e)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------
_RECONCILE_LOCK = "pool_state_reconcile"
_RECONCILE_STALE_MINUTES = 60


def _claim_reconcile_lock(cur, worker_id: str) -> bool:
    """Atomic conditional-UPDATE claim.

    The API runs 8 uvicorn workers, each with its own APScheduler, so an
    unguarded scheduled job fires 8 times. The claim is a single UPDATE whose
    WHERE clause is the guard, so exactly one worker's UPDATE affects a row. The
    staleness escape hatch releases a lock left behind by a worker that died
    mid-run.
    """
    cur.execute(
        """
        UPDATE candidate_pool_reconcile_lock
        SET claimed_at = now(), claimed_by = %s, finished_at = NULL
        WHERE lock_name = %s
          AND (
                claimed_at IS NULL
             OR finished_at IS NOT NULL
             OR claimed_at < now() - (%s || ' minutes')::interval
          )
        """,
        (worker_id, _RECONCILE_LOCK, _RECONCILE_STALE_MINUTES),
    )
    return cur.rowcount > 0


def _release_reconcile_lock(cur) -> None:
    cur.execute(
        "UPDATE candidate_pool_reconcile_lock SET finished_at = now() WHERE lock_name = %s",
        (_RECONCILE_LOCK,),
    )


def reconcile_pool_state() -> Dict[str, int]:
    """Age out silent attempts and repair rollup drift.

    Two jobs:
      1. Flip open attempts with no candidate activity, older than
         POOL_NO_RESPONSE_DAYS, to `no_response`. This is what populates the
         "reached out but never responded" section — nothing else moves a row
         there, because silence produces no webhook.
      2. Refresh rollups for the persons that changed, plus any whose
         `next_eligible_at` has just passed (they become contactable again, so
         their cooldown state is now wrong).
    """
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stats = {"aged_out": 0, "refreshed": 0}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if not _claim_reconcile_lock(cur, worker_id):
                conn.commit()
                logger.debug("pool reconcile: lock held elsewhere, skipping")
                return stats
            conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE candidate_outreach
                SET status = 'no_response', updated_at = now()
                WHERE status = ANY(%s)
                  AND tier <> 'none'
                  AND first_activity_at IS NULL
                  AND completed_at IS NULL
                  AND sent_at < now() - (%s || ' days')::interval
                RETURNING person_id
                """,
                (list(OPEN_STATUSES), POOL_NO_RESPONSE_DAYS),
            )
            changed = {int(r[0]) for r in cur.fetchall() or []}
            stats["aged_out"] = len(changed)

            # Rollups whose cooldown/cap window has elapsed are now wrong even
            # though no ledger row changed.
            cur.execute(
                """
                SELECT person_id FROM candidate_pool_state
                WHERE next_eligible_at IS NOT NULL AND next_eligible_at <= now()
                LIMIT 5000
                """
            )
            changed.update(int(r[0]) for r in cur.fetchall() or [])
            conn.commit()

        for person_id in changed:
            with conn.cursor() as cur:
                refresh_pool_state(person_id, cur=cur)
            conn.commit()
            stats["refreshed"] += 1

        with conn.cursor() as cur:
            _release_reconcile_lock(cur)
        conn.commit()

        if stats["aged_out"] or stats["refreshed"]:
            logger.info(
                "pool reconcile: %s attempts aged to no_response, %s rollups refreshed",
                stats["aged_out"],
                stats["refreshed"],
            )
        return stats
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
            with conn.cursor() as cur:
                _release_reconcile_lock(cur)
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        logger.error("pool reconcile failed: %s", e)
        return stats
    finally:
        conn.close()


async def reconcile_pool_state_async() -> Dict[str, int]:
    """Scheduler entry point — keeps the sync DB work off the event loop."""
    import asyncio

    return await asyncio.to_thread(reconcile_pool_state)
