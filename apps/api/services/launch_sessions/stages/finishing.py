"""S8 — done, and the shared "finish a session" path (completed / cancelled /
failed).

Side effects fire ONCE per session and only when it actually launched
someone (rows launched by THIS session: sent / reconciled / unrecorded):
the guard is ``launch_sessions.side_effects_fired_at`` (a conditional UPDATE,
claimed before spawning; a spawn error resets it so a later finish can try
again). A completed launch fires them at the end of S7 (``side_effects_once``
— the launch is over; JobDiva provisioning follows in the background, as
legacy's inline provisioning did): the recruiter launch email + applicant
sync (initial launch) + the job-posting email only when the launch ran clean
— no failed launch batch and no unconfirmed row, as /engage/launch decides
it (a per-person "no interview" answer is not a failed batch) — and it was
requested. Cancelled / failed with ≥1 launched: the recruiter email and the
applicant sync (initial launch), never the job posting — what /engage/launch
fires on a 409 abort. A pass that launched nobody never uses the guard, so a
later child session can still send its own email.

Then: this session's claims are handed back (launching.hand_back_claims:
never-posted claims released, possibly-posted rows parked 'processing' — an
error here fails the finish, which is retried, so a claim is never leaked),
the counts finalized, ``launch_timing.record_timing_event`` sent (with the
job's T2 so a relaunch is flagged), and the terminal status written
(fenced). Sessions left with unconfirmed / unrecorded rows get a
housekeeping re-check.

Counts written here: ``sent`` (this session's launches), ``already_launched``,
``launched``, ``failed``, ``unconfirmed``, ``needs_contact``,
``no_interview`` (Pairbot created no interview for the person),
``failed_batches`` (S7), ``rows_in_saved`` (rows left saved; ``saved`` /
``saved_total`` keep S6's total) and ``side_effects`` (the outcome).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.launch_sessions import config as cfg
from services.launch_sessions import events as ev
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.stages import launching

logger = logging.getLogger(__name__)

SESSION_SENT_REASONS = ("sent", "reconciled", "unrecorded")
# Outcomes that end the once-per-session question for good.
_SETTLED_OUTCOMES = ("fired", "already_fired", "not_requested")


async def final_counts(ctx: StageContext) -> Dict[str, Any]:
    folded = await ctx.db(ctx.store.state_counts, ctx.sid)
    states, reasons = folded["states"], folded["reasons"]
    launched_reasons = reasons.get(S.ROW_LAUNCHED, {})
    sent = sum(int(launched_reasons.get(r, 0)) for r in SESSION_SENT_REASONS)
    return {
        "states": states, "reasons": reasons,
        "sent": sent,
        "already_launched": int(launched_reasons.get("already_launched", 0)),
        "launched": int(states.get(S.ROW_LAUNCHED, 0)),
        "failed": int(states.get(S.ROW_FAILED, 0)),
        "no_interview": int(reasons.get(S.ROW_FAILED, {}).get("pairbot_rejected", 0)),
        "unconfirmed": int(states.get(S.ROW_UNCONFIRMED, 0)),
        "needs_contact": int(states.get(S.ROW_NEEDS_CONTACT, 0)),
        "rows_in_saved": int(states.get(S.ROW_SAVED, 0)),
        "unrecorded": int(launched_reasons.get("unrecorded", 0)),
        "failed_batches": int((ctx.session.get("counts") or {}).get("failed_batches") or 0),
    }


def launch_was_clean(counts: Dict[str, Any]) -> bool:
    """/engage/launch's rule for the job-posting email: no failed batch (and
    nothing left unconfirmed)."""
    return int(counts.get("failed_batches") or 0) == 0 and int(counts.get("unconfirmed") or 0) == 0


async def fire_side_effects(ctx: StageContext, *, sent: int, full: bool, clean: bool) -> str:
    """'fired' | 'not_requested' | 'nothing_sent' | 'already_fired' | 'failed'."""
    if sent <= 0:
        return "nothing_sent"
    opts = ctx.options
    initial = bool(opts.get("is_initial_launch"))
    notify = bool(opts.get("notify_recruiters"))
    if not (initial or notify):
        return "not_requested"
    if not await ctx.db(ctx.store.claim_side_effects, ctx.fence):
        return "already_fired"
    wanted = opts.get("send_job_posting_email")
    job_posting = full and (initial if wanted is None else bool(wanted)) and clean
    kwargs = dict(
        notify_recruiters=notify, send_job_posting_email=job_posting, is_initial_launch=initial,
        # The fire-once ledger's "this launch" boundary: the session's birth
        # (its click). Only a first interview created since then is ours.
        launch_started_at=ctx.session.get("created_at"),
    )
    try:
        ok = ctx.fx.fire_side_effects(
            ctx.job_ref, sent_count=sent, app_base_url=str(ctx.input.get("app_base_url") or ""), **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("launch_session side effects failed session=%s: %s", ctx.sid, exc)
        ok = False
    if not ok:
        try:
            await ctx.db(ctx.store.reset_side_effects, ctx.sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("launch_session side-effects guard reset failed session=%s: %s", ctx.sid, exc)
        return "failed"
    return "fired"


async def side_effects_once(ctx: StageContext, *, full: bool) -> str:
    """Fire the session's side effects unless an earlier call settled them
    (recorded in checkpoint.fx); records the outcome."""
    prior = (ctx.ck.get("fx") or {}).get("outcome")
    if prior in _SETTLED_OUTCOMES:
        return str(prior)
    counts = await final_counts(ctx)
    outcome = await fire_side_effects(ctx, sent=counts["sent"], full=full, clean=launch_was_clean(counts))
    await ctx.update(checkpoint={"fx": {"outcome": outcome, "full": full}}, counts={"side_effects": outcome})
    return outcome


async def finish(ctx: StageContext, *, status: str, error: Optional[str], full_side_effects: bool) -> Dict[str, Any]:
    """Write the terminal status (see the module docstring). Returns the row."""
    prior = (ctx.session.get("counts") or {}).get("claims") or {}
    handed = await launching.hand_back_claims(ctx)
    effects = await side_effects_once(ctx, full=full_side_effects)
    counts = await final_counts(ctx)
    counts["side_effects"] = effects
    counts["claims"] = {k: int(prior.get(k) or 0) + int(v) for k, v in handed.items()}
    needs_recheck = counts["unconfirmed"] > 0 or counts["unrecorded"] > 0
    row = await ctx.db(
        ctx.store.finish, ctx.fence, status=status, error=error, counts=counts,
        recheck_in_s=cfg.recheck_every_s() if needs_recheck else None,
    )
    ctx.session = row
    try:
        first_launch_at = await ctx.db(ctx.store.job_first_launch_at, ctx.job_id)
    except Exception:  # noqa: BLE001
        first_launch_at = None
    try:
        ctx.fx.record_timing_event(row, job_first_launch_at=first_launch_at)
    except Exception as exc:  # noqa: BLE001
        logger.debug("launch_session timing event failed: %s", exc)
    ev.nr_session_event(row, stage=str(row.get("stage")), status=status, outcome=effects)
    try:
        await ctx.db(ctx.store.append_event, ctx.sid, ev.payload(
            status, stage=row.get("stage"), sent=counts["sent"], failed=counts["failed"],
            needs_contact=counts["needs_contact"], unconfirmed=counts["unconfirmed"], error=error,
        ))
    except Exception:  # noqa: BLE001
        pass
    return row


async def run(ctx: StageContext) -> StageResult:
    await finish(ctx, status=S.STATUS_COMPLETED, error=None, full_side_effects=True)
    return StageResult(done=True)
