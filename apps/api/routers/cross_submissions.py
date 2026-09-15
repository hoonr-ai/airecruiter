"""Cross submissions endpoints — see services/cross_submissions.py.

GET  /jobs/{job}/cross-submissions       → the stored list for this job
POST /jobs/{job}/cross-submissions/run   → force a scan now (optionally email)

Both are job-scoped: `Depends(get_current_user)` + `_verify_job_access_by_id`
(this app has no global auth middleware — every route guards itself).
New /jobs/{id}/<subpath> routes must also be in the nginx allowlist regex
(nginx-app-locations.conf) or nginx hands them to Next.js as an HTML 404.
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import UserIdentity, get_current_user
from routers.jobs import _verify_job_access_by_id
from services import cross_submissions

logger = logging.getLogger(__name__)

router = APIRouter()


class RunCrossSubmissionsRequest(BaseModel):
    send_email: bool = True


def _frontend_origin(request: Request) -> str:
    origin = request.headers.get("origin") or ""
    if origin.startswith("http://") or origin.startswith("https://"):
        return origin
    referer = request.headers.get("referer") or ""
    if referer.startswith("http://") or referer.startswith("https://"):
        parts = referer.split("/")
        return "/".join(parts[:3])
    return ""


@router.get("/jobs/{job_id_or_ref}/cross-submissions")
async def list_cross_submissions(job_id_or_ref: str, user: UserIdentity = Depends(get_current_user)) -> Dict[str, Any]:
    _verify_job_access_by_id(job_id_or_ref, user)
    try:
        rows = await asyncio.to_thread(cross_submissions.list_for_job, job_id_or_ref)
    except Exception as e:  # noqa: BLE001
        logger.error("cross_submissions list failed for %s: %s", job_id_or_ref, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load cross submissions")
    return {"job_ref": job_id_or_ref, "count": len(rows), "candidates": rows}


@router.post("/jobs/{job_id_or_ref}/cross-submissions/run")
async def run_cross_submissions(
    job_id_or_ref: str,
    request: Request,
    body: Optional[RunCrossSubmissionsRequest] = None,
    user: UserIdentity = Depends(get_current_user),
) -> Dict[str, Any]:
    """Force a scan for this job using its saved Step-5 sourcing filters.

    Bypasses the per-job throttle. Persons already surfaced for this job are
    not re-emailed (the cross_submissions row is the claim); the response
    still lists everyone who matched this run.
    """
    _verify_job_access_by_id(job_id_or_ref, user)
    body = body or RunCrossSubmissionsRequest()
    # Late import: routers.candidates is a heavy module and imports routers.jobs;
    # importing it here (not at module load) keeps router boot order simple.
    from routers.candidates import (
        _build_resume_matching_criteria,
        _compute_resume_matching,
        _warm_resume_matching,
    )

    try:
        criteria = await asyncio.to_thread(_build_resume_matching_criteria, job_id_or_ref)
        summary = await cross_submissions.run_for_job_async(
            job_id_or_ref,
            criteria,
            _compute_resume_matching,
            warmer=_warm_resume_matching,
            force=True,
            send_email=bool(body.send_email),
            app_base_url=_frontend_origin(request),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("cross_submissions run failed for %s: %s", job_id_or_ref, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Cross submissions run failed")
    if criteria is None:
        summary["criteria_missing"] = True
    return summary


@router.post("/jobs/{job_id_or_ref}/cross-submissions/{cs_id}/add")
async def add_cross_submission_to_job(
    job_id_or_ref: str,
    cs_id: int,
    user: UserIdentity = Depends(get_current_user),
) -> Dict[str, Any]:
    """Copy the previously screened candidate into this job's candidate pool.

    The new row has no outreach state (Pending on the rank list) and goes
    through the normal Launch PAIR gate. Idempotent.
    """
    _verify_job_access_by_id(job_id_or_ref, user)
    try:
        result = await asyncio.to_thread(
            cross_submissions.add_to_job, job_id_or_ref, cs_id, added_by=str(getattr(user, "email", "") or ""),
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error("cross_submissions add failed for %s/%s: %s", job_id_or_ref, cs_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add candidate to this job")
    try:
        from routers.jobs import invalidate_monitored_jobs_cache
        invalidate_monitored_jobs_cache()
    except Exception:
        pass
    return result
