"""Queue task definitions. Import this module to register them with core.queue.

Keep each task a thin, idempotent wrapper over existing service code; the
API (enqueue side) and the worker both import this module, so the registry is
the same in both processes. Tasks take plain arguments: the worker strips
arq's ``ctx`` before calling them.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.queue import task

PROVISION_JOBDIVA_BATCH = "provision_jobdiva_batch"


@task(PROVISION_JOBDIVA_BATCH, max_tries=2, timeout_s=1800)
async def provision_jobdiva_batch(candidate_ids: List[str], job_id: str) -> Dict[str, Any]:
    """Create the JobDiva applications for a launched batch.

    Retry-safe: `_provision_batch_to_jobdiva` re-reads the job's applicant list
    and skips candidates JobDiva already has.
    """
    from core.pipeline_trace import span as _trace_span
    from routers.engagement import provisioning

    async with _trace_span("jobdiva_provisioning", n=len(candidate_ids)):
        return await provisioning._provision_batch_to_jobdiva(list(candidate_ids), str(job_id))


# The tasks below reach their service objects through the module that called
# them before the queue existed (e.g. ``provisioning.jobdiva_service``), so
# tests that patch those module attributes still intercept the call.

SYNC_JOB_APPLICANTS = "sync_job_applicants"
SEND_PAIR_LAUNCH_EMAIL = "send_pair_launch_email"
UPDATE_JOBDIVA_QUALIFICATION = "update_jobdiva_qualification"
CREATE_JOBDIVA_PASS_NOTE = "create_jobdiva_pass_note"
REFRESH_JOB_METRICS = "refresh_job_metrics"
AUTO_LAUNCH_CANDIDATES = "auto_launch_candidates"
ZERO_MATCH_NOTIFICATION = "zero_match_notification"


@task(SYNC_JOB_APPLICANTS, max_tries=2, timeout_s=1800)
async def sync_job_applicants(job_id: str) -> Any:
    """Pull a job's JobDiva applicants. Upserts, so a retry is harmless."""
    from routers.engagement import launch

    return await launch.auto_assign_service.synchronize_job_applicants(job_id)


@task(SEND_PAIR_LAUNCH_EMAIL, max_tries=1, timeout_s=300)
async def send_pair_launch_email(
    job_id: str, candidate_count: int, send_job_posting: bool, app_base_url: str, relaunch: bool,
) -> None:
    """Launch emails. Not retried: a retry after a partial send would email twice."""
    from routers.engagement import pairbot

    await pairbot._send_pair_launch_email(
        job_id=job_id,
        candidate_count=candidate_count,
        send_job_posting=send_job_posting,
        app_base_url=app_base_url,
        relaunch=relaunch,
    )


@task(UPDATE_JOBDIVA_QUALIFICATION, max_tries=3, timeout_s=300)
async def update_jobdiva_qualification(
    candidate_id: str, qualification_name: str, value: str, recruiter_id: int,
    update_date: str, qualification_type_id: int,
) -> Any:
    """Set a JobDiva qualification. Setting the same value twice is harmless."""
    from routers.engagement import provisioning

    return await provisioning.jobdiva_service.update_candidate_qualification(
        candidate_id=candidate_id,
        qualification_name=qualification_name,
        value=value,
        recruiter_id=recruiter_id,
        update_date=update_date,
        qualification_type_id=qualification_type_id,
    )


@task(CREATE_JOBDIVA_PASS_NOTE, max_tries=1, timeout_s=300)
async def create_jobdiva_pass_note(
    candidate_id: str, job_id: str, action: str, note_text: str, recruiter_id: int,
) -> None:
    """Create and pin the PAIR pass note. Not retried: a retry would add a second note."""
    from routers.engagement import provisioning

    svc = provisioning.jobdiva_service
    note_res = await svc.create_candidate_note(
        candidate_id=candidate_id,
        job_id=job_id,
        action=action,
        note_text=note_text,
        recruiter_id=recruiter_id,
    )
    if note_res.get("status") == "success":
        note_id = note_res.get("data")
        if note_id:
            await svc.pin_candidate_note(note_id=note_id, is_pinned=True)


@task(REFRESH_JOB_METRICS, max_tries=3, timeout_s=600)
async def refresh_job_metrics(job_id: str) -> Any:
    """Recompute a job's performance metrics. Pure recompute, safe to retry."""
    from routers.engagement import provisioning

    return await provisioning.auto_assign_service.refresh_job_performance_metrics(job_id)


@task(AUTO_LAUNCH_CANDIDATES, max_tries=1, timeout_s=3600)
async def auto_launch_candidates(candidate_ids: List[str], job_id: str) -> None:
    """Auto-launch newly synced applicants. Not retried: re-running a
    half-finished launch could send some candidates twice."""
    import routers.engagement as engagement

    await engagement.auto_launch_for_candidates(list(candidate_ids), job_id)


@task(ZERO_MATCH_NOTIFICATION, max_tries=1, timeout_s=120)
async def zero_match_notification(job_id: str, searcher_email: str) -> None:
    from services.notifications_service import create_zero_match_notification

    await create_zero_match_notification(job_id=job_id, searcher_email=searcher_email)
