"""Engagement package: models."""

from pydantic import BaseModel, Field
from typing import Optional, List


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------
class GeneratePayloadRequest(BaseModel):
    candidate_ids: List[str]
    job_id: str

class SendBulkInterviewRequest(BaseModel):
    payload: str  # JSON string (editable by user in modal)
    real_candidate_ids: List[str]
    is_initial_launch: bool = False
    dry_run: bool = False
    # Wizard "Launch PAIR" sends this to trigger the recruiter launch email.
    # Manual rankings Engage clicks leave it false so we don't spam emails.
    # Previously, dry_run did double duty (skip pairbot + fire email), which
    # silently caused bulk launches to never reach pairbot.
    notify_recruiters: bool = False
    # When provided by the caller, controls Email #2 (job posting request)
    # explicitly so batched launches can fire it once on the final successful
    # batch only.
    send_job_posting_email: Optional[bool] = None
    app_base_url: str = ""
    # Recruiter warm handoff:
    # after its last question the bot holds the call for the launch's
    # recruiter (the server-resolved recruiter_email) instead of signing off.
    recruiter_handoff: bool = False
    handoff_timeout_sec: Optional[int] = Field(default=None, ge=60, le=1800)


# ---------------------------------------------------------------------------
# Backend-owned Launch PAIR orchestration (single call + SSE)
# ---------------------------------------------------------------------------
class LaunchRequest(BaseModel):
    """IDs-only launch request. Résumés do NOT ride here — they are persisted
    separately via /candidates/save — so this body stays small and is not
    nginx body-limit sensitive."""
    job_id: str
    candidate_ids: List[str]
    is_initial_launch: bool = False
    notify_recruiters: bool = False
    send_job_posting_email: Optional[bool] = None
    app_base_url: str = ""
    batch_size: Optional[int] = None
    # Fix 3: frontend `launchAsync` opt-in. Honoured only when the server's
    # LAUNCH_ASYNC flag is on; otherwise the SSE response is returned as before.
    async_launch: bool = False
    # Optional link to a server launch session (launch timings for the legacy
    # client-driven flow). ASYNC PATH ONLY: stored on
    # launch_runs.launch_session_id, and launch_runs rows exist only for async
    # launches. The default SSE path (LAUNCH_ASYNC off) has no run row to
    # carry it, so there it is only logged. No other behaviour change.
    launch_session_id: Optional[str] = None
    # Recruiter warm handoff: see SendBulkInterviewRequest.
    recruiter_handoff: bool = False
    handoff_timeout_sec: Optional[int] = Field(default=None, ge=60, le=1800)


# ---------------------------------------------------------------------------
# Re-Provision endpoint
# Backfills all launched (engage_status='sent') candidates for a job that do
# not yet have a jobdiva_candidate_id.  Call this after a bulk launch to
# ensure all candidates are registered as JobDiva applicants so Reject/Submit
# and action-notes work correctly.
# ---------------------------------------------------------------------------
class ReProvisionRequest(BaseModel):
    job_id: str
    # If True, provisions ALL launched candidates (even those already provisioned).
    # Defaults to False = only candidates missing a jobdiva_candidate_id.
    force_all: bool = False


class JobDivaProfileAuditRepairRequest(BaseModel):
    job_id: Optional[str] = None


class JobDivaBlankProfileBackfillRequest(BaseModel):
    job_id: Optional[str] = None
    jobdiva_ids: Optional[List[str]] = None
    # Default True: report what each profile WOULD get. Pass false to write.
    dry_run: bool = True
    # Profiles per call (max 50), processed one at a time: JobDiva's BI résumé
    # endpoints rate-limit hard.
    limit: int = 10


class ApplicantOriginRepairRequest(BaseModel):
    job_id: Optional[str] = None
    # Default True: report what WOULD be merged/deleted. Pass false to apply.
    dry_run: bool = True
