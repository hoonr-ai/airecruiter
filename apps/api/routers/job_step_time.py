"""Wizard step timing: the write side of the reports' "Step 5 Active Time".

POST /api/v1/jobs/{job_id}/step-time   body {step, active_ms}

The job wizard (apps/web/hooks/use-step-active-time.ts) reports ACTIVE time on
Step 5 as a delta about once a minute, plus a zero-ms ENTRY report when the
step opens, which stamps first_entered_at. services/job_step_time owns the
table, the per-report clamp and the read the reports share. This router only
authenticates, resolves the job and records.

The path lives under /api/ ON PURPOSE. nginx (nginx-app-locations.conf) only
forwards `/jobs/{id}/<subpath>` for an explicit allowlist of subpaths, and
anything missing from it 404s as a Next.js HTML page. `/api/` is a plain
passthrough, so a new endpoint here needs no nginx change.

This is telemetry that runs underneath the wizard, so it must never break it.
Unknown jobs and identities that are not a person are ignored, and a DB
failure, in the access check or in the write, is logged and answered with
200 {"status": "error"} rather than a 500 or a 403. The client keeps the
unsent time and retries it on its next flush. Only a real access denial is a
403, which the client drops.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import UserIdentity, get_current_user, verify_job_access
from routers._helpers import get_db_connection
from routers.jobs import _get_job_draft_sync
from services.job_attribution import normalize_actor_email
from services.job_step_time import record_step_time

router = APIRouter(prefix="/api/v1", tags=["Job Step Time"])
logger = logging.getLogger(__name__)

# The wizard has five steps. Only Step 5 is reported today, but the table is
# keyed by step, so any real step is accepted.
MIN_STEP = 1
MAX_STEP = 5

_RESOLVE_JOB_SQL = "SELECT job_id FROM monitored_jobs WHERE job_id::text = %s OR jobdiva_id = %s LIMIT 1"


class StepTimeReport(BaseModel):
    step: int = Field(..., ge=MIN_STEP, le=MAX_STEP)
    # Negative is a client bug and is rejected. Oversized values (a clock
    # jump, a replayed request) are accepted and clamped to
    # MAX_ACTIVE_MS_PER_REPORT by record_step_time. Rejecting them would make
    # the client retry the same chunk forever.
    active_ms: int = Field(..., ge=0)


def _check_access_sync(job_ref: str, user: UserIdentity) -> None:
    """routers.jobs._verify_job_access_by_id(job_ref, user, allow_not_found=True),
    except that a failed lookup RAISES instead of becoming a 403.

    That helper turns every error from the draft lookup (pool exhausted,
    connect timeout, DB down) into a 403, and for the client a 403 is final:
    it drops the chunk. For this endpoint a DB blip must read as "try again",
    or a short outage throws away every open Step 5's pending time. So the
    lookup runs here, and the access rule itself stays the shared
    verify_job_access. Raises HTTPException(403) on a real denial, and
    anything else when the lookup fails.
    """
    if user.is_admin:
        return
    draft = _get_job_draft_sync(job_ref)  # raises on DB errors
    if draft.get("status") == "success" and draft.get("data"):
        verify_job_access(draft["data"], user)
        return
    if draft.get("status") == "error" and "No data found" in str(draft.get("message", "")):
        # Not saved yet: let it through, as allow_not_found does. The resolve
        # in _record_sync then ignores it.
        return
    # A shape the helper would also refuse. Deny, the same way.
    raise HTTPException(
        status_code=403,
        detail="Access denied. You do not have permission to access or modify this job.",
    )


def _record_sync(job_ref: str, step: int, email: str, active_ms: int) -> Optional[str]:
    """Resolve the canonical monitored_jobs.job_id and record. Returns it, or
    None when no job matches. Raises on DB errors."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5000ms'")
            cur.execute(_RESOLVE_JOB_SQL, (job_ref, job_ref))
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        job_id = str(row[0])
        # The job_step_time key is the canonical id, never the ref the page
        # happened to hold (the wizard can hold either one). That way the
        # reports' fetch_step_metrics joins on a single key.
        record_step_time(conn, job_id, step, email, active_ms)
        return job_id
    finally:
        conn.close()


@router.post("/jobs/{job_id}/step-time")
async def record_job_step_time(
    job_id: str,
    report: StepTimeReport,
    user: UserIdentity = Depends(get_current_user),
):
    job_ref = (job_id or "").strip()
    email = normalize_actor_email(user.email)
    if not job_ref or not email:
        # No job, or not a person: the local-dev unauthenticated identity must
        # not collect time that the reports would credit to someone.
        return {"status": "ignored"}

    # The guard the wizard's own job reads use, minus its "lookup failed ->
    # 403" (see _check_access_sync). It runs a DB read, so it goes off the
    # event loop, because this endpoint is hit once a minute by every open
    # Step 5. A real denial still propagates as a 403. Either way out of
    # here, nothing is written.
    try:
        await asyncio.to_thread(_check_access_sync, job_ref, user)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[JobStepTime] could not check access to job {job_ref}: {e}")
        return {"status": "error"}

    try:
        recorded = await asyncio.to_thread(_record_sync, job_ref, report.step, email, report.active_ms)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[JobStepTime] could not record step {report.step} time for job {job_ref} "
            f"({report.active_ms}ms): {e}"
        )
        return {"status": "error"}

    if recorded is None:
        return {"status": "ignored"}
    return {"status": "success"}
