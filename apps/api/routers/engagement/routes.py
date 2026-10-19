"""Engagement package: routes."""

import asyncio
import contextlib
import hashlib
import uuid
from fastapi import Depends, HTTPException, Request
from typing import Optional, List, Any, Dict
import psycopg2.extras
import json
import httpx
from routers._helpers import (
    get_db_connection,
    _verify_job_access_by_id,
)
from services.pair_auth import get_pair_auth_headers
from core.tasks import spawn as _spawn_task
from core.pipeline_trace import new_run as _trace_new_run
from core.auth import UserIdentity, get_current_user
from fastapi.responses import StreamingResponse

from .audits import _APPLICANT_TWIN_FOLD_KEYS, _APPLICANT_TWIN_JOIN, _JOBDIVA_PROFILE_AUDIT_WHERE, _TWIN_OF_ORIGIN, _audit_job_filter_sql, _audit_job_ids, _jobdiva_profile_audit_job_filter, _require_admin
from .common import EXTERNAL_INTERVIEW_API_URL, _log_recordings_upstream_failure, _nr_suppress_apdex, logger, router
from .launch import _authorize_launch_stream, _launch_append_event, _launch_finalize, _launch_flag, _launch_progress_stream, _normalise_launch_session_id, _strip_internal, _sweep_stale_launch_runs
from .models import ApplicantOriginRepairRequest, GeneratePayloadRequest, JobDivaBlankProfileBackfillRequest, JobDivaProfileAuditRepairRequest, LaunchRequest, ReProvisionRequest
from .recordings import _RECORDINGS_MAX_SESSIONS, _proxy_get, _proxy_post, _recording_audit_row
from . import bulk_send
from . import common
from . import launch
from . import models
from . import payloads
from . import provisioning


# ---------------------------------------------------------------------------
# 1. POST /engage/generate-payload
# ---------------------------------------------------------------------------
@router.post("/engage/generate-payload")
async def generate_engage_payload(request: GeneratePayloadRequest):
    """Thin HTTP wrapper. The QA/edit modal fetches an editable payload here;
    the batched launch orchestrator (`/engage/launch`) calls
    `_generate_payload_for` directly, with no browser round-trip."""
    return await payloads._generate_payload_for(request)


@router.post("/engage/send-bulk-interview")
async def send_bulk_interview(request: models.SendBulkInterviewRequest):
    """Thin HTTP wrapper — the QA/edit modal and rankings Engage clicks call
    this. The batched launch orchestrator calls `_send_bulk_interview_core`
    directly, once per internal batch."""
    return await bulk_send._send_bulk_interview_core(request)

@router.get("/engage/bulk-status/stream")
async def stream_engagement_status(
    request: Request,
    bulk_id: Optional[str] = None,
    launch_id: Optional[str] = None,
):
    """Proxy Pairbot's per-bulk SSE stream, or (Fix 3) replay the progress
    events of an async launch keyed by `launch_id`.

    The launch_id branch requires an authenticated user who requested the
    launch, is an admin, or otherwise has access to the launch's job. The
    bulk_id branch is unchanged."""
    from fastapi.responses import StreamingResponse
    import httpx

    _nr_suppress_apdex()
    if launch_id:
        await _authorize_launch_stream(request, launch_id)
        return StreamingResponse(
            _launch_progress_stream(launch_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if not bulk_id:
        raise HTTPException(status_code=400, detail="bulk_id or launch_id is required")

    external_url = f"{EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/{bulk_id}/stream"

    async def proxy_stream():
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream("GET", external_url, timeout=None) as response:
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                logger.error(f"Error proxying SSE stream for bulk_id {bulk_id}: {e}")
                yield b"data: {\"status\": \"error\"}\n\n"

    return StreamingResponse(proxy_stream(), media_type="text/event-stream")


@router.post("/engage/launch")
async def launch_bulk_interviews(request: LaunchRequest, http_request: Request = None):
    """Single-call, backend-owned Launch PAIR orchestration.

    Replaces the frontend's per-batch generate -> send -> SSE loop: the browser
    sends candidate_ids + job/options ONCE, and this endpoint streams aggregated
    progress (SSE) while batching to Pairbot server-side. It reuses the exact
    per-batch logic (`_generate_payload_for` + `_send_bulk_interview_core`), so
    idempotency / DNC / audit / status write-through behave identically to the
    old flow. The pipeline itself is `run_launch` (shared with server launch
    sessions); this endpoint only formats its events.

    The employer gate is resolved once for the whole launch (LAUNCH_GATE_ONCE),
    batches run LAUNCH_BATCH_PARALLELISM at a time (1 = old sequential order),
    and with LAUNCH_ASYNC + `async_launch` the work runs in a tracked
    background task and this returns 202 {launch_id}.

    Difference from the old flow (intentional de-dup): the recruiter launch
    email and applicant sync fire ONCE per launch here, instead of once per
    batch.
    """
    from fastapi.responses import JSONResponse, StreamingResponse

    _nr_suppress_apdex()

    def _sse(obj: Dict[str, Any]) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    # The recruiter on the Pairbot payload comes from the auth token only.
    requested_by: Optional[str] = None
    if http_request is not None:
        try:
            _user = await asyncio.to_thread(get_current_user, http_request, None)
            requested_by = (_user.email or "").strip().lower() or None
        except HTTPException as exc:
            logger.warning(
                "launch: no authenticated requester (status=%s); Pairbot payload will omit recruiter_email",
                exc.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch: could not resolve requester: %s", exc)

    async def _work(launch_id: Optional[str] = None, run_id: Optional[str] = None):
        async with contextlib.aclosing(
            launch.run_launch(request, launch_id=launch_id, run_id=run_id, recruiter_email=requested_by)
        ) as events:
            async for evt in events:
                yield _sse(_strip_internal(evt))

    # --- Fix 3: async path -------------------------------------------------
    if request.async_launch and _launch_flag("LAUNCH_ASYNC", False):
        launch_id = str(uuid.uuid4())
        run_id = _trace_new_run(request.job_id)
        try:
            # Lazy safety net for runs whose worker died without a final write.
            await asyncio.to_thread(_sweep_stale_launch_runs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_runs stale sweep failed: %s", exc)
        launch_session_id = _normalise_launch_session_id(request.launch_session_id)
        recorded = False
        if launch_session_id:
            try:
                await asyncio.to_thread(
                    launch._launch_db,
                    "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at, launch_session_id) "
                    "VALUES (%s, %s, %s, 'running', now(), %s)",
                    (launch_id, str(request.job_id), requested_by, launch_session_id),
                )
                recorded = True
            except Exception as exc:  # noqa: BLE001 — column missing on this DB: link dropped
                logger.warning("async launch: launch_session_id not recorded: %s", exc)
        if not recorded:
            try:
                await asyncio.to_thread(
                    launch._launch_db,
                    "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at) "
                    "VALUES (%s, %s, %s, 'running', now())",
                    (launch_id, str(request.job_id), requested_by),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("async launch could not be recorded: %s", exc, exc_info=True)
                raise HTTPException(status_code=503, detail="Could not start launch")

        async def _run_async() -> None:
            final = "completed"
            seq = {"n": 0}
            cancelled = False

            def _append_sync(evt: Dict[str, Any]) -> None:
                _launch_append_event(launch_id, seq["n"], evt)
                seq["n"] += 1

            try:
                async for chunk in _work(launch_id=launch_id, run_id=run_id):
                    evt = json.loads(chunk[len("data: "):].strip())
                    if evt.get("type") == "error":
                        final = "aborted"
                    await asyncio.to_thread(_append_sync, evt)
            except asyncio.CancelledError:
                # Shutdown drain (or similar) cancelled us. Awaiting a thread
                # here could itself be cancelled, so write synchronously.
                final = "failed"
                cancelled = True
                logger.warning("async launch %s cancelled", launch_id)
                try:
                    _append_sync({"type": "error", "message": "launch interrupted (server restart)"})
                except Exception:  # noqa: BLE001
                    pass
                raise
            except BaseException as exc:
                final = "failed"
                logger.error("async launch %s crashed: %s", launch_id, exc, exc_info=True)
                try:
                    await asyncio.shield(asyncio.to_thread(
                        _append_sync, {"type": "error", "message": str(exc)}
                    ))
                except BaseException:  # noqa: BLE001
                    pass
                raise
            finally:
                try:
                    if cancelled:
                        _launch_finalize(launch_id, final)
                    else:
                        await asyncio.shield(asyncio.to_thread(_launch_finalize, launch_id, final))
                except BaseException as exc:  # noqa: BLE001
                    if not cancelled:
                        # Shield was interrupted: make sure the row is final.
                        try:
                            _launch_finalize(launch_id, final)
                        except Exception:  # noqa: BLE001
                            pass
                    logger.warning("launch_runs final status write failed %s: %s", launch_id, exc)

        _spawn_task(_run_async(), name=f"launch:{launch_id}")
        return JSONResponse(status_code=202, content={"launch_id": launch_id, "run_id": run_id})

    if request.launch_session_id:
        # SSE path: no launch_runs row exists to store the link on.
        logger.info(
            "launch_sse job=%s launch_session_id=%s (not stored: async launches only)",
            request.job_id, _normalise_launch_session_id(request.launch_session_id),
        )

    async def _gen():
        # Heartbeat pump. A single batch can hold the stream silent for many
        # minutes (Pairbot creation wait alone is up to 600s) — long enough for
        # proxies/browsers to idle the connection out and lose the rest of the
        # launch. Run the work in a task and emit an SSE comment (ignored by
        # the client parser) whenever no real event shows up for 15s.
        queue: asyncio.Queue = asyncio.Queue()
        _sentinel = object()

        async def _pump():
            error: Optional[Exception] = None
            try:
                async for chunk in _work():
                    await queue.put(chunk)
            except Exception as exc:
                error = exc
            finally:
                # put_nowait: unbounded queue, and safe even mid-cancellation.
                queue.put_nowait(error if error is not None else _sentinel)

        pump_task = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is _sentinel:
                    break
                if isinstance(item, Exception):
                    # Preserve pre-pump semantics: a crash in the worker aborts
                    # the stream instead of ending it silently.
                    raise item
                yield item
        finally:
            # Client disconnect cancels this generator — cancel the worker so
            # its finally (fire-once side effects) runs, mirroring the old
            # direct-generator cancellation semantics.
            if not pump_task.done():
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            # Tell nginx not to buffer this stream (it has no SSE-specific
            # config of its own) and caches to keep hands off.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/latest-interview/by-id/{candidate_id}")
async def get_latest_interview(candidate_id: str):
    """
    Look up the latest interview_id for a candidate from the audit table.
    Used by the Assess button to determine which interview to display.
    """
    try:
        conn = common._get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT interview_id, candidate_name, candidate_email, jobdiva_id, status, created_at
            FROM engage_interview_audit
            WHERE candidate_id = %s AND interview_id IS NOT NULL AND interview_id::text != ''
            ORDER BY id DESC
            LIMIT 1
        """, (candidate_id,))

        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            return {
                "success": True,
                "interview_id": row["interview_id"],
                "candidate_name": row.get("candidate_name", ""),
                "candidate_email": row.get("candidate_email", ""),
                "job_id": row.get("job_id", ""),
                "status": row.get("status", ""),
                "created_at": str(row.get("created_at", ""))
            }
        else:
            return {
                "success": False,
                "interview_id": None,
                "message": "No interview found for this candidate"
            }

    except Exception as e:
        logger.error(f"❌ latest-interview lookup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))




# ---------------------------------------------------------------------------
# 5. GET /assess/{interview_id}  (Proxy for PAIR dashboard data)
# ---------------------------------------------------------------------------
@router.get("/assess/{interview_id}")
async def get_assessment_data(interview_id: str):
    """
    Aggregates data from multiple PAIR dashboard endpoints into a single response
    for the Assess modal:
      - Interview info (status, score, progress)
      - Evaluation (per-question Q&A with scores)
      - Transcriptions (conversation messages)
      - Outreach status (communication timeline)
    """
    base_url = EXTERNAL_INTERVIEW_API_URL
    headers = get_pair_auth_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fire all 4 requests in parallel
        interview_task = client.get(f"{base_url}/api/interviews/{interview_id}", headers=headers)
        evaluation_task = client.get(f"{base_url}/api/interviews/{interview_id}/evaluation", headers=headers)
        transcription_task = client.get(f"{base_url}/api/interviews/{interview_id}/transcriptions", headers=headers)
        outreach_task = client.get(f"{base_url}/api/interviews/{interview_id}/outreach-status", headers=headers)

        # Await all
        interview_res = await interview_task
        evaluation_res = await evaluation_task
        transcription_res = await transcription_task
        outreach_res = await outreach_task

    # Parse responses (gracefully handle failures)
    def safe_json(response, label):
        try:
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"⚠️ {label} returned {response.status_code}")
                return None
        except Exception as e:
            logger.warning(f"⚠️ Failed to parse {label}: {e}")
            return None

    interview_data = safe_json(interview_res, "interview")
    evaluation_data = safe_json(evaluation_res, "evaluation")
    transcription_data = safe_json(transcription_res, "transcriptions")
    outreach_data = safe_json(outreach_res, "outreach-status")

    # Handle the case where the response wraps data in a "data" key
    if interview_data and "data" in interview_data:
        interview_data = interview_data["data"]
    if evaluation_data and "data" in evaluation_data:
        evaluation_data = evaluation_data["data"]
    if outreach_data and "data" in outreach_data:
        outreach_data = outreach_data["data"]
    if transcription_data and "data" in transcription_data:
        transcription_data = transcription_data["data"]

    # Prefer Curate's resolved pass/fail status when available so assessment
    # surfaces the effective engagement result instead of Pair Bot's raw
    # completion state.
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT status
                    FROM engage_interview_audit
                    WHERE interview_id = %s
                    ORDER BY updated_at DESC NULLS LAST, id DESC
                    LIMIT 1
                    """,
                    (interview_id,),
                )
                audit_row = cur.fetchone()
                if audit_row and interview_data:
                    effective_status = str(audit_row.get("status") or "").strip()
                    if effective_status:
                        interview_data["status"] = effective_status
    except Exception as e:
        logger.warning(f"⚠️ Failed to overlay effective interview status for {interview_id}: {e}")

    return {
        "success": True,
        "interview_id": interview_id,
        "interview": interview_data,
        "evaluation": evaluation_data,
        "transcriptions": transcription_data if isinstance(transcription_data, list) else (transcription_data or []),
        "outreach": outreach_data
    }


@router.get("/mock-recording.wav")
async def get_mock_recording():
    """Dev-only stand-in audio for the recordings player (MOCK_CALL_RECORDINGS=true)."""
    from fastapi.responses import Response
    from services import call_recordings

    if not call_recordings.mock_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=call_recordings.mock_wav(), media_type="audio/wav")


@router.get("/interviews/{interview_id}/recordings")
async def get_interview_recordings(
    interview_id: str,
    user: UserIdentity = Depends(get_current_user),
):
    """Call recordings (S3) of an interview, with presigned playback URLs.

    Requires a signed-in user and only serves interviews Curate itself
    launched (present in engage_interview_audit), scoped to the same
    recruiter/team access as the rest of the job's /interviews/{id}/...
    routes. Disabled unless ENVIRONMENT=production and S3_RECORDINGS_BUCKET
    is set, matching when pair-bot uploads recordings.
    """
    from services import call_recordings

    if not call_recordings.is_enabled():
        if call_recordings.mock_enabled():
            return {
                "success": True,
                "enabled": True,
                "unavailable": False,
                "recordings": call_recordings.mock_recordings(),
            }
        return {"success": True, "enabled": False, "unavailable": False, "recordings": []}

    try:
        row = await asyncio.to_thread(_recording_audit_row, interview_id)
        if not row:
            raise HTTPException(status_code=404, detail="Interview not found")
        jobdiva_id = (row.get("jobdiva_id") or "").strip() or None

        # Scope to the same job-level recruiter/team access as other
        # /interviews/{id}/... routes. An interview with no jobdiva_id on its
        # audit row can't be scoped to any job, so only admins may read it.
        #
        # _verify_job_access_by_id is deliberately called WITHOUT
        # allow_not_found=True: if the job's monitored_jobs draft has been
        # deleted (e.g. an old or cleaned-up job), a non-admin recruiter gets
        # 403 here rather than access. There is no other source of truth to
        # fall back to — engage_interview_audit does not carry recruiter
        # assignment — and recordings are candidate call audio, so failing
        # closed on an unresolvable ownership check is intentional. An admin
        # can always still retrieve the recording if that happens.
        if jobdiva_id:
            await asyncio.to_thread(_verify_job_access_by_id, jobdiva_id, user)
        elif not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Access denied. You do not have permission to access this interview.",
            )

        created = row.get("created_at")
        fallback_day = created.date() if created else None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.get(
                    f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/detail",
                    headers=get_pair_auth_headers(),
                )
                res.raise_for_status()
                detail = res.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Upstream PAIR outage: degrade the same way an S3 outage does
            # (unavailable: true) instead of surfacing a 500, so the modal
            # shows the "temporarily unavailable" hint rather than an error.
            # A 200 with unavailable:true is invisible to status-code-based
            # monitoring, so also emit a New Relic event an alert can fire on.
            logger.warning(
                "recordings detail lookup failed for interview %s: %s", interview_id, exc
            )
            _log_recordings_upstream_failure(interview_id, exc)
            return {"success": True, "enabled": True, "recordings": [], "unavailable": True}

        if isinstance(detail, dict) and isinstance(detail.get("data"), dict):
            detail = detail["data"]
        all_sessions = [
            {"id": s.get("id"), "started_at": s.get("started_at")}
            for s in (detail.get("sessions") or [])
            if isinstance(s, dict)
        ]
        sessions = all_sessions[:_RECORDINGS_MAX_SESSIONS]
        if len(all_sessions) > len(sessions):
            logger.warning(
                "recordings session list truncated interview=%s total=%d kept=%d",
                interview_id, len(all_sessions), len(sessions),
            )

        result = await asyncio.to_thread(
            call_recordings.find_recordings, sessions, jobdiva_id, fallback_day
        )
        # The raw S3 key is internal detail the client never needs — only an
        # opaque, stable id (used as a React list key) and the presigned url.
        recordings = []
        for rec in result.get("recordings", []):
            rec = dict(rec)
            raw_key = rec.pop("key", "")
            rec["id"] = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
            recordings.append(rec)
        result = {**result, "recordings": recordings}
        return {"success": True, "enabled": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ recordings lookup failed for interview {interview_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load call recordings")

@router.get("/dashboard/pair-outreach")
async def get_pair_outreach(status: Optional[str] = None, phase: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, jd_id: Optional[str] = None, search: Optional[str] = None):
    params = {k: v for k, v in {"status": status, "phase": phase, "date_from": date_from, "date_to": date_to, "jd_id": jd_id, "search": search}.items() if v is not None}
    return await _proxy_get("/api/dashboard/pair-outreach", params=params)

@router.get("/dashboard/pair-outreach/{jd_id}")
async def get_pair_outreach_jd(jd_id: str):
    return await _proxy_get(f"/api/dashboard/pair-outreach/{jd_id}")

@router.get("/dashboard/pair-metrics")
async def get_pair_metrics():
    return await _proxy_get("/api/dashboard/pair-metrics")

@router.get("/dashboard/pair-passed")
async def get_pair_passed(score_threshold: Optional[int] = None):
    params = {"score_threshold": score_threshold} if score_threshold is not None else {}
    return await _proxy_get("/api/dashboard/pair-passed", params=params)

@router.get("/interviews/{interview_id}/outreach-status")
async def get_outreach_status(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/outreach-status")

@router.post("/outreach/start-scheduler")
async def start_scheduler():
    return await _proxy_post("/api/outreach/start-scheduler")

@router.post("/interviews/{interview_id}/trigger-phase2")
async def trigger_phase2(interview_id: str):
    return await _proxy_post(f"/api/interviews/{interview_id}/trigger-phase2")


# ---------------------------------------------------------------------------
# 7. Retrieval of Transcripts API Proxies
# ---------------------------------------------------------------------------
@router.get("/interviews/{interview_id}/transcriptions")
async def get_transcriptions(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/transcriptions")

@router.get("/interviews/{interview_id}/evaluation")
async def get_interview_evaluation(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/evaluation")

@router.get("/interviews/{interview_id}/score-summary")
async def get_interview_score_summary(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/score-summary")

@router.get("/interviews/{interview_id}/activity-logs")
async def get_activity_logs(interview_id: str):
    return await _proxy_get(f"/api/interviews/{interview_id}/activity-logs")

@router.get("/interviews/{interview_id}/transcriptions/download")
async def download_transcriptions(interview_id: str):
    try:
        client = httpx.AsyncClient(timeout=30.0)
        req = client.build_request("GET", f"{EXTERNAL_INTERVIEW_API_URL}/api/interviews/{interview_id}/transcriptions/download")
        res = await client.send(req, stream=True)
        return StreamingResponse(res.aiter_bytes(), headers=res.headers)
    except Exception as e:
        logger.error(f"❌ download_transcriptions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engage/re-provision")
async def re_provision_candidates(request: ReProvisionRequest):
    """
    Re-runs JobDiva provisioning for all launched candidates for a job.

    By default only candidates that are missing a jobdiva_candidate_id in
    their data blob are processed (the ones that were silently dropped by the
    old fire-and-forget approach).  Pass force_all=true to re-run for every
    launched candidate (useful to fix 'Unknown Unknown' names).

    Returns counts of { success, skipped, failed, total }.
    """
    job_id = (request.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    try:
        conn = common._get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Resolve both forms of the job ID so we can match audit records
        numeric_job_id, ref_job_id = await provisioning._resolve_provisioning_job_ids(job_id)
        j1 = job_id
        j2 = numeric_job_id or job_id
        j3 = ref_job_id or job_id

        if request.force_all:
            # All launched candidates for this job
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
            """, (j1, j2, j3, j1, j2, j3))
        else:
            # Only those missing a jobdiva_candidate_id
            cur.execute("""
                SELECT DISTINCT sc.candidate_id
                FROM sourced_candidates sc
                JOIN engage_interview_audit eia ON eia.candidate_id = sc.candidate_id
                WHERE eia.jobdiva_id IN (%s, %s, %s)
                  AND sc.jobdiva_id IN (%s, %s, %s)
                  AND (
                    sc.data->>'jobdiva_candidate_id' IS NULL
                    OR sc.data->>'jobdiva_candidate_id' = ''
                  )
            """, (j1, j2, j3, j1, j2, j3))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        candidate_ids = [str(r["candidate_id"]) for r in rows]
        total = len(candidate_ids)
        logger.info(
            f"🔄 [Re-Provision] job={job_id} force_all={request.force_all} "
            f"candidates_to_process={total}"
        )

        if not candidate_ids:
            return {
                "success": True,
                "message": "No candidates require provisioning.",
                "total": 0,
                "success_count": 0,
                "skipped": 0,
                "failed": 0,
            }

        # Run batch provisioning (awaited so the HTTP response carries the result)
        results = await provisioning._provision_batch_to_jobdiva(
            candidate_ids,
            job_id,
            label="Re-Provision",
        )

        return {
            "success": True,
            "job_id": job_id,
            "total": total,
            "success_count": results.get("success", 0),
            "skipped": results.get("skipped", 0),
            "failed": results.get("failed", 0),
            "duplicate_suspected": results.get("duplicate_suspected", 0),
            # Included in `failed`: rows too empty to create a JobDiva profile from.
            "blank_profile_refused": results.get("blank_profile_refused", 0),
            "message": (
                f"Re-provisioning complete. "
                f"{results.get('success', 0)} created, "
                f"{results.get('skipped', 0)} already existed, "
                f"{results.get('failed', 0)} failed."
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [Re-Provision] error for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/engage/jobdiva-profile-audit")
async def jobdiva_profile_audit(
    job_id: Optional[str] = None,
    limit: int = 200,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: JobDiva-sourced rows whose stored jobdiva_candidate_id is not their own id.

    Each row is a suspected duplicate JobDiva profile: `real_profile_id` is the
    profile the recruiter's search returned, `duplicate_profile_id` the one the
    provisioner persisted after JobDiva minted or linked something else. Merge
    the pair in JobDiva, then call the repair endpoint (or re-launch) to re-stamp.
    """
    _require_admin(user)
    limit = max(1, min(int(limit or 200), 2000))
    job_sql, job_params = await _jobdiva_profile_audit_job_filter(job_id)
    conn = common._get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM sourced_candidates sc
            WHERE {_JOBDIVA_PROFILE_AUDIT_WHERE}{job_sql}
            """,
            job_params,
        )
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"""
            SELECT sc.jobdiva_id AS job_id,
                   sc.candidate_id AS real_profile_id,
                   sc.data->>'jobdiva_candidate_id' AS duplicate_profile_id,
                   sc.source, sc.name, sc.updated_at
            FROM sourced_candidates sc
            WHERE {_JOBDIVA_PROFILE_AUDIT_WHERE}{job_sql}
            ORDER BY sc.updated_at DESC NULLS LAST
            LIMIT %s
            """,
            job_params + [limit],
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()
    return {"success": True, "job_id": job_id or None, "total": total, "returned": len(rows), "rows": rows}


@router.post("/engage/jobdiva-profile-audit/repair")
async def jobdiva_profile_audit_repair(
    request: JobDivaProfileAuditRepairRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: re-stamp every JobDiva-sourced row's jobdiva_candidate_id with its own id.

    Idempotent; touches only rows where the stored id is missing or differs.
    Merging the duplicate profiles inside JobDiva stays a recruiter action —
    run the audit first to get the list.
    """
    _require_admin(user)
    job_sql, job_params = await _jobdiva_profile_audit_job_filter(request.job_id)
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE sourced_candidates sc
            SET data = COALESCE(sc.data, '{{}}'::jsonb)
                       || jsonb_build_object('jobdiva_candidate_id', sc.candidate_id)
            WHERE lower(sc.source) LIKE 'jobdiva%%'
              AND sc.candidate_id ~ '^[0-9]+$'
              AND sc.data->>'jobdiva_candidate_id' IS DISTINCT FROM sc.candidate_id{job_sql}
            """,
            job_params,
        )
        repaired = cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    logger.info(
        "jobdiva profile audit repair: job=%s rows=%s by=%s",
        request.job_id or "ALL", repaired, user.email,
    )
    return {"success": True, "job_id": request.job_id or None, "repaired": repaired}


@router.post("/engage/jobdiva-blank-profile-backfill")
async def jobdiva_blank_profile_backfill(
    request: JobDivaBlankProfileBackfillRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: repair JobDiva profiles Launch PAIR created blank before 2026-09-25.

    Per profile (services/jobdiva_profile_backfill.py): upload the résumé PAIR
    can build when every résumé JobDiva holds is empty, then fill only blank
    fields -- name, email (or the alternate slot when another JobDiva record
    holds it: a likely duplicate, reported as email_held_by_another_profile),
    phones, city / state / zip / country, LinkedIn and other social links.
    Nothing entered in JobDiva is overwritten; JobDiva-sourced people are never
    touched. Dry run by default. With dry_run=false each settled profile is
    stamped (jobdiva_backfill_checked_at), so repeated calls walk the list.
    """
    _require_admin(user)
    from services.jobdiva_profile_backfill import (
        BACKFILL_CANDIDATES_SQL,
        BACKFILL_STAMP_SQL,
        backfill_blank_profile,
        settles,
    )

    limit = max(1, min(int(request.limit or 10), 50))
    job_ids = await _audit_job_ids(request.job_id) if request.job_id else []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BACKFILL_CANDIDATES_SQL, {
            "include_checked": bool(request.jobdiva_ids),
            "jobdiva_ids": [str(i).strip() for i in request.jobdiva_ids or [] if str(i).strip()] or None,
            "job_ids": job_ids or None,
            "limit": limit,
        })
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()

    reports: List[Dict[str, Any]] = []
    for row in rows:
        report = await backfill_blank_profile(row, apply=not request.dry_run)
        reports.append(report)
        if settles(report):
            stamp = {"jobdiva_backfill_checked_at": common._utc_now_iso(), "jobdiva_backfill_status": report["status"]}
            conn = common._get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute(BACKFILL_STAMP_SQL, {
                    "delta": json.dumps(stamp), "jobdiva_id": report["jobdiva_candidate_id"],
                })
                conn.commit()
                cur.close()
            finally:
                conn.close()

    counts: Dict[str, int] = {}
    for report in reports:
        counts[report["status"]] = counts.get(report["status"], 0) + 1
    logger.info(
        "jobdiva blank-profile backfill: dry_run=%s job=%s processed=%s %s by=%s",
        request.dry_run, request.job_id or "ALL", len(reports), counts, user.email,
    )
    return {
        "success": True,
        "dry_run": request.dry_run,
        "processed": len(reports),
        "statuses": counts,
        "likely_duplicates": [r["jobdiva_candidate_id"] for r in reports if r.get("email_held_by_another_profile")],
        "reports": reports,
    }


@router.get("/engage/applicant-origin-audit")
async def applicant_origin_audit(
    job_id: Optional[str] = None,
    limit: int = 200,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: JobDiva-labelled rows that are twins of a provisioned origin row.

    A pair is `origin` (non-JobDiva source, e.g. LinkedIn-Exa, whose stored
    jobdiva_candidate_id is the profile Launch PAIR created or attached) and
    `twin` (a JobDiva-* row whose candidate_id IS that profile id) on the same
    job. The twin is the sync's or a re-launch's re-import of the same person;
    the origin row is the one that should survive, keeping its source label.
    One origin can have several twins (an Applicants row from the sync and a
    TalentSearch/JobAgent row from a re-launch); each pair is one row here.
    """
    _require_admin(user)
    limit = max(1, min(int(limit or 200), 2000))
    job_ids = await _audit_job_ids(job_id)
    job_sql = _audit_job_filter_sql(job_ids, "t")
    job_params: list = [job_ids] if job_ids else []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT COUNT(*) AS n
            {_APPLICANT_TWIN_JOIN}{job_sql}
            """,
            job_params,
        )
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"""
            SELECT t.jobdiva_id AS job_id,
                   o.candidate_id AS origin_candidate_id,
                   o.source AS origin_source,
                   o.data->>'jobdiva_application_origin' AS origin_application_origin,
                   o.data->>'engage_status' AS origin_engage_status,
                   t.candidate_id AS jobdiva_candidate_id,
                   t.source AS twin_source,
                   t.data->>'engage_status' AS twin_engage_status,
                   COALESCE(NULLIF(o.name, ''), t.name) AS name,
                   t.created_at AS twin_created_at
            {_APPLICANT_TWIN_JOIN}{job_sql}
            ORDER BY t.created_at DESC NULLS LAST
            LIMIT %s
            """,
            job_params + [limit],
        )
        rows = [dict(r) for r in (cur.fetchall() or [])]
        cur.close()
    finally:
        conn.close()
    return {"success": True, "job_id": job_id or None, "total": total, "returned": len(rows), "rows": rows}


@router.post("/engage/applicant-origin-audit/repair")
async def applicant_origin_audit_repair(
    request: ApplicantOriginRepairRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Admin: fold every JobDiva-labelled twin into its origin row, then delete the twins.

    Per origin row: engage bookkeeping the origin lacks is taken from its twins
    -- ALL of them, key by key, the most recently updated twin winning a key
    that several carry -- the merge is noted in data.jobdiva_twin_merged_at /
    jobdiva_twin_sources / jobdiva_twin_count, and the twins are deleted. The
    origin keeps its `source` -- that is the whole point. `dry_run` (default)
    only counts. Idempotent: a repaired job has no pairs left.

    The fold is a correlated subquery over the origin, not an ``UPDATE ... FROM``
    join: with a join, an origin with two twins is matched twice and Postgres
    applies SET from one arbitrary match, so the other twin's engage state would
    be dropped -- and then deleted with the twin.
    """
    _require_admin(user)
    job_ids = await _audit_job_ids(request.job_id)
    job_params: list = [job_ids] if job_ids else []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        if request.dry_run:
            cur.execute(
                f"""
                SELECT COUNT(*)
                {_APPLICANT_TWIN_JOIN}{_audit_job_filter_sql(job_ids, "t")}
                """,
                job_params,
            )
            row = cur.fetchone()
            would = int((row[0] if isinstance(row, (tuple, list)) else (row or {}).get("count")) or 0)
            cur.close()
            return {"success": True, "job_id": request.job_id or None, "dry_run": True, "would_merge": would}

        fold_keys_sql = ", ".join("%s" for _ in _APPLICANT_TWIN_FOLD_KEYS)
        cur.execute(
            f"""
            UPDATE sourced_candidates AS o
            SET data = COALESCE(o.data, '{{}}'::jsonb)
                       || COALESCE((
                            -- Every engage key the origin lacks, from whichever twin
                            -- carries it; when several do, the most recently updated
                            -- twin's value wins (deterministic, no state dropped).
                            SELECT jsonb_object_agg(folded.k, folded.v)
                            FROM (
                                SELECT DISTINCT ON (e.k) e.k, e.v
                                FROM sourced_candidates AS t
                                CROSS JOIN LATERAL jsonb_each(COALESCE(t.data, '{{}}'::jsonb)) AS e(k, v)
                                WHERE {_TWIN_OF_ORIGIN}
                                  AND e.k IN ({fold_keys_sql})
                                  AND (o.data -> e.k) IS NULL
                                ORDER BY e.k, t.updated_at DESC NULLS LAST, t.id DESC
                            ) AS folded
                          ), '{{}}'::jsonb)
                       || jsonb_build_object(
                            'jobdiva_twin_merged_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                            'jobdiva_twin_sources', (
                                SELECT COALESCE(jsonb_agg(DISTINCT t.source), '[]'::jsonb)
                                FROM sourced_candidates AS t
                                WHERE {_TWIN_OF_ORIGIN}
                            ),
                            'jobdiva_twin_count', (
                                SELECT COUNT(*)
                                FROM sourced_candidates AS t
                                WHERE {_TWIN_OF_ORIGIN}
                            )
                          ),
                updated_at = CURRENT_TIMESTAMP
            WHERE lower(o.source) NOT LIKE 'jobdiva%%'
              AND EXISTS (
                  SELECT 1
                  FROM sourced_candidates AS t
                  WHERE {_TWIN_OF_ORIGIN}
              ){_audit_job_filter_sql(job_ids, "o")}
            """,
            list(_APPLICANT_TWIN_FOLD_KEYS) + job_params,
        )
        origins_updated = cur.rowcount
        cur.execute(
            f"""
            DELETE FROM sourced_candidates AS t
            USING sourced_candidates AS o
            WHERE {_TWIN_OF_ORIGIN}
              AND lower(o.source) NOT LIKE 'jobdiva%%'{_audit_job_filter_sql(job_ids, "t")}
            """,
            job_params,
        )
        twins_deleted = cur.rowcount
        conn.commit()
        cur.close()
    finally:
        conn.close()
    logger.info(
        "applicant origin audit repair: job=%s origins_updated=%s twins_deleted=%s by=%s",
        request.job_id or "ALL", origins_updated, twins_deleted, user.email,
    )
    return {
        "success": True, "job_id": request.job_id or None, "dry_run": False,
        "origins_updated": origins_updated, "twins_deleted": twins_deleted,
    }
