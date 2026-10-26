"""S1 — sourcing: the browser's relaxation loop, server-side.

``input.search_attempts`` is the chain the browser precomputed at click time:
attempt i's payload is exactly what it would POST to /candidates/search
(its ``job_id`` is bound to the session's job: the endpoint checked access
for that job only). For attempt i (from ``checkpoint.s1.next_attempt``):
stop when the qualified count — ``run.qualified()``, the browser's fixed
60% rule, whatever the session's min_score — already reached the target
(i > 0); else consume
``routers.candidates.iter_search_events`` (the endpoint's own stream, gender
patches included) through ONE ``rules.StreamRun`` for the whole run — the D2
port of the browser's stream parser, so the pool, the merge map and the
qualified count are the browser's.

Durability. Every flush (≤ PAIR_LAUNCH_STREAM_FLUSH_EVENTS events or
PAIR_LAUNCH_STREAM_FLUSH_MS) writes, in one fenced transaction: the raw
events (launch_session_stream, for replay), the table rows that changed (the
browser's live table; résumé text / LinkedIn profile left out until the end)
and checkpoint.s1 (attempt, pool, qualified, source statuses, stage text,
JobDiva-criteria flag, errors, detail-failed count). An attempt that is
interrupted is re-run from its start (its partial events are deleted first;
the search caches make that cheap: Exa Pass A is served from the Redis search
cache within its TTL, and Pass B is off by default); finished
attempts are rebuilt by replaying their events through a fresh StreamRun —
the same reducer, so the same state. Cross-submissions are scheduled once per
session (flag written BEFORE the first stream starts).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from services import exa_telemetry
from services.launch_sessions import config as cfg
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.store import finite_score, jsonable, light_row

logger = logging.getLogger(__name__)

_SKIP_EVENTS = ("keepalive",)


def _target(ctx: StageContext) -> int:
    raw = ctx.input.get("qualified_target")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return rules.QUALIFIED_TARGET_COUNT


def bound_payload(ctx: StageContext, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The attempt's search payload with ``job_id`` bound to this session's
    job (control.create_session already refused any other job; this keeps a
    stored snapshot from ever searching — and emailing — for another job)."""
    out = dict(payload or {})
    job_id = str(out.get("job_id") or "").strip()
    if job_id not in ctx.job_keys:
        if job_id:
            logger.warning("launch_session search payload job_id rebound session=%s", ctx.sid)
        out["job_id"] = ctx.job_id
    return out


def _normalise(event: Dict[str, Any]) -> Dict[str, Any]:
    """The event as the browser sees it (a JSON round trip): live state and a
    replay from the stored copy are then built from identical input."""
    return json.loads(json.dumps(jsonable(event)))


def ui_state(run: rules.StreamRun) -> Dict[str, Any]:
    st = run.state
    errors = list(st.get("errors") or [])
    return {
        "source_statuses": st.get("sourceStatuses") or {},
        "search_status": st.get("searchStatus") or "",
        "criteria_unconfigured": bool(st.get("jobdivaCriteriaUnconfigured")),
        "criteria_modal_requested": bool(st.get("criteriaModalRequested")),
        "detail_failed_count": len(st.get("detailFailedIds") or []),
        "errors": [str(e)[:300] for e in errors[-10:]],
        "error_count": len(errors),
    }


class TableTracker:
    """What changed in ``run.table`` since the last flush.

    Table rows are immutable (every patch / merge builds a new dict — see
    rules.deduplicate_candidates_ui), so object identity is the change test.
    ``ord`` is the first-seen order of an id (stable while rows reorder)."""

    def __init__(self) -> None:
        self.flushed: Dict[str, Any] = {}
        self.ord: Dict[str, int] = {}

    def _ord(self, cid: str) -> int:
        if cid not in self.ord:
            self.ord[cid] = len(self.ord)
        return self.ord[cid]

    def diff(self, run: rules.StreamRun, *, full: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """(records to upsert, {id: row} now in the table). ``full``: every
        table row with its complete row (end of sourcing)."""
        current: Dict[str, Any] = {}
        for row in run.table:
            cid = rules.candidate_row_id(row)
            if cid and cid not in current:
                current[cid] = row
        out: List[Dict[str, Any]] = []
        for cid, row in current.items():
            if full or self.flushed.get(cid) is not row:
                out.append({
                    "candidate_id": cid, "ord": self._ord(cid), "source": row.get("source"),
                    "row": row if full else light_row(row), "match_score": finite_score(row.get("match_score")),
                    "state": S.ROW_POOLED, "reason": None,
                })
        absorbed = run._absorbed  # raw merge map: display only (see rules.resolve_absorbed)
        for cid, old in self.flushed.items():
            if cid not in current:
                survivor = absorbed.get(cid)
                out.append({
                    "candidate_id": cid, "ord": self._ord(cid), "source": old.get("source"),
                    "row": old if full else light_row(old), "match_score": finite_score(old.get("match_score")),
                    "state": S.ROW_DROPPED, "reason": f"merged:{survivor}" if survivor else "dropped",
                })
        self.flushed = current
        return out, current


async def replay(ctx: StageContext, upto: int, stats: List[Dict[str, Any]]) -> rules.StreamRun:
    """A StreamRun rebuilt from the stored events of attempts [0, upto)."""
    run = rules.StreamRun()
    if upto <= 0:
        return run
    chunks = await ctx.db(ctx.store.stream_load, ctx.sid)
    for attempt in range(upto):
        stat = stats[attempt] if attempt < len(stats) else {}
        events = chunks.get(attempt) or []
        if stat.get("began", bool(events)):
            run.begin_attempt()
        if events:
            await asyncio.to_thread(run.apply_many, events)
    return run


async def stream_run_for(ctx: StageContext) -> rules.StreamRun:
    """The run of this session: the one S1 built in this claim, else a replay
    of every finished attempt (S2 after a crash)."""
    run = ctx.memo.get("stream_run")
    if run is not None:
        return run
    s1 = ctx.ck.get("s1") or {}
    run = await replay(ctx, int(s1.get("next_attempt") or 0), list(s1.get("attempts") or []))
    ctx.memo["stream_run"] = run
    return run


async def run(ctx: StageContext) -> StageResult:
    attempts = list(ctx.input.get("search_attempts") or [])
    s1: Dict[str, Any] = dict(ctx.ck.get("s1") or {})
    if s1.get("done"):
        return StageResult(next_stage=S.STAGE_SELECTING)
    stats: List[Dict[str, Any]] = list(s1.get("attempts") or [])
    next_attempt = int(s1.get("next_attempt") or 0)
    target = _target(ctx)

    run_ = await replay(ctx, next_attempt, stats)
    ctx.memo["stream_run"] = run_
    tracker = TableTracker()
    ctx.memo["table_tracker"] = tracker
    if not ctx.session.get("sourcing_started_at"):
        await ctx.update(coalesce_now=["sourcing_started_at"])

    def progress(i: int, *, current: Optional[str] = None) -> Dict[str, Any]:
        return {
            **s1, "next_attempt": next_attempt, "attempts": stats, "attempts_total": len(attempts),
            "current_attempt": i, "current_label": current, "pool": len(run_.table),
            "qualified": run_.qualified(), "ui": ui_state(run_),
        }

    for i in range(next_attempt, len(attempts)):
        if i > 0 and run_.qualified() >= target:
            break
        await ctx.boundary()
        label = str(attempts[i].get("label") or f"attempt {i + 1}")
        await ctx.db(ctx.store.stream_delete, ctx.fence, from_attempt=i)
        cross = not s1.get("cross_scheduled")
        if cross:
            # At most once per session: recorded before the stream starts.
            s1["cross_scheduled"] = True
            await ctx.save_ck("s1", progress(i, current=label))
        await ctx.event("attempt_started", stage=S.STAGE_SOURCING, attempt=i, attempts_total=len(attempts), label=label)
        request = ctx.fx.build_search_request(bound_payload(ctx, attempts[i].get("payload") or {}))
        stat: Dict[str, Any] = {"label": label, "began": False, "started_at": time.time()}
        buffer: List[Dict[str, Any]] = []
        chunk = 0
        last_flush = time.monotonic()

        async def flush() -> None:
            nonlocal buffer, chunk, last_flush
            events, buffer = buffer, []
            last_flush = time.monotonic()
            if events:
                await asyncio.to_thread(run_.apply_many, events)
            rows, _ = tracker.diff(run_)
            pool, qualified = len(run_.table), run_.qualified()
            cancel = await ctx.db(
                ctx.store.s1_flush, ctx.fence, attempt=i, chunk=chunk, events=events, rows=rows,
                checkpoint_s1=progress(i, current=label),
                counts={"pool": pool, "qualified": qualified, "attempt": i + 1, "attempts_total": len(attempts)},
            )
            if cancel:
                ctx.lease.cancel_requested = True  # Stop: acted on right below
            if events:
                chunk += 1
            await ctx.boundary()

        # ExaCall attribution: search_candidates() starts a new pipeline run,
        # so the session id is what joins this attempt's Exa spend.
        with exa_telemetry.scope(trigger="launch_s1", attempt=i, launch_session_id=ctx.sid):
            async with contextlib.aclosing(
                ctx.fx.search_events(request, schedule_cross_submissions=cross)
            ) as stream:
                async for raw in stream:
                    if isinstance(raw, dict) and raw.get("type") in _SKIP_EVENTS:
                        if time.monotonic() - last_flush >= cfg.stream_flush_s():
                            await flush()
                        continue
                    if not stat["began"]:
                        run_.begin_attempt()
                        stat["began"] = True
                    buffer.append(_normalise(raw) if isinstance(raw, dict) else raw)
                    if len(buffer) >= cfg.stream_flush_events() or time.monotonic() - last_flush >= cfg.stream_flush_s():
                        await flush()
        if not stat["began"]:
            # The stream answered and ended without a row: it still began
            # (the browser clears / appends the table on an OK response).
            run_.begin_attempt()
            stat["began"] = True
        await flush()
        stat.update({"rows": len(run_.state.get("runList") or []), "qualified": run_.qualified(),
                     "done_at": time.time()})
        stats.append(stat)
        next_attempt = i + 1
        await ctx.save_ck("s1", progress(i))
        await ctx.event(
            "attempt_done", stage=S.STAGE_SOURCING, attempt=i, rows=stat["rows"],
            qualified=stat["qualified"], pool=len(run_.table),
        )

    # The final table, complete rows; anything else stored is dropped.
    await ctx.boundary()
    rows, current = tracker.diff(run_, full=True)
    rows = [r for r in rows if r["state"] == S.ROW_POOLED]
    await ctx.db(ctx.store.upsert_rows, ctx.fence, rows)
    stale = [cid for cid in await ctx.db(ctx.store.candidate_ids, ctx.sid, [S.ROW_POOLED]) if cid not in current]
    if stale:
        await ctx.set_states(
            [{"candidate_id": c, "state": S.ROW_DROPPED, "reason": "dropped"} for c in stale],
            from_states=[S.ROW_POOLED],
        )
    s1.update(progress(next_attempt - 1 if next_attempt else 0))
    s1["done"] = True
    await ctx.update(
        coalesce_now=["sourcing_done_at"], checkpoint={"s1": s1},
        counts={"pool": len(run_.table), "qualified": run_.qualified()},
    )
    return StageResult(next_stage=S.STAGE_SELECTING)
