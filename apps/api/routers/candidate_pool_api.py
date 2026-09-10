"""HTTP surface for the global candidate pool.

Routes (all under `/candidates/…`, which nginx forwards to this API — note the
`location ~ ^/candidates/.+` rule means every sub-path here reaches the backend,
while the bare `/candidates` page falls through to Next.js):

    GET  /candidates/pool           sectioned, keyset-paginated person list
    GET  /candidates/pool/summary   per-bucket counts
    GET  /candidates/pool/person/{person_id}   one person + outreach history
    POST /candidates/reuse-preview  tier decisions for a job's selection

Visibility model
----------------
Everyone authenticated sees every *person* — that is the point, since the pool
exists to stop two recruiters calling the same human about two jobs. What is
scoped is *job attribution*: if the job someone was last contacted for is not in
your scope, its title and client are replaced by "Another job". The suppression
reason is always shown in full, because a block the recruiter can't understand is
worse than one they can.

Every endpoint carries `Depends(get_current_user)`. There is no global auth
middleware in this app — an endpoint without that dependency is an unauthenticated
hole in production.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth import UserIdentity, get_current_user, get_user_scope_emails
from core.db import get_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)

# Section order the UI renders top to bottom. `never_contacted` is deliberately
# NOT a section: it is the overwhelming majority of rows and browsing it tells a
# recruiter nothing. It is reachable only with an explicit bucket + search.
POOL_BUCKETS: Tuple[str, ...] = (
    "in_progress",
    "completed",
    "no_response",
    "suppressed",
    "never_contacted",
)

REDACTED_JOB_LABEL = "Another job"

# Per-process cache of the caller-independent (job_ref -> recruiter_emails) map.
# monitored_jobs is small (thousands of rows) and recruiter_emails is a JSON TEXT
# column, so scoping is computed in Python exactly the way
# routers/jobs._filter_jobs_for_user does. Cached because the pool list would
# otherwise re-read the whole table on every page and every section.
_SCOPE_TTL_SECONDS = 60
_scope_lock = threading.Lock()
_scope_cache: Optional[Tuple[float, Dict[str, List[str]]]] = None


def _load_job_recruiters() -> Dict[str, List[str]]:
    """`{job_ref: [recruiter_email, …]}` keyed by BOTH jobdiva_id and job_id.

    `candidate_outreach.jobdiva_id` stores whichever spelling the launch used, so
    both have to resolve.
    """
    global _scope_cache
    with _scope_lock:
        if _scope_cache and time.monotonic() - _scope_cache[0] < _SCOPE_TTL_SECONDS:
            return _scope_cache[1]

    mapping: Dict[str, List[str]] = {}
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT jobdiva_id, job_id, recruiter_emails FROM monitored_jobs"
            )
            for jobdiva_id, job_id, raw in cur.fetchall() or []:
                emails: List[str] = []
                if isinstance(raw, list):
                    emails = [str(e).strip().lower() for e in raw if e]
                elif isinstance(raw, str) and raw.strip():
                    try:
                        parsed = json.loads(raw) if raw.strip().startswith("[") else [raw]
                    except Exception:  # noqa: BLE001
                        parsed = [raw]
                    emails = [str(e).strip().lower() for e in parsed if e]
                for key in (jobdiva_id, job_id):
                    if key:
                        mapping[str(key)] = emails
    except Exception as e:  # noqa: BLE001
        logger.warning("pool: job scope load failed (treating all jobs as scoped out): %s", e)
        return {}
    finally:
        conn.close()

    with _scope_lock:
        _scope_cache = (time.monotonic(), mapping)
    return mapping


def _visible_job_refs(user: UserIdentity) -> Optional[Set[str]]:
    """Job refs whose title/client this user may see. None means "all of them"."""
    if user.is_admin:
        return None
    allowed = {e.lower() for e in get_user_scope_emails(user)}
    mapping = _load_job_recruiters()
    visible = set()
    for job_ref, emails in mapping.items():
        # An unassigned job is visible to any authenticated recruiter, matching
        # verify_job_access's "legacy or unassigned" allowance.
        if not emails or allowed.intersection(emails):
            visible.add(job_ref)
    return visible


def _redact_job(row: Dict[str, Any], visible: Optional[Set[str]]) -> Dict[str, Any]:
    """Blank a job's identity when it is outside the caller's scope."""
    job_ref = str(row.get("last_job_id") or "")
    if not job_ref:
        return row
    if visible is None or job_ref in visible:
        row["last_job_in_scope"] = True
        return row
    row["last_job_in_scope"] = False
    row["last_job_id"] = None
    row["last_job_title"] = REDACTED_JOB_LABEL
    return row


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "person_id": row.get("person_id"),
        "bucket": row.get("bucket"),
        "display_name": row.get("display_name"),
        "headline": row.get("headline"),
        "location": row.get("location"),
        "has_contact": bool(row.get("has_contact")),
        "last_outreach_at": _iso(row.get("last_outreach_at")),
        "last_job_id": row.get("last_job_id"),
        "last_job_title": row.get("last_job_title"),
        "last_job_in_scope": row.get("last_job_in_scope", True),
        "last_tier": row.get("last_tier"),
        "last_completed_at": _iso(row.get("last_completed_at")),
        "last_outcome": row.get("last_outcome"),
        "last_score": float(row["last_score"]) if row.get("last_score") is not None else None,
        "attempts_90d": row.get("attempts_90d") or 0,
        "attempts_total": row.get("attempts_total") or 0,
        "next_eligible_at": _iso(row.get("next_eligible_at")),
        "suppressed_reason": row.get("suppressed_reason"),
        "facts_fresh_until": _iso(row.get("facts_fresh_until")),
    }


@router.get("/candidates/pool")
async def get_candidate_pool(
    bucket: str = Query("in_progress", pattern="^(in_progress|completed|no_response|suppressed|never_contacted)$"),
    search: Optional[str] = Query(None, max_length=120),
    limit: int = Query(50, ge=1, le=200),
    cursor_sort_at: Optional[str] = Query(None, description="ISO timestamp from the previous page's next_cursor"),
    cursor_person_id: Optional[int] = Query(None),
    user: UserIdentity = Depends(get_current_user),
):
    """One section of the pool, newest activity first.

    Keyset pagination, not OFFSET: `(sort_at, person_id) < (cursor…)` matches the
    `(bucket, sort_at DESC, person_id DESC)` index exactly, so page N costs the
    same as page 1. The old master-pool endpoint's two `DISTINCT ON` passes over
    all of `sourced_candidates` are what made that page unusable; nothing here
    touches that table.
    """
    params: Dict[str, Any] = {"bucket": bucket, "limit": limit}
    where = ["bucket = %(bucket)s"]

    if search and search.strip():
        params["search"] = f"%{search.strip().lower()}%"
        # LOWER(display_name) is indexed (idx_cps_name_lower); a leading-wildcard
        # LIKE can't use it, but the bucket predicate already narrows the scan and
        # search is an explicit user action rather than the default page load.
        where.append("(LOWER(COALESCE(display_name, '')) LIKE %(search)s"
                     " OR LOWER(COALESCE(headline, '')) LIKE %(search)s"
                     " OR LOWER(COALESCE(location, '')) LIKE %(search)s)")

    if cursor_sort_at and cursor_person_id is not None:
        try:
            params["cursor_sort_at"] = datetime.fromisoformat(cursor_sort_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="cursor_sort_at must be an ISO timestamp")
        params["cursor_person_id"] = int(cursor_person_id)
        where.append("(sort_at, person_id) < (%(cursor_sort_at)s, %(cursor_person_id)s)")

    sql = f"""
        SELECT person_id, bucket, display_name, headline, location, has_contact,
               last_outreach_at, last_job_id, last_job_title, last_tier,
               last_completed_at, last_outcome, last_score, attempts_90d,
               attempts_total, next_eligible_at, suppressed_reason,
               facts_fresh_until, sort_at
        FROM candidate_pool_state
        WHERE {' AND '.join(where)}
        ORDER BY sort_at DESC, person_id DESC
        LIMIT %(limit)s
    """

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall() or []]
    except Exception as e:  # noqa: BLE001
        logger.error("get_candidate_pool failed (bucket=%s): %s", bucket, e)
        raise HTTPException(status_code=500, detail="Could not load the candidate pool")
    finally:
        conn.close()

    visible = _visible_job_refs(user)
    last = rows[-1] if rows else None
    return {
        "status": "success",
        "bucket": bucket,
        "candidates": [_serialize(_redact_job(r, visible)) for r in rows],
        "next_cursor": (
            {
                "cursor_sort_at": _iso(last.get("sort_at")),
                "cursor_person_id": last.get("person_id"),
            }
            if last and len(rows) == limit
            else None
        ),
    }


@router.get("/candidates/pool/summary")
async def get_candidate_pool_summary(
    user: UserIdentity = Depends(get_current_user),
):
    """Per-bucket counts for the section headers.

    A grouped aggregate over the whole rollup — one row per bucket, five rows
    total. Counts are global (not scoped), matching the visibility model: you can
    see that a person exists and when they were last contacted; you just may not
    see which job it was for.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bucket, COUNT(*) FROM candidate_pool_state GROUP BY bucket")
            counts = {r[0]: int(r[1]) for r in cur.fetchall() or []}
    except Exception as e:  # noqa: BLE001
        logger.error("get_candidate_pool_summary failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not load pool counts")
    finally:
        conn.close()

    return {
        "status": "success",
        "counts": {b: counts.get(b, 0) for b in POOL_BUCKETS},
        "total": sum(counts.values()),
    }


@router.get("/candidates/pool/person/{person_id}")
async def get_pool_person(
    person_id: int,
    user: UserIdentity = Depends(get_current_user),
):
    """One person: rollup row, full outreach history, and reusable facts."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.*, p.primary_email, p.primary_phone, p.profile_url,
                       p.primary_key_type
                FROM candidate_pool_state s
                JOIN candidate_person p USING (person_id)
                WHERE s.person_id = %s
                """,
                (person_id,),
            )
            state = cur.fetchone()
            if not state:
                raise HTTPException(status_code=404, detail="Person not found in the pool")

            cur.execute(
                """
                SELECT o.jobdiva_id, o.tier, o.screening_level, o.status, o.sent_at,
                       o.first_activity_at, o.completed_at, o.outcome, o.score,
                       o.interview_id,
                       (SELECT COALESCE(mj.enhanced_title, mj.title)
                          FROM monitored_jobs mj
                         WHERE mj.jobdiva_id = o.jobdiva_id OR mj.job_id = o.jobdiva_id
                         ORDER BY (mj.job_id ~ '^[0-9]+$') DESC, mj.created_at DESC
                         LIMIT 1) AS job_title
                FROM candidate_outreach o
                WHERE o.person_id = %s
                ORDER BY o.sent_at DESC
                LIMIT 50
                """,
                (person_id,),
            )
            history = [dict(r) for r in cur.fetchall() or []]

            cur.execute(
                """
                SELECT fact_key, fact_value, captured_at, expires_at
                FROM candidate_screen_fact
                WHERE person_id = %s
                ORDER BY fact_key
                """,
                (person_id,),
            )
            facts = [dict(r) for r in cur.fetchall() or []]
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("get_pool_person failed for %s: %s", person_id, e)
        raise HTTPException(status_code=500, detail="Could not load the candidate")
    finally:
        conn.close()

    visible = _visible_job_refs(user)
    state_row = _serialize(_redact_job(dict(state), visible))
    state_row.update({
        "primary_email": state.get("primary_email"),
        "primary_phone": state.get("primary_phone"),
        "profile_url": state.get("profile_url"),
        "primary_key_type": state.get("primary_key_type"),
    })

    def _history_row(row: Dict[str, Any]) -> Dict[str, Any]:
        in_scope = visible is None or str(row.get("jobdiva_id") or "") in visible
        return {
            "jobdiva_id": row.get("jobdiva_id") if in_scope else None,
            "job_title": row.get("job_title") if in_scope else REDACTED_JOB_LABEL,
            "job_in_scope": in_scope,
            "tier": row.get("tier"),
            "screening_level": row.get("screening_level"),
            "status": row.get("status"),
            "sent_at": _iso(row.get("sent_at")),
            "first_activity_at": _iso(row.get("first_activity_at")),
            "completed_at": _iso(row.get("completed_at")),
            "outcome": row.get("outcome"),
            "score": float(row["score"]) if row.get("score") is not None else None,
            # interview_id is only meaningful for a job you can see.
            "interview_id": row.get("interview_id") if in_scope else None,
        }

    from services import screen_facts

    return {
        "status": "success",
        "person": state_row,
        "history": [_history_row(r) for r in history],
        "facts": [
            {
                "fact_key": f["fact_key"],
                "label": (
                    screen_facts.spec_for(f["fact_key"]).label
                    if screen_facts.spec_for(f["fact_key"])
                    else f["fact_key"]
                ),
                "fact_value": f["fact_value"],
                "captured_at": _iso(f["captured_at"]),
                "expires_at": _iso(f["expires_at"]),
                "is_fresh": bool(
                    f["expires_at"] and f["expires_at"] > datetime.now(f["expires_at"].tzinfo)
                ),
            }
            for f in facts
        ],
    }


class ReusePreviewRequest(BaseModel):
    job_id: str
    candidate_ids: List[str]


@router.post("/candidates/reuse-preview")
async def reuse_preview(
    request: ReusePreviewRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """What each selected candidate would get if launched right now.

    Read-only in the sense that matters to the recruiter — it sends nothing — but
    it does resolve identities, so `candidate_person` rows may be created. That is
    the same resolution the launch would do, just earlier, and it is idempotent.

    The wizard calls this before Launch PAIR so the recruiter can see "42 full
    screens, 18 short re-checks, 6 reused, 4 on cooldown" and override with
    `force_full_screen` if they disagree.
    """
    import asyncio

    from core.auth import verify_job_access
    from services import reuse_policy

    # Reuse preview is job-scoped data, so unlike the pool list this DOES enforce
    # job access — it reveals who is in a specific job's pipeline.
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT recruiter_emails FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
                LIMIT 1
                """,
                (request.job_id, request.job_id),
            )
            job_row = cur.fetchone()
    finally:
        conn.close()

    if job_row:
        verify_job_access(dict(job_row), user)

    ids = [str(c).strip() for c in (request.candidate_ids or []) if str(c).strip()]
    if not ids:
        return {
            "status": "success",
            "tier_counts": {"full": 0, "recheck": 0, "none": 0, "suppressed": 0},
            "decisions": [],
        }

    from routers.engagement import _load_candidates_for_identity

    try:
        rows = await asyncio.to_thread(_load_candidates_for_identity, ids)
        decisions = await asyncio.to_thread(
            reuse_policy.decide_tiers_for_job, rows, request.job_id
        )
    except Exception as e:  # noqa: BLE001
        logger.error("reuse_preview failed for job %s: %s", request.job_id, e)
        raise HTTPException(status_code=500, detail="Could not compute the reuse preview")

    visible = _visible_job_refs(user)
    ordered = []
    for cid in ids:
        decision = decisions.get(cid) or reuse_policy.TierDecision(candidate_id=cid)
        if visible is not None and decision.prior_jobdiva_id:
            decision.prior_job_redacted = str(decision.prior_jobdiva_id) not in visible
        ordered.append(decision)

    return {
        "status": "success",
        "tier_counts": reuse_policy.summarize(ordered),
        "decisions": [d.to_dict() for d in ordered],
    }
