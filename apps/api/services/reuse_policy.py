"""Decide how much outreach a candidate actually needs for a given job.

Four tiers
----------
``full``       Screen them properly. Unknown person, or nothing reusable.
``recheck``    Short L0.5 boolean call: confirm interest in *this* job, re-ask
               only the facts that have gone stale, plus this job's own
               role-specific hard filters.
``none``       Don't call at all. Surface them in the rank list with the prior
               screen's result for the recruiter to review.
``suppressed`` Must not be contacted right now — DNC, an unanswered attempt
               still inside its cooldown, or the 90-day attempt cap.

What ``none`` does and does not mean
------------------------------------
Tier C is a *recruiter review* state, never an auto-submit. Interest in role X is
not interest in role Y even at the same client, so the rank list shows these
candidates in their own group with the prior score, the prior job, and a one-click
escalate-to-recheck. The tier saves the phone call; it does not make the
submission decision.

Cross-client policy
-------------------
Logistics facts (work authorization, visa, arrangements, pay band, location,
current role) describe the person and travel between clients. Interest never
does. So with ``POOL_CROSS_CLIENT_REUSE_FACTS_ONLY`` on, a screen done for
Client A can downgrade a Client B job to ``recheck`` but never to ``none``.

Failure direction
-----------------
Every uncertain path resolves toward *more* outreach, not less. An unidentifiable
person, a missing fact, an unreadable prior result, a DB error — all produce
``full``. The only tier that reduces contact is one we can positively justify
from a completed screen we can see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.config import (
    CANDIDATE_POOL_REUSE_ENABLED,
    POOL_COOLDOWN_DAYS,
    POOL_CROSS_CLIENT_REUSE_FACTS_ONLY,
    POOL_MAX_ATTEMPTS_90D,
    POOL_SKIP_OUTREACH_DAYS,
)
from core.db import get_db_connection
from services import candidate_pool, screen_facts
from services.candidate_pool import PersonOutreachContext

logger = logging.getLogger(__name__)

TIER_FULL = "full"
TIER_RECHECK = "recheck"
TIER_NONE = "none"
TIER_SUPPRESSED = "suppressed"


@dataclass
class TierDecision:
    """Why one candidate lands in one tier, in a shape the UI can render."""

    candidate_id: str
    tier: str = TIER_FULL
    # Stable machine-readable code; the UI keys badges and copy off this.
    reason_code: str = "unknown_person"
    # One sentence for a recruiter, safe to show verbatim.
    reason: str = "Not screened before — full screen."
    person_id: Optional[int] = None

    prior_jobdiva_id: Optional[str] = None
    prior_job_title: Optional[str] = None
    prior_customer_name: Optional[str] = None
    prior_completed_at: Optional[datetime] = None
    prior_outcome: Optional[str] = None
    prior_score: Optional[float] = None
    # True when the prior screen belongs to a different client, so the recruiter
    # knows the reused facts are logistics-only.
    prior_is_other_client: bool = False
    # Set when the caller is not allowed to see the prior job's identity.
    prior_job_redacted: bool = False

    # Facts we could not reuse and will therefore re-ask in the recheck.
    stale_fact_keys: List[str] = field(default_factory=list)
    # Populated for suppressed candidates.
    next_eligible_at: Optional[datetime] = None
    attempts_90d: int = 0

    @property
    def contacts(self) -> bool:
        """Whether this tier results in an outbound screen."""
        return self.tier in (TIER_FULL, TIER_RECHECK)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "tier": self.tier,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "person_id": self.person_id,
            "prior_jobdiva_id": None if self.prior_job_redacted else self.prior_jobdiva_id,
            "prior_job_title": None if self.prior_job_redacted else self.prior_job_title,
            "prior_customer_name": None if self.prior_job_redacted else self.prior_customer_name,
            "prior_job_redacted": self.prior_job_redacted,
            "prior_completed_at": self.prior_completed_at.isoformat() if self.prior_completed_at else None,
            "prior_outcome": self.prior_outcome,
            "prior_score": self.prior_score,
            "prior_is_other_client": self.prior_is_other_client,
            "stale_fact_keys": self.stale_fact_keys,
            "stale_fact_labels": [
                (screen_facts.spec_for(k).label if screen_facts.spec_for(k) else k)
                for k in self.stale_fact_keys
            ],
            "next_eligible_at": self.next_eligible_at.isoformat() if self.next_eligible_at else None,
            "attempts_90d": self.attempts_90d,
        }


@dataclass
class JobContext:
    jobdiva_id: str
    job_id: Optional[str] = None
    title: Optional[str] = None
    customer_name: Optional[str] = None
    screening_level: Optional[str] = None

    @property
    def keys(self) -> Tuple[str, ...]:
        return tuple(k for k in (self.jobdiva_id, self.job_id) if k)


def load_job_context(job_id_or_ref: str, *, cur=None) -> JobContext:
    """Resolve a job ref to the fields the policy needs."""

    def _run(active_cur) -> JobContext:
        active_cur.execute(
            """
            SELECT job_id, jobdiva_id, COALESCE(enhanced_title, title), customer_name,
                   screening_level
            FROM monitored_jobs
            WHERE job_id = %s OR jobdiva_id = %s
            ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
            LIMIT 1
            """,
            (str(job_id_or_ref), str(job_id_or_ref)),
        )
        row = active_cur.fetchone()
        if not row:
            return JobContext(jobdiva_id=str(job_id_or_ref))
        return JobContext(
            job_id=row[0],
            jobdiva_id=row[1] or row[0] or str(job_id_or_ref),
            title=row[2],
            customer_name=row[3],
            screening_level=row[4],
        )

    if cur is not None:
        return _run(cur)
    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            return _run(own_cur)
    except Exception as e:  # noqa: BLE001
        logger.warning("load_job_context failed for %s: %s", job_id_or_ref, e)
        return JobContext(jobdiva_id=str(job_id_or_ref))
    finally:
        conn.close()


def _load_job_meta(job_refs: Sequence[str], *, cur=None) -> Dict[str, Dict[str, Any]]:
    """`{job_ref: {title, customer_name}}` for the prior jobs we cite.

    One query for the whole batch. Both `jobdiva_id` and `job_id` are indexed as
    lookup keys because `candidate_outreach.jobdiva_id` stores whichever spelling
    the launch used.
    """
    refs = sorted({str(r) for r in job_refs if r})
    if not refs:
        return {}

    def _run(active_cur) -> Dict[str, Dict[str, Any]]:
        active_cur.execute(
            """
            SELECT jobdiva_id, job_id, COALESCE(enhanced_title, title), customer_name
            FROM monitored_jobs
            WHERE jobdiva_id = ANY(%s) OR job_id = ANY(%s)
            """,
            (refs, refs),
        )
        out: Dict[str, Dict[str, Any]] = {}
        for row in active_cur.fetchall() or []:
            meta = {"title": row[2], "customer_name": row[3]}
            for key in (row[0], row[1]):
                if key:
                    out.setdefault(str(key), meta)
        return out

    if cur is not None:
        return _run(cur)
    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            return _run(own_cur)
    except Exception as e:  # noqa: BLE001
        logger.warning("_load_job_meta failed: %s", e)
        return {}
    finally:
        conn.close()


def _same_client(a: Optional[str], b: Optional[str]) -> bool:
    left = str(a or "").strip().lower()
    right = str(b or "").strip().lower()
    return bool(left) and left == right


def _days_ago(moment: datetime, now: datetime) -> int:
    return max(0, int((now - moment).total_seconds() // 86400))


def _humanize_days(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def decide_tier(
    *,
    candidate_id: str,
    person_id: Optional[int],
    job: JobContext,
    ctx: Optional[PersonOutreachContext],
    facts: Dict[str, screen_facts.StoredFact],
    job_meta: Dict[str, Dict[str, Any]],
    now: Optional[datetime] = None,
) -> TierDecision:
    """Pure decision function — no I/O, so it is directly unit-testable."""
    moment = now or datetime.now(timezone.utc)
    decision = TierDecision(candidate_id=str(candidate_id), person_id=person_id)

    if not CANDIDATE_POOL_REUSE_ENABLED:
        decision.tier = TIER_FULL
        decision.reason_code = "reuse_disabled"
        decision.reason = "Reuse is turned off — full screen."
        return decision

    # No identity means no history we can trust. Full screen, every time.
    if not person_id or ctx is None:
        return decision

    decision.attempts_90d = ctx.attempts_90d

    # ---- Suppression, highest precedence ----------------------------------
    if ctx.dnc_blocked:
        decision.tier = TIER_SUPPRESSED
        decision.reason_code = "dnc"
        decision.reason = "On the Do Not Contact list."
        return decision

    if ctx.has_open_attempt:
        open_job = ctx.open_attempt_jobdiva_id
        if open_job and open_job in job.keys:
            # This is the existing per-job idempotency rule, restated in tier
            # terms so the launch UI can explain it.
            decision.tier = TIER_SUPPRESSED
            decision.reason_code = "already_launched_this_job"
            decision.reason = "Already launched for this job — awaiting their response."
        else:
            meta = job_meta.get(str(open_job or ""), {})
            sent_days = _days_ago(ctx.last_sent_at, moment) if ctx.last_sent_at else 0
            decision.tier = TIER_SUPPRESSED
            decision.reason_code = "awaiting_response_other_job"
            decision.prior_jobdiva_id = open_job
            decision.prior_job_title = meta.get("title")
            decision.prior_customer_name = meta.get("customer_name")
            decision.reason = (
                f"Screen sent {_humanize_days(sent_days)} for another job and still "
                "awaiting their response."
            )
        decision.next_eligible_at = ctx.next_eligible_at()
        return decision

    if POOL_MAX_ATTEMPTS_90D > 0 and ctx.attempts_90d >= POOL_MAX_ATTEMPTS_90D:
        decision.tier = TIER_SUPPRESSED
        decision.reason_code = "attempt_cap"
        decision.reason = (
            f"{ctx.attempts_90d} outreach attempts in the last 90 days "
            f"(cap is {POOL_MAX_ATTEMPTS_90D})."
        )
        decision.next_eligible_at = ctx.next_eligible_at()
        return decision

    cooldown_until = ctx.cooldown_until()
    if cooldown_until and moment < cooldown_until:
        sent_days = _days_ago(ctx.last_sent_at, moment) if ctx.last_sent_at else 0
        decision.tier = TIER_SUPPRESSED
        decision.reason_code = "cooldown"
        decision.reason = (
            f"Contacted {_humanize_days(sent_days)} with no response — "
            f"cooling down for {POOL_COOLDOWN_DAYS} days."
        )
        decision.next_eligible_at = cooldown_until
        return decision

    # ---- Reuse ------------------------------------------------------------
    if not ctx.last_completed_at or ctx.last_outcome is None:
        # Attempts may exist, but nothing completed, so there is nothing to
        # reuse. Past cooldown, so a fresh full screen is the right move.
        decision.tier = TIER_FULL
        decision.reason_code = "no_completed_screen"
        decision.reason = "No completed screen on file — full screen."
        return decision

    prior_meta = job_meta.get(str(ctx.last_completed_jobdiva_id or ""), {})
    decision.prior_jobdiva_id = ctx.last_completed_jobdiva_id
    decision.prior_job_title = prior_meta.get("title")
    decision.prior_customer_name = prior_meta.get("customer_name")
    decision.prior_completed_at = ctx.last_completed_at
    decision.prior_outcome = ctx.last_outcome
    decision.prior_score = ctx.last_score

    same_client = _same_client(prior_meta.get("customer_name"), job.customer_name)
    decision.prior_is_other_client = not same_client

    completed_days = _days_ago(ctx.last_completed_at, moment)
    within_skip_window = ctx.last_completed_at >= moment - timedelta(days=POOL_SKIP_OUTREACH_DAYS)

    # Facts we cannot reuse get re-asked in the recheck.
    decision.stale_fact_keys = screen_facts.stale_or_missing(
        facts, screen_facts.PORTABLE_FACT_KEYS, now=moment
    )

    tier_c_blockers = screen_facts.stale_or_missing(
        facts, screen_facts.TIER_C_REQUIRED_FACT_KEYS, now=moment
    )

    cross_client_blocks_tier_c = POOL_CROSS_CLIENT_REUSE_FACTS_ONLY and not same_client

    if (
        within_skip_window
        and ctx.last_outcome == "pass"
        and not tier_c_blockers
        and not cross_client_blocks_tier_c
    ):
        decision.tier = TIER_NONE
        decision.reason_code = "recent_pass_same_client"
        title = prior_meta.get("title") or ctx.last_completed_jobdiva_id or "another role"
        decision.reason = (
            f"Passed a screen for {title} {_humanize_days(completed_days)} at the same "
            "client — review instead of calling again."
        )
        return decision

    decision.tier = TIER_RECHECK
    if ctx.last_outcome == "fail":
        decision.reason_code = "recheck_after_fail"
        decision.reason = (
            f"Screened {_humanize_days(completed_days)} and did not pass then — "
            "short re-check for this role."
        )
    elif cross_client_blocks_tier_c:
        decision.reason_code = "recheck_other_client"
        client = prior_meta.get("customer_name") or "another client"
        decision.reason = (
            f"Screened {_humanize_days(completed_days)} for {client} — reusing their "
            "logistics answers, short re-check to confirm interest in this role."
        )
    elif not within_skip_window:
        decision.reason_code = "recheck_stale_screen"
        decision.reason = (
            f"Screened {_humanize_days(completed_days)} — short re-check instead of a "
            "full screen."
        )
    else:
        decision.reason_code = "recheck_stale_facts"
        labels = [
            (screen_facts.spec_for(k).label if screen_facts.spec_for(k) else k)
            for k in tier_c_blockers
        ]
        decision.reason = (
            f"Screened {_humanize_days(completed_days)}, but "
            f"{', '.join(labels).lower()} needs confirming — short re-check."
        )
    return decision


def decide_tiers_for_job(
    candidates: Sequence[Dict[str, Any]],
    job_id_or_ref: str,
    *,
    now: Optional[datetime] = None,
    cur=None,
) -> Dict[str, TierDecision]:
    """Decide a tier for every candidate in one pass.

    `candidates` are sourced-candidate-shaped dicts (at minimum `candidate_id`,
    ideally with contact fields and `data` so identity resolution has something
    to work with).

    Cost is a fixed handful of queries regardless of batch size: identity
    resolution, outreach contexts, facts, and one job-metadata lookup. Any
    failure degrades the whole batch to `full`, which is the safe direction.
    """
    from services import candidate_identity

    out: Dict[str, TierDecision] = {}
    ids = [str(c.get("candidate_id") or c.get("id") or "").strip() for c in candidates]
    ids = [i for i in ids if i]
    if not ids:
        return out

    # Default everyone to a full screen, then upgrade as evidence allows.
    for cid in ids:
        out[cid] = TierDecision(candidate_id=cid)

    if not CANDIDATE_POOL_REUSE_ENABLED:
        for decision in out.values():
            decision.reason_code = "reuse_disabled"
            decision.reason = "Reuse is turned off — full screen."
        return out

    def _run(active_cur) -> Dict[str, TierDecision]:
        job = load_job_context(job_id_or_ref, cur=active_cur)

        person_by_cid = candidate_identity.resolve_persons_bulk(candidates, cur=active_cur)
        person_ids = [p for p in person_by_cid.values() if p]
        contexts = candidate_pool.get_outreach_contexts(person_ids, cur=active_cur)
        facts_by_person = screen_facts.load_facts_bulk(person_ids, cur=active_cur)

        # Every prior job we might cite, resolved in one query.
        referenced_jobs: set = set()
        for ctx in contexts.values():
            for ref in (
                ctx.last_jobdiva_id,
                ctx.last_completed_jobdiva_id,
                ctx.open_attempt_jobdiva_id,
            ):
                if ref:
                    referenced_jobs.add(str(ref))
        job_meta = _load_job_meta(sorted(referenced_jobs), cur=active_cur)

        for cid in ids:
            person_id = person_by_cid.get(cid)
            out[cid] = decide_tier(
                candidate_id=cid,
                person_id=person_id,
                job=job,
                ctx=contexts.get(person_id) if person_id else None,
                facts=facts_by_person.get(person_id, {}) if person_id else {},
                job_meta=job_meta,
                now=now,
            )
        return out

    if cur is not None:
        return _run(cur)

    conn = get_db_connection()
    try:
        with conn.cursor() as own_cur:
            result = _run(own_cur)
        # resolve_persons_bulk may have created person rows.
        conn.commit()
        return result
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error(
            "decide_tiers_for_job failed for job %s (%s candidates) — defaulting all "
            "to a full screen: %s",
            job_id_or_ref,
            len(ids),
            e,
        )
        return out
    finally:
        conn.close()


def summarize(decisions: Sequence[TierDecision]) -> Dict[str, int]:
    """Tier counts, for the SSE stream and the pre-launch summary."""
    counts = {TIER_FULL: 0, TIER_RECHECK: 0, TIER_NONE: 0, TIER_SUPPRESSED: 0}
    for d in decisions:
        counts[d.tier] = counts.get(d.tier, 0) + 1
    return counts
