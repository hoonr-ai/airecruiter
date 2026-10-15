"""S7 — launching: the saved rows to Pairbot, at most once per person.

A pass = claim the rows for this session (engage_status 'queued' +
engage_session_id: legacy paths treat them as already sent), then
``routers.engagement.run_launch`` with ``claimed_session_id`` (the core's send
claim moves each batch to 'sending' right before its POST and posts only what
it won), ``fire_side_effects=False`` (S8 fires them once), ``provision_inline=
False`` (S7b provisions, awaited) and ``should_stop`` (stopping / cancelled /
lease at risk: no new batch starts). Before every POST the core calls our
``bulk_id_for(batch_idx, posted_ids)``: it asserts ownership, then records
``checkpoint.s7.inflight[bulk_id] = {batch, attempt, ids, posted_at}`` (fenced)
and returns the deterministic bulk id
``uuid5(NS, f"{session}:{launch_attempt}:{batch_idx}")`` — Pairbot honours
the client bulk_id, and the record exists BEFORE anything leaves the process.
After every batch the rows' engage state is re-read (the ground truth) and
each row moves to launched / failed / skipped / back to saved.

Settling (every pass start, every pass end, and the reconcile-only pass of a
cancel or a failure) — the rows this session may have posted without a
recorded outcome ('processing', 'sending', or 'queued' AND posted):

  by-job lists it under its own id (``sent``)      → launched (written)
  proven but the DB write failed (``unrecorded``)  → launched; housekeeping
                                                     re-reconciles to record it
  ``duplicate_of`` another id for this job         → skipped (never re-posted)
  ``missing`` and posted > PAIR_LAUNCH_REPOST_AFTER_S ago (or never posted)
                                                   → requeued: re-posted in a
                                                     new pass (new bulk id)
  ``missing`` but posted moments ago, a bulk still creating
  (``bulk_in_progress`` / ``own_bulks_creating``)  → wait (RetryLater)
  by-job ``error`` (Pairbot down)                  → never re-post; wait
  still ambiguous after PAIR_LAUNCH_SETTLE_MAX_S, or in a final pass
                                                   → unconfirmed (counted;
                                                     re-checked 24 h)

Only ``missing`` may ever be re-posted. own_bulk_ids = the bulk ids recorded
for those rows, minus ones posted in the last PAIR_LAUNCH_FRESH_BULK_S.

A batch that fails BEFORE its POST (payload generation, the core's checks —
nothing recorded for it, not our own stop) fails its rows (``launch_failed``,
retryable by a retry-failed child) and hands their claims back: re-planning
them would loop on a deterministic error. Every batch that did not complete
(other than our own stop) counts in ``counts.failed_batches`` — the
job-posting email's "clean launch" rule, as in /engage/launch. A claim's
passes that leave the same rows plannable count as a failed attempt.

When S7 ends, the launch is over: the session's side effects fire here
(finishing.side_effects_once), before S7b provisions in the background.

Hand-back (``hand_back_claims``, when the session finishes): only claims that
were never posted are released; rows that may be at Pairbot without a
recorded outcome (posted + unconfirmed / unrecorded, left 'queued' by D1's
no-answer path) are parked 'processing' with their bulk id so no launch path
can post them again, and the housekeeping re-check settles them.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from services.launch_sessions import config as cfg
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.errors import (
    LaunchSessionStop,
    LeaseExpired,
    OutreachStopped,
    RetryLater,
)
from services.launch_sessions.events import now_iso

logger = logging.getLogger(__name__)

# Fixed namespace for the deterministic per-batch Pairbot bulk ids.
BULK_NAMESPACE = uuid.UUID("5b1c3a52-0f3e-4c6a-9f0d-7a2e8d3b6c41")
_MAX_PASSES_PER_CLAIM = 3
_IN_FLIGHT = ("processing", "sending")

OUTCOME_SENT = "sent"  # this session's POST created it
OUTCOME_RECONCILED = "reconciled"  # Pairbot's by-job proved it (ours)
OUTCOME_UNRECORDED = "unrecorded"  # proven, DB write failed — re-checked
OUTCOME_ALREADY = "already_launched"  # someone else launched this person
SESSION_SENT_REASONS = (OUTCOME_SENT, OUTCOME_RECONCILED, OUTCOME_UNRECORDED)


def bulk_id_for(session_id: str, launch_attempt: int, batch_idx: int) -> str:
    return str(uuid.uuid5(BULK_NAMESPACE, f"{session_id}:{launch_attempt}:{batch_idx}"))


def launch_id_for(session_id: str, launch_attempt: int) -> str:
    return str(uuid.uuid5(BULK_NAMESPACE, f"{session_id}:launch:{launch_attempt}"))


def _parse_ts(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    try:
        parsed = _dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _age_s(value: Any) -> Optional[float]:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds()


def posted_index(posted: Dict[str, Any]) -> Dict[str, Tuple[str, Any]]:
    """{candidate_id: (bulk_id, posted_at)} — the latest record per id."""
    out: Dict[str, Tuple[str, Any]] = {}
    for bulk_id, rec in sorted((posted or {}).items(), key=lambda kv: str((kv[1] or {}).get("posted_at") or "")):
        for cid in (rec or {}).get("ids") or []:
            out[str(cid)] = (bulk_id, (rec or {}).get("posted_at"))
    return out


def classify(
    fact: Optional[Dict[str, Any]], *, session_id: str, posted: bool, claimed: bool,
) -> str:
    """One row's position from its folded engage facts (store.fold_engage_rows):

      launched   an interview id, or engage_status sent
      blocked    data.engage_block_reason (a Pairbot duplicate)
      ours_open  ours and possibly at Pairbot: processing/sending, or queued
                 AND posted — settle before anything else
      launchable ours queued and never posted, or ''/'failed' never posted
      failed     ''/'failed' after we posted it (Pairbot said no)
      in_flight  another path / session holds it
      no_row     no sourced row under the job (save lost)
    """
    if fact is None:
        return "no_row"
    if fact.get("interview_id") or "sent" in fact.get("statuses", []):
        return "launched"
    if fact.get("block_reason"):
        return "blocked"
    statuses = set(fact.get("statuses") or [])
    ours = session_id in (fact.get("session_ids") or [])
    if statuses & set(_IN_FLIGHT):
        return "ours_open" if ours else "in_flight"
    if "queued" in statuses:
        if not ours:
            return "in_flight"
        return "ours_open" if posted else "launchable"
    if posted and claimed and "failed" in statuses:
        return "failed"
    return "launchable"


def _launched_item(cid: str, fact: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {"candidate_id": cid, "state": S.ROW_LAUNCHED, "reason": reason,
            "interview_id": fact.get("interview_id") or None}


async def _facts(ctx: StageContext, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    return await ctx.db(ctx.store.engage_rows, ctx.job_keys, ids)


async def _s7(ctx: StageContext, *, fresh: bool = False) -> Dict[str, Any]:
    """checkpoint.s7, created on first use. ``fresh``: re-read the session
    first — the posted records are written by the core's bulk_id_for hook
    (committed BEFORE each POST), so a DB read after a batch always has them."""
    if fresh:
        await ctx.reload()
    s7 = dict(ctx.ck.get("s7") or {})
    if "inflight" not in s7:
        s7 = {"launch_attempt": 0, "inflight": {}, "settle_since": None, **s7}
        s7.setdefault("inflight", {})
        await ctx.save_ck("s7", s7)
    return s7


# ── settling ────────────────────────────────────────────────────────────────

async def settle(ctx: StageContext, *, final: bool = False) -> Dict[str, int]:
    """Resolve every 'launching' row (see the module docstring). Raises
    RetryLater unless ``final``."""
    s7 = await _s7(ctx, fresh=True)
    ids = await ctx.db(ctx.store.candidate_ids, ctx.sid, [S.ROW_LAUNCHING])
    if not ids:
        if s7.get("settle_since"):
            s7["settle_since"] = None
            await ctx.save_ck("s7", s7)
        return {}
    facts = await _facts(ctx, ids)
    index = posted_index(s7.get("inflight") or {})
    items: List[Dict[str, Any]] = []
    ambiguous: List[str] = []
    for cid in ids:
        kind = classify(facts.get(cid), session_id=ctx.sid, posted=cid in index, claimed=True)
        fact = facts.get(cid) or {}
        if kind == "launched":
            items.append(_launched_item(cid, fact, OUTCOME_SENT))
        elif kind == "blocked":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": f"blocked:{fact['block_reason']}"})
        elif kind == "failed":
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "pairbot_rejected",
                          "failure_stage": "engage"})
        elif kind == "in_flight":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "in_flight"})
        elif kind == "no_row":
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "not_saved",
                          "failure_stage": "engage", "error": "no saved row for this job"})
        elif kind == "launchable":
            items.append({"candidate_id": cid, "state": S.ROW_SAVED, "reason": None})
        else:
            ambiguous.append(cid)
    await ctx.set_states(items, from_states=[S.ROW_LAUNCHING])
    out = {"resolved": len(items), "ambiguous": len(ambiguous)}
    if not ambiguous:
        if s7.get("settle_since"):
            s7["settle_since"] = None
            await ctx.save_ck("s7", s7)
        return out

    fresh = cfg.fresh_bulk_s()
    own_bulks = sorted({
        index[cid][0] for cid in ambiguous
        if cid in index and (_age_s(index[cid][1]) is None or _age_s(index[cid][1]) >= fresh)
    })
    res = await ctx.fx.reconcile(ctx.job_keys, ambiguous, session_id=ctx.sid, own_bulk_ids=own_bulks)
    items = []
    resolved: Set[str] = set()
    for cid, iid in (res.get("sent") or {}).items():
        items.append({"candidate_id": str(cid), "state": S.ROW_LAUNCHED, "reason": OUTCOME_RECONCILED,
                      "interview_id": str(iid)})
        resolved.add(str(cid))
    for cid, iid in (res.get("unrecorded") or {}).items():
        items.append({"candidate_id": str(cid), "state": S.ROW_LAUNCHED, "reason": OUTCOME_UNRECORDED,
                      "interview_id": str(iid)})
        resolved.add(str(cid))
    for cid in (res.get("duplicate_of") or {}):
        items.append({"candidate_id": str(cid), "state": S.ROW_SKIPPED, "reason": "duplicate_person_this_job"})
        resolved.add(str(cid))
    repost: List[str] = []
    exhausted: List[str] = []
    waiting: List[float] = []
    stale_after = cfg.repost_after_s()
    reposts: Dict[str, int] = {str(k): int(v or 0) for k, v in (s7.get("reposts") or {}).items()}
    for cid in res.get("missing") or []:
        cid = str(cid)
        if cid in resolved:
            continue
        age = _age_s(index[cid][1]) if cid in index else None
        if age is None or age >= stale_after:
            # A row we already POSTED and re-posted PAIR_LAUNCH_MAX_REPOSTS
            # times is never posted again: "missing" is judged by its echoed
            # source_candidate_id only, so an interview Pairbot filed under
            # another id would otherwise be re-created every ~10 minutes.
            if cid in index and not final and reposts.get(cid, 0) >= cfg.max_reposts():
                exhausted.append(cid)
            else:
                repost.append(cid)
        else:
            waiting.append(stale_after - age)
    if exhausted:
        items += [{"candidate_id": c, "state": S.ROW_UNCONFIRMED, "reason": "unconfirmed",
                   "error": "Pairbot never listed this candidate after a re-post"} for c in exhausted]
        resolved |= set(exhausted)
        await ctx.event("repost_limit", stage=S.STAGE_LAUNCHING, count=len(exhausted))
    if repost:
        if final:
            failed = await ctx.db(ctx.store.fail_engage_rows, ctx.job_keys, repost, ctx.sid)
            items += [{"candidate_id": c, "state": S.ROW_FAILED, "reason": "not_created", "failure_stage": "engage"}
                      for c in failed]
        else:
            requeued = await ctx.db(ctx.store.requeue_engage_rows, ctx.job_keys, repost, ctx.sid)
            items += [{"candidate_id": c, "state": S.ROW_SAVED, "reason": None} for c in requeued]
            for c in requeued:
                if c in index:  # a row never posted is posted, not RE-posted
                    reposts[c] = reposts.get(c, 0) + 1
            s7["reposts"] = reposts
            await _forget_posted(ctx, s7, set(requeued))
        resolved |= set(repost)
    await ctx.set_states(items, from_states=[S.ROW_LAUNCHING])
    remaining = [c for c in ambiguous if c not in resolved]
    out.update({"reconciled": len(res.get("sent") or {}), "reposts": len(repost), "remaining": len(remaining)})
    if not remaining:
        if s7.get("settle_since"):
            s7["settle_since"] = None
            await ctx.save_ck("s7", s7)
        return out
    since = _age_s(s7.get("settle_since")) if s7.get("settle_since") else None
    if final or (since is not None and since >= cfg.settle_max_s()):
        await ctx.set_states(
            [{"candidate_id": c, "state": S.ROW_UNCONFIRMED, "reason": "unconfirmed",
              "error": (res.get("error") or "Pairbot could not confirm this candidate")[:300]} for c in remaining],
            from_states=[S.ROW_LAUNCHING],
        )
        await ctx.event("unconfirmed", stage=S.STAGE_LAUNCHING, count=len(remaining))
        return out
    if not s7.get("settle_since"):
        s7["settle_since"] = now_iso()
        await ctx.save_ck("s7", s7)
    delay = min(waiting) if waiting else float(cfg.settle_wait_s())
    if res.get("error"):
        delay = max(delay, float(cfg.settle_wait_s()))
    delay = max(5.0, min(delay, 600.0))
    await ctx.event("waiting_for_pairbot", stage=S.STAGE_LAUNCHING, count=len(remaining), retry_in_s=int(delay))
    raise RetryLater(delay, "waiting for Pairbot")


async def _forget_posted(ctx: StageContext, s7: Dict[str, Any], ids: Set[str]) -> None:
    """Requeued rows are no longer 'posted' (the next pass posts them anew)."""
    if not ids:
        return
    posted = dict(s7.get("inflight") or {})
    for bulk_id, rec in list(posted.items()):
        keep = [c for c in (rec or {}).get("ids") or [] if c not in ids]
        if len(keep) != len((rec or {}).get("ids") or []):
            posted[bulk_id] = {**rec, "ids": keep, "requeued": sorted(set((rec or {}).get("requeued") or []) | (set(rec.get("ids") or []) & ids))}
    s7["inflight"] = posted
    await ctx.save_ck("s7", s7)


# ── planning and one launch pass ───────────────────────────────────────────

async def plan(ctx: StageContext) -> List[str]:
    """Saved rows that may be launched now (in pool order); the rest are
    moved to their final state."""
    s7 = await _s7(ctx, fresh=True)
    ids = await ctx.db(ctx.store.candidate_ids, ctx.sid, [S.ROW_SAVED])
    if not ids:
        return []
    facts = await _facts(ctx, ids)
    index = posted_index(s7.get("inflight") or {})
    items: List[Dict[str, Any]] = []
    out: List[str] = []
    for cid in ids:
        fact = facts.get(cid)
        kind = classify(fact, session_id=ctx.sid, posted=cid in index, claimed=False)
        if kind == "launched":
            items.append(_launched_item(cid, fact or {}, OUTCOME_ALREADY))
        elif kind == "blocked":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": f"blocked:{fact['block_reason']}"})
        elif kind == "in_flight":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "in_flight"})
        elif kind == "no_row":
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "not_saved",
                          "failure_stage": "engage", "error": "no saved row for this job"})
        elif kind == "ours_open":
            items.append({"candidate_id": cid, "state": S.ROW_LAUNCHING, "reason": None})
        else:
            out.append(cid)
    await ctx.set_states(items, from_states=[S.ROW_SAVED])
    return out


async def _on_batch(
    ctx: StageContext, evt: Dict[str, Any], excluded_marked: Set[str], hook_stopped: Set[Any],
) -> Dict[str, int]:
    """Re-read the batch's rows (engage state is the ground truth) and move
    each to its state; rows still possibly at Pairbot stay 'launching'.

    A failed row keeps the batch_index its SAVE batch wrote (the browser's
    progress modal and failed CSV read it as the save batch of 25); the
    Pairbot batch ``idx`` (75 per batch) goes in the 'batch' event only."""
    internal = evt.get("_internal") or {}
    idx = evt.get("index")
    status = str(internal.get("status") or evt.get("status") or "")
    # Our own stop (the bulk_id_for hook refused: ownership / lease / cancel /
    # shutdown) is resumable, never a failure of the batch.
    our_stop = idx in hook_stopped or ctx.should_stop()
    batch_failed = status != "completed" and not our_stop
    failed_ids = {str(c) for c in internal.get("failed_ids") or []}
    ids: List[str] = []
    for key in ("candidate_ids", "sent_ids", "no_interview_ids", "failed_ids", "skipped_ids", "excluded_ids"):
        for cid in internal.get(key) or []:
            if str(cid) not in ids:
                ids.append(str(cid))
    s7 = await _s7(ctx, fresh=True)
    index = posted_index(s7.get("inflight") or {})
    facts = await _facts(ctx, ids)
    sent_ids = {str(c) for c in internal.get("sent_ids") or []}
    excluded = {str(c) for c in internal.get("excluded_ids") or []}
    items: List[Dict[str, Any]] = []
    release: List[str] = []
    sent = failed = 0
    for cid in ids:
        fact = facts.get(cid)
        kind = classify(fact, session_id=ctx.sid, posted=cid in index, claimed=True)
        if kind == "launched":
            reason = OUTCOME_SENT if cid in sent_ids or cid in index else OUTCOME_ALREADY
            items.append(_launched_item(cid, fact or {}, reason))
            sent += reason == OUTCOME_SENT
        elif kind == "blocked":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": f"blocked:{(fact or {})['block_reason']}"})
        elif cid in excluded and kind != "ours_open":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "excluded"})
            excluded_marked.add(cid)
        elif kind == "failed":
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "pairbot_rejected",
                          "failure_stage": "engage",
                          "error": str(evt.get("error") or "no interview created")[:300]})
            failed += 1
        elif kind == "in_flight":
            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "in_flight"})
        elif kind == "no_row":
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "not_saved",
                          "failure_stage": "engage", "error": "no saved row for this job"})
            failed += 1
        elif kind == "launchable" and batch_failed and cid in failed_ids:
            # The batch failed before its POST (nothing recorded for it):
            # failed, claim handed back — re-planning would hit the same error.
            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "launch_failed",
                          "failure_stage": "engage",
                          "error": str(evt.get("error") or "launch batch failed")[:300]})
            release.append(cid)
            failed += 1
        elif kind == "launchable":
            # Never reached Pairbot (no bulk recorded for it): back to saved.
            items.append({"candidate_id": cid, "state": S.ROW_SAVED, "reason": None})
        # ours_open stays 'launching': settled after the pass.
    await ctx.set_states(items, from_states=[S.ROW_LAUNCHING])
    if release:
        await ctx.db(ctx.fx.release_claims, ctx.sid, ctx.job_keys, release)
    if batch_failed:
        n = int((ctx.session.get("counts") or {}).get("failed_batches") or 0) + 1
        await ctx.update(counts={"failed_batches": n})
    if sent:
        await ctx.update(coalesce_now=["first_interview_created_at"], set_now=["last_batch_done_at"])
        try:
            await ctx.db(ctx.fx.stamp_first_launch, ctx.job_id)
        except Exception as exc:  # noqa: BLE001 — timing never fails a launch
            logger.warning("launch_session first-launch stamp failed: %s", exc)
    else:
        await ctx.update(set_now=["last_batch_done_at"])
    await ctx.event(
        "batch", stage=S.STAGE_LAUNCHING, index=idx, status=evt.get("status"), sent=sent, failed=failed,
        bulk_id=internal.get("bulk_id"),
    )
    return {"sent": sent, "failed": failed}


async def launch_pass(ctx: StageContext, ids: List[str]) -> None:
    s7 = await _s7(ctx)
    attempt_no = int(s7.get("launch_attempt") or 0) + 1
    launch_id = launch_id_for(ctx.sid, attempt_no)
    s7.update({"launch_attempt": attempt_no, "launch_id": launch_id})
    await ctx.save_ck("s7", s7)
    await ctx.update(values={"launch_id": launch_id}, coalesce_now=["launch_started_at"])
    await ctx.db(
        ctx.fx.register_launch_run, launch_id, ctx.job_ref,
        requested_by=ctx.session.get("requested_by"), session_id=ctx.sid,
    )
    await ctx.assert_owner()
    duplicates: Dict[str, Any] = {}
    claimed = await ctx.db(ctx.fx.claim_for_session, ctx.job_keys, ids, ctx.sid, duplicates=duplicates)
    claimed_set = set(claimed)
    lost = [c for c in ids if c not in claimed_set]
    if lost:
        facts = await _facts(ctx, lost)
        items = []
        for cid in lost:
            if cid in duplicates:
                # Person gate: another id of this person is already held for
                # the job (launched, or in flight elsewhere) — never posted.
                items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "duplicate_person_this_job"})
                continue
            fact = facts.get(cid)
            kind = classify(fact, session_id=ctx.sid, posted=False, claimed=False)
            if kind == "launched":
                items.append(_launched_item(cid, fact or {}, OUTCOME_ALREADY))
            elif kind == "blocked":
                items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": f"blocked:{fact['block_reason']}"})
            elif kind == "no_row":
                items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "not_saved",
                              "failure_stage": "engage", "error": "no saved row for this job"})
            else:
                items.append({"candidate_id": cid, "state": S.ROW_SKIPPED,
                              "reason": "in_flight" if kind == "in_flight" else "claim_lost"})
        await ctx.set_states(items, from_states=[S.ROW_SAVED])
    if not claimed:
        return
    await ctx.set_states([{"candidate_id": c, "state": S.ROW_LAUNCHING, "reason": None} for c in claimed],
                         from_states=[S.ROW_SAVED])
    await ctx.event("launch_pass", stage=S.STAGE_LAUNCHING, attempt=attempt_no, candidates=len(claimed))

    async def record_bulk(batch_idx: Any, posted: List[str]) -> str:
        try:
            await ctx.assert_owner()  # nothing is posted unless this still holds
        except LaunchSessionStop:
            hook_stopped.add(batch_idx)
            raise
        bid = bulk_id_for(ctx.sid, attempt_no, int(batch_idx or 0))
        await ctx.set_ck_path(
            ["s7", "inflight", bid],
            {"batch": batch_idx, "attempt": attempt_no, "ids": [str(c) for c in posted], "posted_at": now_iso()},
        )
        return bid

    opts = ctx.options
    request = ctx.fx.launch_request(
        job_id=ctx.job_ref, candidate_ids=list(claimed), is_initial_launch=False, notify_recruiters=False,
        send_job_posting_email=False, app_base_url=str(ctx.input.get("app_base_url") or ""),
        batch_size=int(opts.get("batch_size") or cfg.pairbot_batch_size()), launch_session_id=ctx.sid,
    )
    outreach_stopped: Optional[str] = None
    stop_error: Optional[BaseException] = None
    done: Optional[Dict[str, Any]] = None
    excluded_marked: Set[str] = set()
    hook_stopped: Set[Any] = set()
    async with contextlib.aclosing(ctx.fx.run_launch(
        request, launch_id=launch_id, run_id=ctx.sid, fire_side_effects=False, should_stop=ctx.should_stop,
        provision_inline=False, claimed_session_id=ctx.sid, bulk_id_for=record_bulk,
        recruiter_email=(ctx.session.get("requested_by") or None),
    )) as events:
        async for evt in events:
            kind = evt.get("type")
            if kind == "batch":
                if stop_error is not None:
                    continue  # lost the session: let in-flight batches finish, write nothing
                try:
                    await _on_batch(ctx, evt, excluded_marked, hook_stopped)
                except LaunchSessionStop as exc:
                    stop_error = exc
                except Exception as exc:  # noqa: BLE001 — the next settle re-derives rows
                    logger.warning("launch_session batch bookkeeping failed session=%s: %s", ctx.sid, exc)
            elif kind == "error" and int(evt.get("status_code") or 0) == 409:
                outreach_stopped = str(evt.get("message") or "Job activity has been stopped.")
            elif kind == "done":
                done = evt
    if stop_error is not None:
        raise stop_error
    if done is not None:
        await _refine_exclusions(ctx, done, excluded_marked)
    if outreach_stopped is not None:
        raise OutreachStopped(outreach_stopped)
    if done is not None and (done.get("_internal") or {}).get("stopped"):
        ctx.lease.check()
        raise LeaseExpired(ctx.sid)


async def _refine_exclusions(ctx: StageContext, done: Dict[str, Any], marked: Set[str]) -> None:
    """The done event carries each exclusion's reason text."""
    items = []
    for rec in done.get("excluded_candidates") or []:
        if isinstance(rec, dict) and str(rec.get("candidate_id") or "") in marked:
            items.append({"candidate_id": str(rec["candidate_id"]), "state": S.ROW_SKIPPED,
                          "reason": f"excluded:{str(rec.get('reason') or '')[:120]}"})
    if items:
        await ctx.set_states(items, from_states=[S.ROW_SKIPPED])


async def run(ctx: StageContext) -> StageResult:
    from services.launch_sessions.stages import finishing

    await _s7(ctx)
    first: Optional[List[str]] = None
    for _ in range(_MAX_PASSES_PER_CLAIM):
        await ctx.boundary()
        await settle(ctx)
        ids = await plan(ctx)
        if not ids:
            break
        if first is None:
            first = list(ids)
        await launch_pass(ctx, ids)
    else:
        await settle(ctx)
        left = await plan(ctx)
        if left:
            if first is not None and set(left) >= set(first):
                # Three passes and not one row moved on: a failure, counted
                # (bounded by PAIR_LAUNCH_MAX_ATTEMPTS), never an endless wait.
                raise RuntimeError(f"launch made no progress in {_MAX_PASSES_PER_CLAIM} passes")
            raise RetryLater(float(cfg.settle_wait_s()), "more launch passes needed")
    counts = await ctx.refresh_counts()
    states = counts["states"]
    await ctx.update(counts={
        "launched": states.get(S.ROW_LAUNCHED, 0), "failed": states.get(S.ROW_FAILED, 0),
        "unconfirmed": states.get(S.ROW_UNCONFIRMED, 0),
    })
    # The launch is over: hand the claims back now (a launch started while
    # this session provisions must not see its leftovers as in flight), then
    # the recruiter email (+ applicant sync, job posting) — not after the
    # JobDiva provisioning that follows.
    handed = await hand_back_claims(ctx)
    await ctx.update(counts={"claims": handed})
    await finishing.side_effects_once(ctx, full=True)
    return StageResult(next_stage=S.STAGE_PROVISIONING)


async def hand_back_claims(ctx: StageContext) -> Dict[str, int]:
    """When the session finishes (any status): release this session's claims
    on rows it never posted, park the rows that may be at Pairbot (see the
    module docstring). Raises on a DB error — the finish is then retried, so
    a claim is never leaked behind a terminal status."""
    await ctx.reload()  # the posted records are written by the core's hook
    if "s7" not in ctx.ck:
        return {"released": 0, "parked": 0}  # S7 never claimed anything
    index = posted_index((ctx.ck.get("s7") or {}).get("inflight") or {})
    rows = await ctx.db(ctx.store.candidate_states, ctx.sid)
    # Rows a failed reconcile-only pass left 'launching': posted → unconfirmed
    # (the re-check settles them), never posted → saved.
    leftover = [
        {"candidate_id": r["candidate_id"], "state": S.ROW_UNCONFIRMED, "reason": "unconfirmed",
         "error": "not settled when the session finished"} if str(r["candidate_id"]) in index
        else {"candidate_id": r["candidate_id"], "state": S.ROW_SAVED, "reason": None}
        for r in rows if r["state"] == S.ROW_LAUNCHING
    ]
    if leftover:
        await ctx.set_states(leftover, from_states=[S.ROW_LAUNCHING])
        rows = await ctx.db(ctx.store.candidate_states, ctx.sid)
    hold: Dict[str, str] = {}
    release: List[str] = []
    for r in rows:
        cid = str(r["candidate_id"])
        # A row recorded as posted, or settled as possibly at Pairbot. (A
        # 'launching' row with no posted record never left the process.)
        unsettled = r["state"] == S.ROW_UNCONFIRMED or (
            r["state"] == S.ROW_LAUNCHED and r.get("reason") == OUTCOME_UNRECORDED
        )
        if cid in index or unsettled:
            hold[cid] = index[cid][0] if cid in index else ""
        else:
            release.append(cid)
    released = parked = 0
    if release:
        released = int(await ctx.db(ctx.fx.release_claims, ctx.sid, ctx.job_keys, release) or 0)
    if hold:
        parked = len(await ctx.db(ctx.store.park_posted_rows, ctx.job_keys, hold, ctx.sid))
    return {"released": released, "parked": parked}
