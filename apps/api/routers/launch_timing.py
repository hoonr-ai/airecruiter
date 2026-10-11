"""Launch timings for one job: the read for job pages, and the legacy click stamp.

GET  /api/v1/jobs/{job_id}/launch-timing   T0 / T1 / T2, Added → Click (A),
                                           Click → Launch (B), and each launch
                                           session's stage breakdown
POST /api/v1/jobs/{job_id}/launch-clicked  T1 from the legacy browser-driven
                                           Source & Launch flow (the server
                                           launch session stamps its own)

services/launch_timing owns the columns, the definitions and the reads; this
router only authenticates, checks job access and calls it. ``job_id`` is the
canonical monitored_jobs.job_id or the JobDiva ref.

The paths live under /api/ on purpose: nginx forwards `/jobs/{id}/<subpath>`
only for an allowlist, while `/api/` is a plain passthrough, so these need no
nginx change (see routers/job_step_time.py).

Both are telemetry around the wizard and must never break it. A DB failure, in
the access check or in the read/write, answers 200 {"status": "error"} rather
than a 500 or a 403; the click stamp is fire-and-forget for the page. Only a
real access denial is a 403, and an unknown job a 404 on the read.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import UserIdentity, get_current_user, verify_job_access
from routers._helpers import _get_job_draft_sync
from services import launch_timing

router = APIRouter(prefix="/api/v1", tags=["Launch Timing"])
logger = logging.getLogger(__name__)


def _check_access_sync(job_ref: str, user: UserIdentity) -> None:
    """routers._helpers._verify_job_access_by_id(job_ref, user,
    allow_not_found=True), except that a failed lookup RAISES instead of
    becoming a 403 — the same distinction as routers/job_step_time: for these
    endpoints a DB blip must read as "try again", not as a final denial.
    Raises HTTPException(403) on a real denial, and anything else when the
    lookup fails. A job not saved yet is let through; the service then finds
    no row (404 on the read, "ignored" on the stamp).
    """
    if user.is_admin:
        return
    draft = _get_job_draft_sync(job_ref)  # raises on DB errors
    if draft.get("status") == "success" and draft.get("data"):
        verify_job_access(draft["data"], user)
        return
    if draft.get("status") == "error" and "No data found" in str(draft.get("message", "")):
        return
    raise HTTPException(
        status_code=403,
        detail="Access denied. You do not have permission to access or modify this job.",
    )


async def _access_or_error(job_ref: str, user: UserIdentity) -> bool:
    """True when access is granted; False when the check itself failed (the
    caller answers {"status": "error"}). A real denial raises the 403."""
    try:
        await asyncio.to_thread(_check_access_sync, job_ref, user)
        return True
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LaunchTiming] could not check access to job {job_ref}: {e}")
        return False


@router.get("/jobs/{job_id}/launch-timing")
async def get_job_launch_timing(
    job_id: str,
    user: UserIdentity = Depends(get_current_user),
):
    """services.launch_timing.job_timing for one job: ISO UTC timestamps,
    durations in seconds and minutes (None = not recorded), and the job's
    launch sessions with their stage breakdown."""
    job_ref = (job_id or "").strip()
    if not job_ref:
        raise HTTPException(status_code=404, detail="Job not found")
    if not await _access_or_error(job_ref, user):
        return {"status": "error", "data": None}
    try:
        timing = await asyncio.to_thread(launch_timing.job_timing, job_ref)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LaunchTiming] could not read launch timings for job {job_ref}: {e}")
        return {"status": "error", "data": None}
    if timing is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "success", "data": timing}


@router.post("/jobs/{job_id}/launch-clicked")
async def record_launch_clicked(
    job_id: str,
    user: UserIdentity = Depends(get_current_user),
):
    """Stamp the job's first Launch click (T1) at the server's clock — a no-op
    when an earlier click is recorded, or when the job has already launched
    (a relaunch is not the first click). The legacy flow calls it when the
    recruiter clicks "Source & Launch PAIR"; the page does not wait on it."""
    job_ref = (job_id or "").strip()
    if not job_ref:
        return {"status": "ignored"}
    if not await _access_or_error(job_ref, user):
        return {"status": "error"}
    # stamp_launch_clicked never raises: a failed write is logged there and
    # reads as None, the same as an unknown job.
    stamped = await asyncio.to_thread(launch_timing.stamp_launch_clicked, job_ref)
    if stamped is None:
        return {"status": "ignored"}
    return {"status": "success", "job_id": stamped}
