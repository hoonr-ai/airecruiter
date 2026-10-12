"""The launch-session runner: claims, leases, the stage loop, housekeeping.

Per worker (8 uvicorn workers, one runner each):

- ``tick()`` (APScheduler every PAIR_LAUNCH_TICK_S, plus once at startup to
  adopt sessions orphaned by a deploy) only CLAIMS — up to the free local
  slots (PAIR_LAUNCH_WORKER_CONCURRENCY; reserved synchronously before the
  claim is awaited, so a tick and a POST's claim_now never both take one)
  and, inside the claim transaction under pg_advisory_xact_lock(728197), the
  free global slots: PAIR_LAUNCH_SEARCH_CONCURRENCY for full searches in
  their search phase (S1/S2), PAIR_LAUNCH_GLOBAL_CONCURRENCY for the launch
  phase (S3-S7), none for S7b/S8 (JobDiva provisioning, the wrap-up). A
  session crossing from search to launch is handed over (lease
  released, re-claimed under the launch cap — at once when there is room),
  so each cap holds exactly. Each claim runs as a tracked
  ``core.tasks.spawn`` task (nr_txn=False: every stage opens its own
  ``background_transaction("launch_session/<stage>:<id>")``). POST create
  calls ``claim_now(id)`` so a new session starts without waiting for a tick.
- A claim = ``(id, claims)``; every write is fenced by owner + claims (see
  store.py). A heartbeat renews the lease every PAIR_LAUNCH_HEARTBEAT_S
  (LEASE PAIR_LAUNCH_LEASE_S = 180 s: a SIGKILLed worker's sessions are
  adopted within ~3 min); a failing heartbeat self-fences the claim.
- Shutdown (core.tasks.stopping, set first in lifespan shutdown): checked at
  every unit boundary → owner-scoped release (lease_until = now(), owner
  cleared, status stays running) so another worker adopts it; on
  CancelledError (the drain's cancel) the release is a synchronous
  statement, never awaited. Whatever must be written under the claim (a
  progress event) is written before the release.
- Errors: RetryLater → release with not_before, attempt NOT counted; a stage
  exception → attempt + 1, release with not_before = min(15 min, 30 s·2^(n-1));
  attempt ≥ PAIR_LAUNCH_MAX_ATTEMPTS → reconcile-only pass, then failed.
  409 outreach stopped → cancelled; 403 → retried, then failed ('access
  revoked'). Cancel requested (seen on claim, heartbeat, assert_owner, an S1
  flush or any fenced update; a cancel-requested session is claimed first,
  past the caps, and its releases set no back-off unless its finalize
  failed) → finalize: reconcile-only pass when anything may have been
  posted, JobDiva provisioning of whoever was launched, the claims handed
  back, recruiter email (+ applicant sync) if ≥1 launched, cancelled.
- ``housekeep()`` (every PAIR_LAUNCH_HOUSEKEEPING_S, one worker per run via
  pg_try_advisory_xact_lock; it runs with the runner off too): collecting
  > 15 min → cancelled, awaiting_input > 24 h → cancelled, queued and never
  claimed > PAIR_LAUNCH_QUEUED_MAX_S (30 min with the runner off, 6 h on) → cancelled 'queue_expired',
  retention of rows of sessions finished > 14 days, and
  the by-job re-check of unconfirmed rows for 24 h (rows it proves launched
  are provisioned to JobDiva).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import weakref
from typing import Any, Callable, Dict, List, Optional

from core import tasks as core_tasks
from services.launch_sessions import config as cfg
from services.launch_sessions import events as ev
from services.launch_sessions import schema as S
from services.launch_sessions.context import Lease, StageContext, StageResult
from services.launch_sessions.effects import Effects
from services.launch_sessions.errors import (
    AccessDenied,
    CancelRequested,
    LeaseExpired,
    OutreachStopped,
    OwnershipLost,
    RetryLater,
    Shutdown,
)
from services.launch_sessions.store import Claim, Fence, PgStore

logger = logging.getLogger(__name__)

_POSTING_STAGES = (S.STAGE_SAVING, S.STAGE_LAUNCHING, S.STAGE_PROVISIONING, S.STAGE_DONE)


def _owner_id() -> str:
    try:
        from core.startup_once import BOOT_ID
    except Exception:  # noqa: BLE001
        BOOT_ID = "boot"
    return f"{BOOT_ID}:{os.getpid()}"


def backoff_s(attempt: int) -> float:
    return float(min(900, 30 * (2 ** max(0, attempt - 1))))


def _nr(name: str):
    try:
        from core import newrelic as _nr_mod

        return getattr(_nr_mod, name)
    except Exception:  # noqa: BLE001
        return None


def _ignore_transaction() -> None:
    fn = _nr("ignore_transaction")
    if fn:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


class _BackgroundTxn:
    """core.newrelic.background_transaction, or a no-op when unavailable."""

    def __init__(self, name: str) -> None:
        cls = _nr("background_transaction")
        self._inner = cls(name) if cls else None

    async def __aenter__(self):
        if self._inner is not None:
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._inner is not None:
            return await self._inner.__aexit__(*exc)
        return False


def _adopt_run(session_id: str, job_id: Any) -> None:
    try:
        from core import pipeline_trace

        pipeline_trace.adopt_run(session_id, job_id)
    except Exception:  # noqa: BLE001
        pass


class LaunchSessionRunner:
    def __init__(
        self,
        store: Any = None,
        fx: Any = None,
        *,
        owner: Optional[str] = None,
        spawn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.store = store or PgStore()
        self.fx = fx or Effects()
        self.owner = owner or _owner_id()
        self._spawn = spawn or core_tasks.spawn
        self.active: Dict[str, Any] = {}
        # Local slots promised to claims still in flight (see _free_slots).
        self._reserved = 0
        self._sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
            weakref.WeakKeyDictionary()
        )

    # ── plumbing ───────────────────────────────────────────────────────────
    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._sems.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(cfg.db_concurrency())
            self._sems[loop] = sem
        return sem

    async def db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Blocking DB / IO work off the loop, at most PAIR_LAUNCH_DB_CONCURRENCY
        at a time per worker (HTTP keeps the rest of the pool)."""
        async with self._sem():
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ── claiming ───────────────────────────────────────────────────────────
    def _free_slots(self) -> int:
        return max(0, cfg.worker_concurrency() - len(self.active) - self._reserved)

    async def _claim(self, limit: int, session_id: Optional[str] = None) -> List[Claim]:
        """Claim up to ``limit`` (never more than the free local slots, which
        are reserved before the await: concurrent callers in this worker
        cannot both take the last one)."""
        limit = min(int(limit), self._free_slots())
        if limit <= 0 or core_tasks.is_stopping() or not cfg.runner_enabled():
            return []
        self._reserved += limit
        try:
            return await self.db(
                self.store.claim, self.owner, limit, global_cap=cfg.global_concurrency(),
                lease_s=cfg.lease_s(), session_id=session_id, search_cap=cfg.search_concurrency(),
            )
        finally:
            self._reserved -= limit

    def _start(self, claim: Claim) -> None:
        self.active[claim.session_id] = claim
        try:
            self._spawn(self.run_claim(claim), name=f"launch_session:{claim.session_id}", nr_txn=False)
        except Exception:
            self.active.pop(claim.session_id, None)
            # Nothing ran: give the lease back so another tick takes it.
            try:
                self.store.release(Fence(claim.session_id, self.owner, claim.claims))
            except Exception as exc:  # noqa: BLE001
                logger.warning("launch_session release after spawn failure failed: %s", exc)
            raise

    async def tick(self) -> int:
        """Claim what this worker may run now. Returns how many it started."""
        if core_tasks.is_stopping() or not cfg.runner_enabled():
            _ignore_transaction()
            return 0
        free = self._free_slots()
        if free <= 0:
            _ignore_transaction()
            return 0
        try:
            claims = await self._claim(free)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session_tick_claim_failed: %s", exc)
            return 0
        if not claims:
            _ignore_transaction()
            return 0
        for claim in claims:
            self._start(claim)
        return len(claims)

    async def claim_now(self, session_id: str) -> bool:
        """POST create: start this session in this worker when there is room."""
        if self._free_slots() <= 0:
            return False
        try:
            claims = await self._claim(1, session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session_claim_now_failed session=%s: %s", session_id, exc)
            return False
        for claim in claims:
            self._start(claim)
        return bool(claims)

    # ── one claim ──────────────────────────────────────────────────────────
    async def run_claim(self, claim: Claim) -> None:
        fence = Fence(claim.session_id, self.owner, claim.claims)
        lease = Lease(self.store, fence)
        heartbeat = asyncio.ensure_future(lease.heartbeat_loop(self.db))
        handoff = False
        try:
            handoff = await self._drive(lease)
        except asyncio.CancelledError:
            # Shutdown drain: give the session back NOW, synchronously — an
            # awaited release could itself be cancelled.
            if not lease.lost and not lease.released:
                self._release_sync(lease)
            raise
        except Exception as exc:  # noqa: BLE001 — never let a runner crash silently
            logger.error("launch_session_runner_crashed session=%s err=%s", claim.session_id, exc, exc_info=True)
            if not lease.lost and not lease.released:
                self._release_sync(lease, not_before_s=backoff_s(1), cancel_skips_wait=False)
        finally:
            heartbeat.cancel()
            self.active.pop(claim.session_id, None)
        if handoff:
            # Search → launch: re-claim it under the launch cap now (else a tick).
            try:
                self._spawn(self.claim_now(claim.session_id), name=f"launch_session_claim:{claim.session_id}",
                            nr_txn=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("launch_session handoff claim failed session=%s: %s", claim.session_id, exc)

    def _release_sync(
        self, lease: Lease, *, not_before_s: Optional[float] = None, cancel_skips_wait: bool = True,
    ) -> None:
        try:
            if self.store.release(lease.fence, not_before_s=not_before_s,
                                  cancel_skips_wait=cancel_skips_wait) is not None:
                lease.released = True
        except Exception as exc:  # noqa: BLE001 — the lease expiry covers it
            logger.warning("launch_session_release_failed session=%s err=%s", lease.fence.session_id, exc)

    async def _release(self, lease: Lease, **kwargs: Any) -> Optional[int]:
        """Give the session back (owner cleared: nothing more may be written
        under this claim — write events BEFORE calling this)."""
        try:
            out = await self.db(self.store.release, lease.fence, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session_release_failed session=%s err=%s", lease.fence.session_id, exc)
            return None
        if out is not None:
            lease.released = True
        return out

    async def _drive(self, lease: Lease) -> bool:
        """Run the session's stages under this claim. Returns True when it was
        handed over at the search → launch boundary (re-claim it)."""
        session = await self.db(self.store.get_session, lease.fence.session_id)
        if (
            session is None or session.get("owner") != self.owner
            or int(session.get("claims") or 0) != lease.fence.claims or session.get("status") != S.STATUS_RUNNING
        ):
            return False
        ctx = StageContext(self, lease, session)
        _adopt_run(ctx.sid, ctx.job_id)
        if session.get("cancel_requested"):
            lease.cancel_requested = True
        while True:
            stage = str(ctx.session.get("stage") or S.STAGE_CREATED)
            try:
                lease.check()
                if stage == S.STAGE_CREATED:
                    from services.launch_sessions.stages import first_stage

                    await self._advance(ctx, first_stage(ctx.session), 0)
                    continue
                result = await self._run_stage(ctx, stage)
                if result.done:
                    return False
                if result.wait_input:
                    await self._hand_to_recruiter(ctx, stage, result)
                    return False
                await self._advance(ctx, str(result.next_stage), self._ms_in_stage(ctx))
                mode = ctx.session.get("mode")
                if S.in_search_phase(mode, stage) and not S.in_search_phase(mode, ctx.session.get("stage")):
                    # The search is over: the launch phase runs under its own
                    # cap — give the session back and re-claim it under that.
                    await self._release(lease)
                    return True
            except RetryLater as wait:
                await ctx.event("waiting", stage=stage, retry_in_s=int(wait.delay_s), note=wait.note or None)
                await self._release(lease, not_before_s=wait.delay_s)
                return False
            except Shutdown:
                await self._release(lease)
                return False
            except OwnershipLost:
                logger.warning("launch_session_ownership_lost session=%s stage=%s", ctx.sid, stage)
                return False
            except LeaseExpired:
                logger.warning("launch_session_lease_at_risk session=%s stage=%s", ctx.sid, stage)
                await self._release(lease)
                return False
            except CancelRequested:
                await self._terminal(ctx, S.STATUS_CANCELLED, ctx.session.get("cancel_reason") or "cancelled by user")
                return False
            except OutreachStopped:
                await self._terminal(ctx, S.STATUS_CANCELLED, "outreach stopped")
                return False
            except AccessDenied as exc:
                await self._failure(ctx, stage, exc, cap=cfg.access_denied_attempts(), final_error="access revoked")
                return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("launch_session_stage_failed session=%s stage=%s err=%s", ctx.sid, stage, exc,
                             exc_info=True)
                await self._failure(ctx, stage, exc, cap=cfg.max_attempts(), final_error=None)
                return False

    async def _hand_to_recruiter(self, ctx: StageContext, stage: str, result: StageResult) -> None:
        """QA review: release the session to awaiting_input (no owner, no lease)."""
        row = await self.db(self.store.to_awaiting_input, ctx.fence, result.checkpoint or None)
        ctx.session = row
        ev.nr_session_event(row, stage=stage, status=S.STATUS_AWAITING_INPUT, ms_in_stage=self._ms_in_stage(ctx))
        await self.db(self.store.append_event, ctx.sid, ev.payload(
            "awaiting_input", stage=stage, count=(result.checkpoint.get("review") or {}).get("count"),
        ))

    @staticmethod
    def _ms_in_stage(ctx: StageContext) -> Optional[int]:
        started = ctx.memo.get("stage_t0")
        return int((time.monotonic() - started) * 1000) if started else None

    async def _run_stage(self, ctx: StageContext, stage: str) -> StageResult:
        from services.launch_sessions.stages import STAGE_FUNCTIONS

        fn = STAGE_FUNCTIONS.get(stage)
        if fn is None:
            raise RuntimeError(f"unknown stage {stage!r}")
        ctx.memo["stage_t0"] = time.monotonic()
        async with _BackgroundTxn(f"launch_session/{stage}:{ctx.sid}"):
            _adopt_run(ctx.sid, ctx.job_id)
            return await fn(ctx)

    async def _advance(self, ctx: StageContext, next_stage: str, ms: Optional[int]) -> None:
        prev = str(ctx.session.get("stage"))
        await ctx.update(values={"stage": next_stage}, set_now=["stage_started_at"])
        ev.nr_session_event(ctx.session, stage=prev, status="stage_done", ms_in_stage=ms)
        await ctx.event("stage", stage=next_stage, previous=prev, ms_in_previous=ms)

    async def _failure(
        self, ctx: StageContext, stage: str, exc: BaseException, *, cap: int, final_error: Optional[str],
    ) -> None:
        attempt = int(ctx.session.get("attempt") or 0) + 1
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        if attempt >= cap:
            await self._terminal(ctx, S.STATUS_FAILED, final_error or f"{stage} failed: {message}")
            return
        delay = backoff_s(attempt)
        await ctx.event("retry_scheduled", stage=stage, attempt=attempt, retry_in_s=int(delay))
        await self._release(ctx.lease, not_before_s=delay, error=message, count_failure=True)
        ev.nr_session_event({**ctx.session, "attempt": attempt}, stage=stage, status="retry", outcome="error")

    async def _terminal(self, ctx: StageContext, status: str, error: Optional[str]) -> None:
        """Cancelled / failed: a reconcile-only pass when anything may have
        reached Pairbot, the JobDiva provisioning of whoever was launched
        (legacy provisioned per batch), then the shared finish (recruiter
        email + applicant sync, never the job posting)."""
        from services.launch_sessions.stages import finishing, launching, provisioning

        try:
            stage = str(ctx.session.get("stage"))
            if stage in _POSTING_STAGES or (ctx.ck.get("s7") or {}).get("inflight"):
                await launching.settle(ctx, final=True)
        except OwnershipLost:
            return
        except Exception as exc:  # noqa: BLE001 — unsettled rows stay launching / unconfirmed
            logger.warning("launch_session reconcile-only pass failed session=%s: %s", ctx.sid, exc)
        try:
            if await ctx.db(ctx.store.candidate_ids, ctx.sid, [S.ROW_LAUNCHED]):
                await provisioning.provision_launched(ctx, finalizing=True)
        except OwnershipLost:
            return
        except (Shutdown, LeaseExpired):
            # Resumed by the next claim (the cancel / failure is re-raised there).
            await self._release(ctx.lease)
            return
        except Exception as exc:  # noqa: BLE001 — provisioning never blocks a finish
            logger.warning("launch_session finalize provisioning failed session=%s: %s", ctx.sid, exc)
        try:
            await finishing.finish(ctx, status=status, error=error, full_side_effects=False)
        except OwnershipLost:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("launch_session finalize failed session=%s status=%s: %s", ctx.sid, status, exc)
            # The finalize itself failed: back off even for a pending cancel.
            await self._release(ctx.lease, not_before_s=backoff_s(1), cancel_skips_wait=False)

    # ── housekeeping ───────────────────────────────────────────────────────
    async def housekeep(self) -> Dict[str, Any]:
        if core_tasks.is_stopping():
            _ignore_transaction()
            return {"skipped": True}
        try:
            result = await self.db(
                self.store.housekeeping, collecting_ttl_min=cfg.collecting_ttl_min(),
                review_ttl_h=cfg.review_ttl_h(), retention_days=cfg.retention_days(),
                recheck_h=cfg.unconfirmed_recheck_h(), queued_max_s=cfg.queued_max_s(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session_housekeeping_failed: %s", exc)
            return {"error": str(exc)}
        if result.get("queue_expired"):
            # Never started: the runner is off (PAIR_LAUNCH_RUNNER_ENABLED) or
            # the fleet is not claiming — worth a look either way.
            logger.warning(
                "launch_session_queue_expired n=%d runner_enabled=%s sessions=%s",
                len(result["queue_expired"]), cfg.runner_enabled(), ",".join(result["queue_expired"][:20]),
            )
        try:
            result["rechecked"] = await self.recheck_unconfirmed()
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session_recheck_failed: %s", exc)
        if not result.get("skipped") and self._legacy_settle_due():
            # Rows a launch OUTSIDE a session left parked 'processing' (no
            # recorded outcome): settled against Pairbot's by-job record. The
            # one worker that won this tick's housekeeping lock runs it; each
            # row is claimed at most every 10 min fleet-wide.
            try:
                result["legacy_settled"] = await self.fx.settle_parked_legacy_rows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("launch_session_legacy_settle_failed: %s", exc)
        if result.get("skipped") and not result.get("rechecked"):
            _ignore_transaction()
        return result

    def _legacy_settle_due(self) -> bool:
        """At most every PAIR_LEGACY_SETTLE_EVERY_S per worker (the sweep scans
        sourced_candidates for parked rows; the per-row claim keeps it single)."""
        now = time.monotonic()
        last = getattr(self, "_legacy_settle_at", None)
        if last is not None and now - last < cfg.legacy_settle_every_s():
            return False
        self._legacy_settle_at = now
        return True

    async def recheck_unconfirmed(self, limit: int = 5) -> int:
        """By-job re-check of finished sessions' unconfirmed / unrecorded rows
        (never re-posts: a row Pairbot provably never created → failed)."""
        from services.launch_sessions.stages import launching

        due = await self.db(self.store.claim_rechecks, limit=limit, every_s=cfg.recheck_every_s())
        for session in due:
            sid = str(session["id"])
            keys = [k for k in (session.get("job_id"), session.get("jobdiva_ref")) if k]
            rows = await self.db(self.store.candidate_rows, sid, states=[S.ROW_UNCONFIRMED, S.ROW_LAUNCHED])
            todo = [r for r in rows if r["state"] == S.ROW_UNCONFIRMED
                    or (r["state"] == S.ROW_LAUNCHED and r.get("reason") == launching.OUTCOME_UNRECORDED)]
            if not todo:
                await self.db(self.store.finish_recheck, sid, counts={}, done=True)
                continue
            index = launching.posted_index(((session.get("checkpoint") or {}).get("s7") or {}).get("inflight") or {})
            ids = [r["candidate_id"] for r in todo]
            own = sorted({index[c][0] for c in ids if c in index})
            res = await self.fx.reconcile(keys, ids, session_id=sid, own_bulk_ids=own)
            items: List[Dict[str, Any]] = []
            for cid, iid in (res.get("sent") or {}).items():
                items.append({"candidate_id": str(cid), "state": S.ROW_LAUNCHED,
                              "reason": launching.OUTCOME_RECONCILED, "interview_id": str(iid)})
            for cid in res.get("duplicate_of") or {}:
                items.append({"candidate_id": str(cid), "state": S.ROW_SKIPPED, "reason": "duplicate_person_this_job"})
            unconfirmed = {r["candidate_id"] for r in todo if r["state"] == S.ROW_UNCONFIRMED}
            missing = [c for c in res.get("missing") or [] if c in unconfirmed]
            if missing:
                failed = await self.db(self.store.fail_engage_rows, keys, missing, sid)
                items += [{"candidate_id": c, "state": S.ROW_FAILED, "reason": "not_created",
                           "failure_stage": "engage"} for c in failed]
            if items:
                await self.db(self.store.set_states, None, items, session_id=sid)
            # Proven (and now recorded) launches — S7b ran before they settled.
            newly = [str(c) for c in (res.get("sent") or {}) if str(c) in set(ids)]
            if newly:
                await self._provision_late(session, newly)
            folded = await self.db(self.store.state_counts, sid)
            left = int(folded["states"].get(S.ROW_UNCONFIRMED, 0)) + int(
                folded["reasons"].get(S.ROW_LAUNCHED, {}).get(launching.OUTCOME_UNRECORDED, 0)
            )
            await self.db(
                self.store.finish_recheck, sid,
                counts={"states": folded["states"], "reasons": folded["reasons"],
                        "unconfirmed": int(folded["states"].get(S.ROW_UNCONFIRMED, 0)),
                        "failed": int(folded["states"].get(S.ROW_FAILED, 0)),
                        "launched": int(folded["states"].get(S.ROW_LAUNCHED, 0))},
                done=left == 0,
            )
        return len(due)


    async def _provision_late(self, session: Dict[str, Any], ids: List[str]) -> None:
        """Rows the re-check proved launched after the session finished get
        their JobDiva application too (claim-stamped: safe to repeat)."""
        try:
            if not self.fx.provisioning_enabled():
                return
            ref = str(session.get("jobdiva_ref") or session.get("job_id"))
            for i in range(0, len(ids), cfg.provision_chunk()):
                await self.fx.provision(ref, ids[i:i + cfg.provision_chunk()])
        except Exception as exc:  # noqa: BLE001 — the rows stay launched; logged
            logger.warning("launch_session late provisioning failed session=%s: %s", session.get("id"), exc)


_runner: Optional[LaunchSessionRunner] = None


def get_runner() -> LaunchSessionRunner:
    """The worker's runner (created on first use)."""
    global _runner
    if _runner is None:
        _runner = LaunchSessionRunner()
    return _runner
