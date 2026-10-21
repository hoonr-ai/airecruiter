"""Engagement package: launch."""

import asyncio
import hashlib
import uuid
from fastapi import HTTPException, Request
from typing import Optional, List, Any, Dict, Tuple, AsyncIterator, Callable
import psycopg2.extras
import json
from datetime import datetime, timezone
from routers._helpers import (
    get_db_connection,
    _verify_job_access_by_id,
)
from core.tasks import spawn as _spawn_task
from core.queue import enqueue
from core.queue_tasks import SEND_PAIR_LAUNCH_EMAIL, SYNC_JOB_APPLICANTS
from core.pipeline_trace import new_run as _trace_new_run, span as _trace_span
from services.auto_assign_service import auto_assign_service  # noqa: F401  (core.queue_tasks calls it through this module so tests can patch it)
from core.auth import get_current_user

from .common import logger
from .models import GeneratePayloadRequest, LaunchRequest
from .payloads import _merge_employer_signals, _pair_phone_digits, _sanitize_pair_candidate_email, is_candidate_excluded_from_pair
from . import bulk_send
from . import common
from . import models
from . import pairbot
from . import payloads


# Backend batch size for the launch orchestrator's per-batch Pairbot calls.
# Decoupled from the FE /candidates/save batch (which IS nginx body-limit
# bound because it carries resume_text): the /engage/launch request carries
# only candidate_ids, so this is tuned for Pairbot throughput + failure
# granularity, not payload size.
_LAUNCH_PAIRBOT_BATCH_SIZE = 75
_LAUNCH_BATCH_DELAY_SECONDS = 0.35


def _launch_flag(name: str, default: Any) -> Any:
    """Read a launch flag from core.config at call time (monkeypatchable)."""
    try:
        import core.config as _cfg
        return getattr(_cfg, name, default)
    except Exception:  # noqa: BLE001
        return default


# --- Fix 1: employer gate once per launch ----------------------------------

async def _precompute_launch_signals(
    job_id: str, candidate_ids: List[str]
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Resolve employer signals + resume freshness ONCE for the whole launch.

    Returns {candidate_id: signals} with a key for EVERY row it loaded (empty
    dict when nothing was resolved), so per-batch gates know which ids are
    covered. Returns None when the rows could not be loaded — batches then
    fall back to the per-batch pass. A resolution failure fails open exactly
    like the per-batch gate: covered ids gate on stored signals.
    """
    def _load() -> List[Dict[str, Any]]:
        conn = common._get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT job_id, jobdiva_id FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
                LIMIT 1
                """,
                (job_id, job_id),
            )
            jrow = cur.fetchone()
            jid = str((jrow[0] if jrow else None) or job_id)
            jdid = str((jrow[1] if jrow else None) or "")
            cur.execute(
                """
                SELECT candidate_id, data, name, headline, resume_text, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                """,
                (list(candidate_ids), jid, jdid),
            )
            rows = cur.fetchall() or []
            signals = payloads._fetch_stored_employer_signals(conn, [str(r[0]) for r in rows])
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                cdata = r[1] or {}
                if isinstance(cdata, str):
                    try:
                        cdata = json.loads(cdata)
                    except Exception:
                        cdata = {}
                cdata = _merge_employer_signals(
                    cdata, headline=str(r[3] or ""), signals=signals.get(str(r[0]))
                )
                cdata.setdefault("candidate_id", str(r[0]))
                if r[4] and not cdata.get("resume_text"):
                    cdata["resume_text"] = str(r[4])
                if r[5] and not cdata.get("source"):
                    cdata["source"] = str(r[5])
                out.setdefault(str(r[0]), cdata)
            cur.close()
            return list(out.values())
        finally:
            conn.close()

    try:
        cands = await asyncio.to_thread(_load)
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch gate preload failed for job %s (per-batch fallback): %s", job_id, exc)
        return None

    result: Dict[str, Dict[str, Any]] = {str(c["candidate_id"]): {} for c in cands}
    if not cands:
        return result
    try:
        from services.employer_resolution import resolve_employer_signals
        try:
            import core.sourcing_config as _sc
            conc = int(getattr(_sc, "EMPLOYER_RESOLUTION_CONCURRENCY", 6) or 6)
        except Exception:  # noqa: BLE001
            conc = 6
        timeout = min(600.0, 30.0 + 2.0 * len(cands) / max(1, conc))
        resolved = await asyncio.wait_for(resolve_employer_signals(cands), timeout=timeout) or {}
        for cid, sig in resolved.items():
            if cid in result and sig:
                result[cid] = dict(sig)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "employer resolution failed for job %s (gating on stored signals): %s", job_id, exc
        )
    try:
        from services.employer_resolution import stamp_resume_freshness
        merged = [
            _merge_employer_signals(c, signals=result.get(str(c["candidate_id"])) or None)
            for c in cands
        ]
        await asyncio.wait_for(stamp_resume_freshness(merged), timeout=20.0)
        for c in merged:
            ts = c.get("resume_updated_at")
            if ts:
                result[str(c["candidate_id"])]["resume_updated_at"] = ts
    except Exception as exc:  # noqa: BLE001
        logger.warning("resume-freshness check failed for job %s: %s", job_id, exc)
    return result


# --- Fix 3: launch_runs / launch_batches -------------------------------------

_LAUNCH_TABLES_READY = {"done": False}

# Statements mirror migrations/20261009_pair_launch_pipeline.sql. Run once at
# startup (main.py lifespan -> init_launch_tables), never per request.
_LAUNCH_DDL = (
    """
    CREATE TABLE IF NOT EXISTS launch_runs (
        id uuid PRIMARY KEY,
        job_id text NOT NULL,
        requested_by text,
        status text NOT NULL,
        events jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz DEFAULT now(),
        finished_at timestamptz,
        last_event_at timestamptz DEFAULT now()
    )
    """,
    "ALTER TABLE launch_runs ADD COLUMN IF NOT EXISTS last_event_at timestamptz DEFAULT now()",
    # Server launch sessions: the session a run belongs to. Runs a session
    # creates use status 'session', which the stale sweep and _launch_finalize
    # (both `WHERE status = 'running'`) never touch.
    "ALTER TABLE launch_runs ADD COLUMN IF NOT EXISTS launch_session_id uuid NULL",
    """
    CREATE INDEX IF NOT EXISTS ix_launch_runs_running
    ON launch_runs (last_event_at) WHERE status = 'running'
    """,
    """
    CREATE TABLE IF NOT EXISTS launch_events (
        launch_id uuid NOT NULL REFERENCES launch_runs(id) ON DELETE CASCADE,
        seq int NOT NULL,
        payload jsonb NOT NULL,
        created_at timestamptz DEFAULT now(),
        PRIMARY KEY (launch_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS launch_batches (
        id uuid PRIMARY KEY,
        launch_id uuid REFERENCES launch_runs(id),
        idx int,
        candidate_ids text[],
        pairbot_bulk_id text,
        status text,
        error text,
        creation_event jsonb,
        idempotency_key text UNIQUE,
        updated_at timestamptz DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_launch_batches_bulk ON launch_batches (pairbot_bulk_id)",
    "CREATE INDEX IF NOT EXISTS ix_launch_batches_launch ON launch_batches (launch_id)",
)


def _ensure_launch_tables() -> None:
    """CREATE IF NOT EXISTS for the async-launch tables."""
    if _LAUNCH_TABLES_READY["done"]:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for stmt in _LAUNCH_DDL:
                cur.execute(stmt)
            conn.commit()
    _LAUNCH_TABLES_READY["done"] = True


async def init_launch_tables() -> None:
    """Startup hook (main.py lifespan). Fail-soft: the caller logs errors."""
    await asyncio.to_thread(_ensure_launch_tables)


def _launch_batch_idempotency_key(launch_id: str, idx: int, candidate_ids: List[str]) -> str:
    raw = f"{launch_id}:{idx}:{','.join(sorted(str(c) for c in candidate_ids))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _launch_db(sql: str, params: tuple, fetch: bool = False) -> Any:
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        cur.close()
        return rows
    finally:
        conn.close()


def _launch_batch_set_bulk(idem_key: str, bulk_id: str, *, launch_id: Optional[str] = None,
                           idx: Optional[int] = None, candidate_ids: Optional[List[str]] = None) -> None:
    """Record the Pairbot bulk_id for an async-launch batch (fail-open)."""
    try:
        _launch_db(
            """
            INSERT INTO launch_batches (id, launch_id, idx, candidate_ids, pairbot_bulk_id,
                                        status, idempotency_key, updated_at)
            VALUES (%s, %s, %s, %s, %s, 'creating', %s, now())
            ON CONFLICT (idempotency_key) DO UPDATE
              SET pairbot_bulk_id = EXCLUDED.pairbot_bulk_id,
                  status = 'creating', updated_at = now()
            """,
            (str(uuid.uuid4()), launch_id, idx, list(candidate_ids or []), bulk_id, idem_key),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_batch_set_bulk failed key=%s: %s", idem_key, exc)


async def _poll_launch_batch_creation(bulk_id: str, *, timeout_seconds: float = 600.0) -> Dict[str, Any]:
    """Jittered DB poll for the creation status the Pairbot webhook writes."""
    import random
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        await asyncio.sleep(2.0 + random.random() * 2.0)
        try:
            rows = await asyncio.to_thread(
                _launch_db,
                "SELECT status, creation_event, error FROM launch_batches "
                "WHERE pairbot_bulk_id = %s ORDER BY updated_at DESC LIMIT 1",
                (bulk_id,),
                True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("launch batch poll failed bulk_id=%s: %s", bulk_id, exc)
            continue
        if not rows:
            continue
        status, event, error = rows[0]
        if status == "creation_completed":
            if isinstance(event, str):
                try:
                    event = json.loads(event)
                except Exception:
                    event = None
            return event if isinstance(event, dict) else {"status": status}
        if status == "creation_failed":
            raise RuntimeError(error or f"Pairbot creation_failed for bulk_id={bulk_id}")
    raise TimeoutError(f"Timed out waiting for Pairbot creation webhook bulk_id={bulk_id}")


async def _wait_for_creation_webhook_or_sse(bulk_id: str, *, timeout_seconds: float = 600.0) -> Dict[str, Any]:
    """Race the webhook-backed poll against the legacy SSE wait. The first
    success wins; if one fails, the other gets the rest of the budget."""
    sse = asyncio.ensure_future(pairbot._wait_for_pairbot_creation(bulk_id, timeout_seconds=timeout_seconds))
    hook = asyncio.ensure_future(_poll_launch_batch_creation(bulk_id, timeout_seconds=timeout_seconds))
    pending = {sse, hook}
    first_error: Optional[BaseException] = None
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    return t.result()
                if first_error is None:
                    first_error = t.exception()
        assert first_error is not None
        raise first_error
    finally:
        for t in (sse, hook):
            if not t.done():
                t.cancel()
        await asyncio.gather(sse, hook, return_exceptions=True)


_LAUNCH_TERMINAL_STATUSES = ("completed", "failed", "aborted")


def _launch_stale_minutes() -> int:
    try:
        return max(1, int(_launch_flag("LAUNCH_STALE_MINUTES", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _launch_stream_max_seconds() -> float:
    try:
        return float(_launch_flag("LAUNCH_STREAM_MAX_SECONDS", 45 * 60) or 45 * 60)
    except (TypeError, ValueError):
        return 45 * 60.0


def _launch_append_event(launch_id: str, seq: int, evt: Dict[str, Any]) -> None:
    """Append one event row and bump launch_runs.last_event_at (one statement)."""
    _launch_db(
        """
        WITH ins AS (
            INSERT INTO launch_events (launch_id, seq, payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (launch_id, seq) DO NOTHING
            RETURNING 1
        )
        UPDATE launch_runs SET last_event_at = now() WHERE id = %s
        """,
        (launch_id, seq, json.dumps(evt), launch_id),
    )


def _launch_finalize(launch_id: str, status: str) -> None:
    """Write the terminal status, only if the run is still 'running'."""
    _launch_db(
        "UPDATE launch_runs SET status = %s, finished_at = now() "
        "WHERE id = %s AND status = 'running'",
        (status, launch_id),
    )


def _sweep_stale_launch_runs(launch_id: Optional[str] = None) -> int:
    """Mark 'running' launches with no event for LAUNCH_STALE_MINUTES as
    'failed' (worker died / was killed without writing a final status).
    Scoped to one launch when `launch_id` is given. Returns rows swept."""
    sql = (
        "UPDATE launch_runs SET status = 'failed', finished_at = now() "
        "WHERE status = 'running' "
        "AND COALESCE(last_event_at, created_at) < now() - make_interval(mins => %s)"
    )
    params: tuple = (_launch_stale_minutes(),)
    if launch_id:
        sql += " AND id = %s"
        params = params + (launch_id,)
    rows = _launch_db(sql + " RETURNING id", params, True)
    n = len(rows or [])
    if n:
        logger.warning("launch_runs stale sweep marked %d run(s) failed", n)
    return n


# launch_runs.status of a run a server launch session drives. The session owns
# its lifecycle; the stale sweep and _launch_finalize only ever touch 'running'.
LAUNCH_RUN_SESSION_STATUS = "session"


def _normalise_launch_session_id(value: Any) -> Optional[str]:
    """A launch session id as a canonical uuid string, or None (never raises)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        logger.warning("ignoring malformed launch_session_id=%r", raw[:64])
        return None


def register_session_launch_run(
    launch_id: str,
    job_id: str,
    *,
    requested_by: Optional[str] = None,
    launch_session_id: Optional[str] = None,
) -> None:
    """Create the launch_runs row a session-driven ``run_launch`` uses as its
    ``launch_id`` (launch_batches has a foreign key to it). Status 'session':
    never swept, never finalized by the legacy code. Idempotent on id. Sync."""
    _launch_db(
        "INSERT INTO launch_runs (id, job_id, requested_by, status, last_event_at, launch_session_id) "
        "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (id) DO NOTHING",
        (
            str(launch_id), str(job_id),
            (requested_by or "").strip().lower() or None,
            LAUNCH_RUN_SESSION_STATUS,
            _normalise_launch_session_id(launch_session_id),
        ),
    )


async def _authorize_launch_stream(request: Request, launch_id: str) -> None:
    """Requester, admins, or users with access to the launch's job only."""
    user = await asyncio.to_thread(get_current_user, request, None)
    try:
        rows = await asyncio.to_thread(
            _launch_db,
            "SELECT job_id, requested_by FROM launch_runs WHERE id = %s",
            (launch_id,),
            True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch auth lookup failed launch_id=%s: %s", launch_id, exc)
        raise HTTPException(status_code=503, detail="Launch lookup failed")
    if not rows:
        raise HTTPException(status_code=404, detail="Launch not found")
    job_id, requested_by = rows[0]
    if user.is_admin:
        return
    if requested_by and str(requested_by).strip().lower() == (user.email or "").strip().lower():
        return
    await asyncio.to_thread(_verify_job_access_by_id, str(job_id), user)


async def _launch_progress_stream(launch_id: str):
    """SSE replay of an async launch's events (launch_events, by seq).

    Each connection replays from seq 0 (the frontend skips already-seen
    events by count) and then reads only rows with seq > last seen. Ends on a
    terminal status, a stale run (swept to 'failed'), or after
    LAUNCH_STREAM_MAX_SECONDS."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _launch_stream_max_seconds()
    last_seq = -1
    last_type: Optional[str] = None
    idle = 0
    while True:
        if loop.time() >= deadline:
            yield f"data: {json.dumps({'type': 'error', 'message': 'launch progress stream timed out; refresh to check status'})}\n\n"
            return
        try:
            # Status first: events are written before the final status, so a
            # terminal status read here means every event is already visible.
            rows = await asyncio.to_thread(
                _launch_db,
                "SELECT status, COALESCE(last_event_at, created_at) "
                "< now() - make_interval(mins => %s) FROM launch_runs WHERE id = %s",
                (_launch_stale_minutes(), launch_id),
                True,
            )
            events = []
            if rows:
                events = await asyncio.to_thread(
                    _launch_db,
                    "SELECT seq, payload FROM launch_events "
                    "WHERE launch_id = %s AND seq > %s ORDER BY seq",
                    (launch_id, last_seq),
                    True,
                ) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch progress read failed launch_id=%s: %s", launch_id, exc)
            rows = None
            events = []
        if not rows:
            idle += 1
            if idle > 30:
                yield f"data: {json.dumps({'type': 'error', 'message': 'launch not found'})}\n\n"
                return
            await asyncio.sleep(1.0)
            continue
        status, stale = rows[0]
        for seq, ev in events:
            if isinstance(ev, str):
                ev = json.loads(ev)
            last_seq = seq
            last_type = ev.get("type") if isinstance(ev, dict) else None
            yield f"data: {json.dumps(ev)}\n\n"
        if status == "running" and stale:
            try:
                if await asyncio.to_thread(_sweep_stale_launch_runs, launch_id):
                    status = "failed"
            except Exception as exc:  # noqa: BLE001
                logger.warning("launch stale sweep failed launch_id=%s: %s", launch_id, exc)
        if status in _LAUNCH_TERMINAL_STATUSES:
            if status == "failed" and last_type not in ("error", "done"):
                yield f"data: {json.dumps({'type': 'error', 'message': 'launch did not complete'})}\n\n"
            return
        yield ": keepalive\n\n"
        await asyncio.sleep(1.5)


# --- Launch side effects -----------------------------------------------------

def _decide_launch_effects(job_id: str, **kwargs: Any) -> Tuple[bool, bool, bool]:
    """services.launch_effects_ledger.decide (a seam the tests stub)."""
    from services import launch_effects_ledger

    return launch_effects_ledger.decide(job_id, **kwargs)


def fire_launch_side_effects(
    job_id: str,
    *,
    sent_count: int,
    notify_recruiters: bool,
    send_job_posting_email: bool,
    is_initial_launch: bool,
    app_base_url: str,
    launch_started_at: Optional[datetime] = None,
) -> bool:
    """The once-per-launch side effects, spawned as tracked background tasks:
    the applicant sync (initial launch only) and the recruiter launch email
    (initial launch or ``notify_recruiters``), whose job-posting part goes out
    only when ``send_job_posting_email`` — the caller decides that (today:
    requested AND the launch ran clean: not aborted, no failed batch).

    The FIRST-launch effects (applicant sync, job-posting email) are then
    checked against the job's fire-once ledger (services/launch_effects_ledger):
    they fire only for the launch that produced the job's first interview,
    once per job version, whatever the browser's flags say. The recruiter
    email still goes out for every launch that sent someone, worded as a
    relaunch when the job's first interview predates ``launch_started_at``.

    Nothing fires when ``sent_count <= 0``. The caller owns "once" per launch:
    /engage/launch keeps an in-memory flag; a launch session claims a DB guard
    first. Returns True only when every requested effect was spawned (False on
    an error, which is logged, never raised). Must be called with a running
    event loop."""
    if sent_count <= 0:
        return False
    if not (is_initial_launch or notify_recruiters):
        return False
    initial, job_posting, relaunch = _decide_launch_effects(
        job_id,
        is_initial_launch=bool(is_initial_launch),
        send_job_posting_email=bool(send_job_posting_email),
        launch_started_at=launch_started_at,
    )
    fired = False
    try:
        if initial:
            logger.info("launch: initial launch for job %s -> applicant sync", job_id)
            try:
                _spawn_task(enqueue(SYNC_JOB_APPLICANTS, str(job_id)), name=f"enqueue:{SYNC_JOB_APPLICANTS}")
            except Exception:
                from services import launch_effects_ledger

                launch_effects_ledger.release(job_id, launch_effects_ledger.INITIAL)
                raise
            fired = True
        _spawn_task(
            enqueue(
                SEND_PAIR_LAUNCH_EMAIL,
                str(job_id), int(sent_count), bool(job_posting), app_base_url or "", bool(relaunch),
            ),
            name=f"enqueue:{SEND_PAIR_LAUNCH_EMAIL}",
        )
        fired = True
    except Exception as _se_err:
        logger.warning("launch: side-effect firing failed: %s", _se_err)
        return False
    return fired


# --- Fix 2: one launch batch -----------------------------------------------

async def _run_one_batch(
    idx: int,
    batch: List[str],
    *,
    request: "LaunchRequest",
    precomputed_signals: Optional[Dict[str, Dict[str, Any]]],
    abort_flag: Dict[str, bool],
    launch_id: Optional[str] = None,
    claimed_session_id: Optional[str] = None,
    provision_inline: bool = True,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one launch batch and RETURN its outcome; never mutates shared
    launch state (only the consumer aggregates). Keys:

      status            completed | failed | aborted (409) | not_run (after abort)
      event             SSE event dict for this batch (None for aborted/not_run)
      sent, no_interview, already_sent, excluded_count, employer_unverified_count
      skipped, excluded_records, employer_unverified, failed_ids
      candidate_ids     ids this batch tried to send (after DNC + contact gate)
      sent_ids, no_interview_ids, bulk_id   per-candidate outcome (completed)
    """
    res_out: Dict[str, Any] = {
        "idx": idx, "status": "completed", "event": None,
        "sent": 0, "no_interview": 0, "already_sent": 0,
        "excluded_count": 0, "employer_unverified_count": 0,
        "skipped": [], "excluded_records": [], "employer_unverified": [], "failed_ids": [],
        "candidate_ids": list(batch), "sent_ids": [], "no_interview_ids": [], "bulk_id": None,
    }
    # Ids actually sent to Pairbot for this batch: batch minus DNC-blocked
    # minus contact-gate exclusions. Failure paths must report THESE ids —
    # reporting the original batch would re-record rows already surfaced as
    # exclusions, putting permanently uncontactable candidates into the retry
    # set forever.
    sendable_ids: List[str] = batch

    def _failed(error: str) -> Dict[str, Any]:
        res_out["status"] = "failed"
        res_out["failed_ids"] = list(sendable_ids)
        res_out["event"] = {
            "type": "batch", "index": idx, "status": "failed",
            "error": error, "candidate_ids": list(sendable_ids),
        }
        return res_out

    try:
        async with _trace_span("payload_generation", batch_idx=idx, n=len(batch)):
            gp = await payloads._generate_payload_for(
                GeneratePayloadRequest(candidate_ids=batch, job_id=request.job_id)
            )
        payload_str = gp.get("payload") if isinstance(gp, dict) else None
        if not payload_str:
            raise RuntimeError("generate-payload returned no payload")

        # Keep real_candidate_ids aligned with the payload's resumes:
        # generate drops DNC-stopped candidates from resumes, so drop them
        # from real_candidate_ids too — otherwise the positional matching in
        # _send_bulk_interview_core misaligns.
        dnc_blocked = set(gp.get("dnc_blocked_ids") or []) if isinstance(gp, dict) else set()
        real_ids = [c for c in batch if c not in dnc_blocked] if dnc_blocked else batch
        sendable_ids = real_ids
        # Reported on the internal channel only (the wire format is pinned):
        # a launch session must see these as excluded, or it re-plans them
        # every pass and ends "made no progress".
        res_out["dnc_blocked_ids"] = [c for c in batch if c in dnc_blocked]

        # Per-candidate contact gate. _send_bulk_interview_core's payload
        # validation 400s the ENTIRE batch when any single resume has no
        # usable phone/email — including the "Unknown Candidate" stub
        # generate-payload emits for an id with no sourced_candidates row.
        # Drop those rows here and surface them as exclusions.
        try:
            payload_obj = json.loads(payload_str)
        except Exception:
            payload_obj = None
        resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
        if isinstance(resumes, list) and len(resumes) == len(real_ids):
            launchable: List[tuple] = []
            batch_dropped: List[Dict[str, str]] = []
            for cid, resume in zip(real_ids, resumes):
                if not isinstance(resume, dict):
                    batch_dropped.append({
                        "candidate_id": cid, "name": "",
                        "reason": "invalid resume payload at launch",
                    })
                    continue
                clean_email = _sanitize_pair_candidate_email(str(resume.get("email") or ""))
                resume["email"] = clean_email
                has_contact = (
                    len(_pair_phone_digits(resume.get("phone"))) >= 7 or bool(clean_email)
                )
                if has_contact:
                    launchable.append((cid, resume))
                    continue
                name = str(resume.get("name") or "")
                batch_dropped.append({
                    "candidate_id": cid,
                    "name": name,
                    "reason": (
                        "no candidate record found at launch"
                        if name == "Unknown Candidate"
                        else "no usable phone or email at launch"
                    ),
                })
            if batch_dropped:
                logger.warning(
                    "launch batch %d: excluding %d contactless/missing rows pre-send: %s",
                    idx, len(batch_dropped), [d["candidate_id"] for d in batch_dropped],
                )
                res_out["excluded_records"].extend(batch_dropped)
                res_out["excluded_count"] += len(batch_dropped)
                real_ids = [cid for cid, _ in launchable]
                sendable_ids = real_ids
                payload_obj["resumes"] = [r for _, r in launchable]
                payload_str = json.dumps(payload_obj)
            if not real_ids:
                # Whole batch was uncontactable — nothing to send.
                res_out["candidate_ids"] = []
                res_out["event"] = {
                    "type": "batch", "index": idx, "status": "completed",
                    "sent": 0, "no_interview": 0, "already_sent": 0,
                    "excluded": len(batch_dropped), "bulk_id": None,
                }
                return res_out
        res_out["candidate_ids"] = list(sendable_ids)

        send_req = models.SendBulkInterviewRequest(
            payload=payload_str,
            real_candidate_ids=real_ids,
            is_initial_launch=False,       # fired once after the loop
            dry_run=False,
            notify_recruiters=False,       # fired once after the loop
            send_job_posting_email=False,  # fired once after the loop
            app_base_url=request.app_base_url,
            recruiter_handoff=request.recruiter_handoff,
            handoff_timeout_sec=request.handoff_timeout_sec,
        )
        # Launch-session hooks ride along only when set, so the legacy call
        # shape (and anything that stands in for the core) is unchanged.
        session_kwargs: Dict[str, Any] = {}
        if claimed_session_id is not None:
            session_kwargs["claimed_session_id"] = claimed_session_id
        if not provision_inline:
            session_kwargs["provision_inline"] = False
        if bulk_id_for is not None:
            session_kwargs["bulk_id_for"] = bulk_id_for
        if recruiter_email:
            session_kwargs["recruiter_email"] = recruiter_email
        if launch_id is not None or bulk_id_for is not None:
            res = await bulk_send._send_bulk_interview_core(
                send_req, precomputed_signals=precomputed_signals,
                launch_id=launch_id, batch_idx=idx, **session_kwargs,
            )
        else:
            res = await bulk_send._send_bulk_interview_core(
                send_req, precomputed_signals=precomputed_signals, **session_kwargs,
            )

        skipped = res.get("skipped_already_sent") or []
        res_out["skipped"] = list(skipped)
        # Server-side exclusions (employed by hiring client, offer extended,
        # …) are surfaced on the stream so the modal reports server skips.
        batch_excluded = res.get("excluded_candidates") or []
        res_out["excluded_records"].extend(batch_excluded)
        # Candidates that passed the gate with weak/unverified employer data.
        # Aggregated only for SUCCESSFUL sends.
        batch_unverified = res.get("employer_unverified") or []

        if res.get("success"):
            rows = res.get("data") or []
            # Only rows PAIR actually created an interview for count as "sent".
            sent = sum(1 for r in rows if isinstance(r, dict) and r.get("interview_id"))
            # Pairbot was silent about these: parked 'processing' until its
            # by-job record settles them — neither sent nor a failure yet.
            unconfirmed_ids = [
                str(r.get("candidate_id")) for r in rows
                if isinstance(r, dict) and not r.get("interview_id") and r.get("unconfirmed")
            ]
            no_interview = len(rows) - sent - len(unconfirmed_ids)
            res_out.update({
                "sent": sent,
                "no_interview": no_interview,
                "unconfirmed": len(unconfirmed_ids),
                "unconfirmed_ids": unconfirmed_ids,
                "already_sent": len(skipped),
                "employer_unverified": list(batch_unverified),
                "employer_unverified_count": len(batch_unverified),
                "sent_ids": [
                    str(r.get("candidate_id")) for r in rows
                    if isinstance(r, dict) and r.get("interview_id")
                ],
                "no_interview_ids": [
                    str(r.get("candidate_id")) for r in rows
                    if isinstance(r, dict) and not r.get("interview_id") and not r.get("unconfirmed")
                ],
                "bulk_id": res.get("bulk_id"),
            })
            res_out["excluded_count"] += len(batch_excluded)
            res_out["event"] = {
                "type": "batch",
                "index": idx,
                "status": "completed",
                "sent": sent,
                "no_interview": no_interview,
                # Only when non-zero: the wire shape is unchanged otherwise.
                **({"unconfirmed": len(unconfirmed_ids)} if unconfirmed_ids else {}),
                "already_sent": len(skipped),
                "excluded": len(batch_excluded),
                "employer_unverified": len(batch_unverified),
                "bulk_id": res.get("bulk_id"),
            }
            return res_out
        return _failed(res.get("message") or "send failed")
    except HTTPException as he:
        # 409 = outreach stopped for this job -> abort the whole launch.
        # Flag it immediately so batches not yet started don't run.
        if getattr(he, "status_code", None) == 409:
            abort_flag["aborted"] = True
            res_out["status"] = "aborted"
            res_out["failed_ids"] = list(sendable_ids)
            res_out["message"] = str(he.detail)
            return res_out
        return _failed(str(he.detail))
    except Exception as e:
        logger.error("launch batch %d failed: %s", idx, e, exc_info=True)
        return _failed(str(e))


def _adopt_trace_run(run_id: str, job_id: Any) -> None:
    """Make ``run_id`` the pipeline-trace run of this task (core.pipeline_trace
    ``adopt_run`` when present; fail-open no-op otherwise)."""
    try:
        from core import pipeline_trace as _pt
        fn = getattr(_pt, "adopt_run", None)
        if callable(fn):
            fn(run_id, job_id)
    except Exception:  # noqa: BLE001 — tracing never breaks a launch
        pass


def _strip_internal(evt: Dict[str, Any]) -> Dict[str, Any]:
    """The wire (SSE / launch_events) form of a run_launch event."""
    if "_internal" not in evt:
        return evt
    return {k: v for k, v in evt.items() if k != "_internal"}


async def run_launch(
    request: "LaunchRequest",
    *,
    launch_id: Optional[str] = None,
    run_id: Optional[str] = None,
    side_effects_guard: Optional[Callable[[], Any]] = None,
    fire_side_effects: bool = True,
    should_stop: Optional[Callable[[], Any]] = None,
    provision_inline: bool = True,
    claimed_session_id: Optional[str] = None,
    bulk_id_for: Optional[Callable[..., Any]] = None,
    recruiter_email: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """The launch pipeline behind /engage/launch, as an async generator of
    event dicts: ``start`` → ``batch``* (completion order) → optional
    ``error`` (409 outreach stopped) → ``done``.

    Each event is the exact wire event plus one ``_internal`` key that the
    endpoint strips (`_strip_internal`), so the SSE / launch_events format is
    unchanged. ``_internal`` carries what a server-side consumer needs:

      start  {"batches": [[ids]…]}
      batch  {"status", "candidate_ids" (tried to send), "sent_ids",
              "no_interview_ids", "failed_ids", "skipped_ids" (already sent),
              "excluded_ids", "bulk_id"}
      done   {"sent_ids", "no_interview_ids", "skipped_ids", "excluded_ids",
              "failed_ids", "not_run_ids", "stopped"}

    Session hooks (defaults = the endpoint's behaviour):
      run_id              adopt this pipeline-trace run id (else a new one).
      fire_side_effects   False = never fire the recruiter email / applicant
                          sync here (a session fires them once itself).
      side_effects_guard  sync ``() -> bool`` consulted once, right before the
                          side effects would fire; falsy or raising = skip.
      should_stop         sync ``() -> bool``, checked next to the 409 abort
                          flag before each batch starts (raising = stop).
                          Batches not started come back in done's
                          ``_internal.not_run_ids`` with ``stopped`` True.
      provision_inline / claimed_session_id / bulk_id_for
                          passed to `_send_bulk_interview_core` per batch.

    Consume it with ``contextlib.aclosing(run_launch(...))``. Helpers
    (`_run_one_batch`, `_send_bulk_interview_core`, `_precompute_launch_signals`,
    `_generate_payload_for`) are looked up as module globals at call time.
    """
    # Fix 0: run id for per-stage timing. Set in this task's context BEFORE
    # batch tasks are created so they inherit it.
    if run_id is None:
        run_id = _trace_new_run(request.job_id)
    else:
        _adopt_trace_run(run_id, request.job_id)
    # Fix 5: pause background JobDiva hydration while launching.
    _clear_active = None
    try:
        from services.jobdiva_activity import mark_job_active, clear_job_active
        await mark_job_active(str(request.job_id))
        _clear_active = clear_job_active
    except ImportError:
        pass
    except Exception as _mark_err:  # noqa: BLE001
        logger.warning("launch: mark_job_active failed: %s", _mark_err)

    ids = [str(c) for c in (request.candidate_ids or []) if str(c).strip()]
    batch_size = max(1, int(request.batch_size or _LAUNCH_PAIRBOT_BATCH_SIZE))
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    parallelism = max(1, int(_launch_flag("LAUNCH_BATCH_PARALLELISM", 1) or 1))

    start_evt: Dict[str, Any] = {
        "type": "start",
        "total_candidates": len(ids),
        "total_batches": len(batches),
        "batch_size": batch_size,
        "run_id": run_id,
    }
    if launch_id:
        start_evt["launch_id"] = launch_id

    totals = {"sent": 0, "already_sent": 0, "failed_batches": 0, "no_interview": 0, "excluded": 0, "employer_unverified": 0}
    all_skipped: List[str] = []
    all_excluded: List[Dict[str, str]] = []
    all_dnc_blocked: List[str] = []
    all_employer_unverified: List[Dict[str, str]] = []
    failed_candidate_ids: List[str] = []
    all_sent_ids: List[str] = []
    all_no_interview_ids: List[str] = []
    not_run_ids: List[str] = []
    stopped = False
    aborted = False
    side_effects_fired = {"done": False}
    # The fire-once ledger's "this launch" boundary: the job's first interview
    # at or after this moment came from THIS launch (launch_effects_ledger).
    launch_started_at = datetime.now(timezone.utc)
    abort_flag = {"aborted": False}
    tasks: List[asyncio.Task] = []

    def _stop_requested() -> bool:
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception as exc:  # noqa: BLE001 — unsure → start nothing new
            logger.warning("launch: should_stop raised (stopping): %s", exc)
            return True

    def _fire_once_side_effects() -> None:
        # Recruiter launch email + applicant sync fire ONCE per launch.
        # Invoked from the finally below so a client disconnect mid-stream
        # still fires them for candidates already engaged.
        if side_effects_fired["done"]:
            return
        side_effects_fired["done"] = True
        if not fire_side_effects or totals["sent"] <= 0:
            return
        if side_effects_guard is not None:
            try:
                if not side_effects_guard():
                    return
            except Exception as exc:  # noqa: BLE001 — at most once: unsure → skip
                logger.warning("launch: side-effects guard failed (not firing): %s", exc)
                return
        requested_job_posting = (
            request.send_job_posting_email
            if request.send_job_posting_email is not None
            else request.is_initial_launch
        )
        fire_launch_side_effects(
            request.job_id,
            sent_count=totals["sent"],
            notify_recruiters=request.notify_recruiters,
            # Public job-board posting only on a fully clean launch.
            send_job_posting_email=bool(
                requested_job_posting and not aborted and totals["failed_batches"] == 0
            ),
            is_initial_launch=request.is_initial_launch,
            app_base_url=request.app_base_url,
            launch_started_at=launch_started_at,
        )

    # Per-batch session hooks ride along only when set (legacy call shape).
    batch_kwargs: Dict[str, Any] = {}
    if claimed_session_id is not None:
        batch_kwargs["claimed_session_id"] = claimed_session_id
    if not provision_inline:
        batch_kwargs["provision_inline"] = False
    if bulk_id_for is not None:
        batch_kwargs["bulk_id_for"] = bulk_id_for
    if recruiter_email:
        batch_kwargs["recruiter_email"] = recruiter_email

    try:
        yield {**start_evt, "_internal": {"batches": [list(b) for b in batches]}}

        precomputed: Optional[Dict[str, Dict[str, Any]]] = None
        if ids and _launch_flag("LAUNCH_GATE_ONCE", False):
            async with _trace_span("employer_gate", scope="launch", n=len(ids)):
                precomputed = await _precompute_launch_signals(request.job_id, ids)

        sem = asyncio.Semaphore(parallelism)

        async def _guarded(i: int, b: List[str]) -> Dict[str, Any]:
            async with sem:
                if abort_flag["aborted"] or _stop_requested():
                    return {"idx": i, "status": "not_run", "failed_ids": list(b)}
                if i > 0:
                    await asyncio.sleep(_LAUNCH_BATCH_DELAY_SECONDS)  # stagger
                if abort_flag["aborted"] or _stop_requested():
                    return {"idx": i, "status": "not_run", "failed_ids": list(b)}
                async with _trace_span("launch_batch", batch_idx=i, n=len(b)):
                    return await _run_one_batch(
                        i, b, request=request, precomputed_signals=precomputed,
                        abort_flag=abort_flag, launch_id=launch_id, **batch_kwargs,
                    )

        tasks = [asyncio.ensure_future(_guarded(i, b)) for i, b in enumerate(batches)]
        abort_result: Optional[Dict[str, Any]] = None
        not_run: List[Dict[str, Any]] = []
        # Only this consumer loop touches the shared totals — no lock.
        for fut in asyncio.as_completed(tasks):
            r = await fut
            status = r.get("status")
            if status == "not_run":
                not_run.append(r)
                continue
            all_skipped.extend(r["skipped"])
            all_excluded.extend(r["excluded_records"])
            all_dnc_blocked.extend(str(c) for c in (r.get("dnc_blocked_ids") or []))
            totals["excluded"] += r["excluded_count"]
            if status == "aborted":
                abort_result = r
                continue
            if status == "completed":
                totals["sent"] += r["sent"]
                totals["already_sent"] += r["already_sent"]
                totals["no_interview"] += r["no_interview"]
                if r.get("unconfirmed"):
                    # Only present when non-zero (wire shape unchanged otherwise).
                    totals["unconfirmed"] = totals.get("unconfirmed", 0) + int(r["unconfirmed"])
                totals["employer_unverified"] += r["employer_unverified_count"]
                all_employer_unverified.extend(r["employer_unverified"])
                all_sent_ids.extend(r.get("sent_ids") or [])
                all_no_interview_ids.extend(r.get("no_interview_ids") or [])
            else:
                totals["failed_batches"] += 1
                failed_candidate_ids.extend(r["failed_ids"])
            if r.get("event"):
                yield {
                    **r["event"],
                    "_internal": {
                        "status": status,
                        "candidate_ids": list(r.get("candidate_ids") or []),
                        "sent_ids": list(r.get("sent_ids") or []),
                        "no_interview_ids": list(r.get("no_interview_ids") or []),
                        "failed_ids": list(r.get("failed_ids") or []),
                        "skipped_ids": list(r.get("skipped") or []),
                        "excluded_ids": [
                            str(e.get("candidate_id")) for e in (r.get("excluded_records") or [])
                            if isinstance(e, dict) and e.get("candidate_id")
                        ] + [str(c) for c in (r.get("dnc_blocked_ids") or [])],
                        "bulk_id": r.get("bulk_id"),
                    },
                }

        if abort_result is not None:
            # Same shape as the sequential flow: the aborting batch plus
            # every batch that never ran, in batch order.
            aborted = True
            remaining = list(abort_result["failed_ids"]) + [
                c for nr in sorted(not_run, key=lambda x: x["idx"]) for c in nr["failed_ids"]
            ]
            totals["failed_batches"] += 1
            failed_candidate_ids.extend(remaining)
            yield {
                "type": "error",
                "status_code": 409,
                "message": abort_result.get("message") or "",
                "index": abort_result["idx"],
                "candidate_ids": remaining,
            }
        elif not_run:
            # should_stop: batches never started — resumable, not failed.
            stopped = True
            not_run_ids = [
                c for nr in sorted(not_run, key=lambda x: x["idx"]) for c in nr["failed_ids"]
            ]
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Fire once even if the client disconnected mid-stream.
        _fire_once_side_effects()
        if _clear_active is not None:
            try:
                await _clear_active(str(request.job_id))
            except Exception as _clr_err:  # noqa: BLE001
                logger.warning("launch: clear_job_active failed: %s", _clr_err)

    yield {
        "type": "done",
        "aborted": aborted,
        "totals": totals,
        "skipped_already_sent": all_skipped,
        "excluded_candidates": all_excluded,
        "employer_unverified": all_employer_unverified,
        "failed_candidate_ids": failed_candidate_ids,
        "_internal": {
            "sent_ids": all_sent_ids,
            "no_interview_ids": all_no_interview_ids,
            "skipped_ids": list(all_skipped),
            "excluded_ids": [
                str(e.get("candidate_id")) for e in all_excluded
                if isinstance(e, dict) and e.get("candidate_id")
            ] + all_dnc_blocked,
            "failed_ids": list(failed_candidate_ids),
            "not_run_ids": not_run_ids,
            "stopped": stopped,
        },
    }


# Hard cap so a single sync run can never blast more than this many
# candidates at pairbot, even if the job's filters are loose. The auto-sync
# cron path uses this to avoid an unbounded blast radius.
AUTO_LAUNCH_BATCH_CAP = 25


async def auto_launch_for_candidates(candidate_ids: List[str], job_id: str) -> None:
    """Auto-launch pairbot interviews for newly-synced JobDiva applicants.

    Called from `auto_assign_service.synchronize_job_applicants` after it
    upserts new rows into `sourced_candidates`. Closes the gap where the
    15-min cron created candidate rows but never actually launched them
    to pairbot — so recruiters had to manually click Engage on every new
    applicant.

    Guards:
      - Only runs for jobs that have already been launched at least once
        (≥1 row with engage_status in sent/pending/completed). This stops
        us from auto-blasting brand-new jobs whose recruiter hasn't yet
        clicked "Launch PAIR".
      - Skips candidates that already have an engage_status set, are on
        the DNC list, or whose job has outreach_stopped_at set.
      - Hard-capped at AUTO_LAUNCH_BATCH_CAP per call.

    Failures are logged and swallowed — this runs as a background task and
    must not crash the calling sync cycle.
    """
    if not candidate_ids or not job_id:
        return
    try:
        eligible_ids: List[str] = []
        gate_rows: List[Tuple[str, Dict[str, Any]]] = []
        conn = common._get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            # Job-level gating: outreach must not be stopped, and the
            # recruiter must have launched PAIR at least once before — we
            # treat any prior engage event as the signal that auto-launch
            # is wanted for incoming applicants on this job.
            cur.execute(
                """
                SELECT outreach_stopped_at
                FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            job_row = cur.fetchone()
            if job_row and job_row.get("outreach_stopped_at") is not None:
                logger.info(
                    "auto_launch_skip job_id=%s reason=outreach_stopped",
                    job_id,
                )
                return

            cur.execute(
                """
                SELECT 1 FROM sourced_candidates
                WHERE (jobdiva_id = %s OR jobdiva_id = %s)
                  AND COALESCE(data->>'engage_status', '') != ''
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            if not cur.fetchone():
                logger.info(
                    "auto_launch_skip job_id=%s reason=no_prior_launch count=%d",
                    job_id,
                    len(candidate_ids),
                )
                return

            # Filter to candidates that haven't already been engaged and
            # aren't DNC-blocked. We re-check at the DB layer rather than
            # trusting the caller's list, since the sync run could overlap
            # with a manual Engage click.
            cur.execute(
                """
                SELECT customer_name
                FROM monitored_jobs
                WHERE job_id = %s OR jobdiva_id = %s
                LIMIT 1
                """,
                (str(job_id), str(job_id)),
            )
            client_row = cur.fetchone()
            client_name = str(client_row["customer_name"]) if client_row and client_row.get("customer_name") else ""

            cur.execute(
                """
                SELECT candidate_id, data, headline, resume_text, source
                FROM sourced_candidates
                WHERE candidate_id = ANY(%s)
                  AND (jobdiva_id = %s OR jobdiva_id = %s)
                  AND dnc_stopped_at IS NULL
                  AND COALESCE(data->>'engage_status', '') = ''
                  AND NOT (
                      -- Skip jobdiva.local candidates with no verified phone in the DB.
                      email ILIKE '%%jobdiva.local%%'
                      AND REGEXP_REPLACE(COALESCE(phone, ''), '\\D', '', 'g') = ''
                  )
                  AND NOT (
                      -- Exclude jobdiva.local applicants that are phone-duplicates of a real candidate
                      email ILIKE '%%jobdiva.local%%'
                      AND REGEXP_REPLACE(SPLIT_PART(COALESCE(email, ''), '@', 1), '\\D', '', 'g') <> ''
                      AND EXISTS (
                          SELECT 1 FROM sourced_candidates sc2
                          WHERE (sc2.jobdiva_id = %s OR sc2.jobdiva_id = %s)
                            AND sc2.candidate_id != sourced_candidates.candidate_id
                            AND sc2.email NOT ILIKE '%%jobdiva.local%%'
                            AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g') <> ''
                            AND REGEXP_REPLACE(COALESCE(sc2.phone, ''), '\\D', '', 'g')
                                = REGEXP_REPLACE(SPLIT_PART(COALESCE(sourced_candidates.email, ''), '@', 1), '\\D', '', 'g')
                      )
                  )
                """,
                (list(candidate_ids), str(job_id), str(job_id), str(job_id), str(job_id)),
            )
            candidate_rows = cur.fetchall() or []
            # This is the path that contacted a Bank of America employee for a
            # Bank of America req: the applicant sync persists `data` before
            # resume extraction, so every employer field the gate reads is
            # absent from it. Hydrate from candidate_enhanced_info (+ the
            # headline column) so the gate judges the same candidate the
            # recruiter sees, not an empty row.
            employer_signals = payloads._fetch_stored_employer_signals(
                conn, [str(r["candidate_id"]) for r in candidate_rows]
            )
            for r in candidate_rows:
                c_data = r.get("data") or {}
                if isinstance(c_data, str):
                    try:
                        c_data = json.loads(c_data)
                    except Exception:
                        c_data = {}
                c_data = _merge_employer_signals(
                    c_data,
                    headline=str(r.get("headline") or ""),
                    signals=employer_signals.get(str(r["candidate_id"])),
                )
                c_data.setdefault("candidate_id", str(r["candidate_id"]))
                # Stored resume + source ride along so the resolution pass
                # can parse without re-fetching from JobDiva.
                if r.get("resume_text") and not c_data.get("resume_text"):
                    c_data["resume_text"] = str(r.get("resume_text"))
                if r.get("source") and not c_data.get("source"):
                    c_data["source"] = str(r.get("source"))
                gate_rows.append((str(r["candidate_id"]), c_data))
        finally:
            cur.close()
            conn.close()

        # Employer resolution (policy 2026-09-02): auto-launch runs unattended,
        # so this is the last point where a missing/unconfident employer can be
        # resolved before outreach — fetch the resume and parse it now, with
        # JobDiva's profile work history as corroboration. Runs after the DB
        # connection is released (it can await resume fetches + LLM parses),
        # and fails open: a resolution error gates on stored signals as before.
        _emp_state_fn = None
        resolved_signals: Dict[str, Dict[str, Any]] = {}
        try:
            from services.employer_resolution import (
                employer_verification_state as _emp_state_fn,
                resolve_employer_signals as _resolve_fn,
            )
        except Exception:  # noqa: BLE001
            _resolve_fn = None
        if _resolve_fn is not None:
            try:
                resolved_signals = await asyncio.wait_for(
                    _resolve_fn([c for _, c in gate_rows]),
                    timeout=240.0
                )
            except Exception as res_err:  # noqa: BLE001
                logger.warning(
                    "auto_launch employer resolution failed job_id=%s (gating on stored signals): %s",
                    job_id, res_err,
                )

        gate_rows = [
            (
                cid,
                _merge_employer_signals(c_data, signals=resolved_signals[cid])
                if resolved_signals.get(cid) else c_data,
            )
            for cid, c_data in gate_rows
        ]
        # Resume freshness (phase 3) — advisory classification only; fails open.
        try:
            from services.employer_resolution import stamp_resume_freshness
            # Same 20s advisory cap as the manual launch site.
            await asyncio.wait_for(
                stamp_resume_freshness([c for _, c in gate_rows]), timeout=20.0
            )
        except Exception as fresh_err:  # noqa: BLE001
            logger.warning(
                "auto_launch resume-freshness check failed job_id=%s: %s", job_id, fresh_err
            )

        unverified_count = 0
        for cid, c_data in gate_rows:
            excluded, reason = is_candidate_excluded_from_pair(c_data, client_name)
            if excluded:
                logger.info("auto_launch_skip candidate=%s job_id=%s reason=%s", cid, job_id, reason)
                continue
            eligible_ids.append(cid)
            if _emp_state_fn is not None:
                emp_state = _emp_state_fn(c_data)
                if emp_state != "verified":
                    unverified_count += 1
                    logger.info(
                        "launch_employer_unverified candidate=%s job_id=%s state=%s client=%r",
                        cid, job_id, emp_state, client_name,
                    )
        if unverified_count:
            logger.warning(
                "auto_launch_employer_unverified job_id=%s count=%d of %d eligible — "
                "weak or unverified employer data (no history, JobDiva profile "
                "only, or a stale resume); see per-candidate state logs above",
                job_id, unverified_count, len(eligible_ids),
            )

        if not eligible_ids:
            logger.info(
                "auto_launch_skip job_id=%s reason=no_eligible_candidates",
                job_id,
            )
            return

        if len(eligible_ids) > AUTO_LAUNCH_BATCH_CAP:
            logger.warning(
                "auto_launch_truncated job_id=%s requested=%d cap=%d",
                job_id, len(eligible_ids), AUTO_LAUNCH_BATCH_CAP,
            )
            eligible_ids = eligible_ids[:AUTO_LAUNCH_BATCH_CAP]

        # Generate payload via the existing builder so JD context, rubric
        # and pre-screen questions stay in sync with manual launches.
        payload_result = await routes.generate_engage_payload(
            GeneratePayloadRequest(candidate_ids=eligible_ids, job_id=job_id)
        )
        payload_str = payload_result.get("payload", "")
        if not payload_str:
            logger.warning(
                "auto_launch_skip job_id=%s reason=empty_payload candidate_count=%d",
                job_id, len(eligible_ids),
            )
            return

        logger.info(
            "auto_launch_dispatch job_id=%s candidate_count=%d",
            job_id, len(eligible_ids),
        )
        result = await routes.send_bulk_interview(
            models.SendBulkInterviewRequest(
                payload=payload_str,
                real_candidate_ids=eligible_ids,
                is_initial_launch=False,
                dry_run=False,
                notify_recruiters=False,
            )
        )
        if not (result or {}).get("success"):
            logger.warning(
                "auto_launch_pairbot_unsuccessful job_id=%s message=%s",
                job_id, (result or {}).get("message"),
            )
    except Exception as exc:  # noqa: BLE001 — background task, must not crash caller
        logger.error(
            "auto_launch_failed job_id=%s candidate_count=%d error=%s",
            job_id, len(candidate_ids), exc,
            exc_info=True,
        )


# Imported last: these sibling modules import from this one at load time,
# and the names below are only looked up at call time.
from . import routes  # noqa: E402
