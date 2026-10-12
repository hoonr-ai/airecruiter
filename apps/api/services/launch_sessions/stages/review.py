"""S4 — review (only when input.require_review: the QA Override).

Every selected, not hard-filter-skipped row goes to the recruiter with what
the lookup found (GET …/candidates?state=selected; ``current_phone`` /
``current_email`` = override || the row's own). The session is handed over:
status awaiting_input, no owner, no lease — nothing runs and no worker slot
is held while a human decides. POST /confirm stores the confirmed contacts as
FINAL (they replace both channels; empty = none, never a fallback — QA must
never dial a real candidate it overrode), skips the rows not confirmed and
re-queues the session at this stage; the next claim moves on to splitting.
Unconfirmed for PAIR_LAUNCH_REVIEW_TTL_H → housekeeping cancels it.
"""

from __future__ import annotations

from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult


async def run(ctx: StageContext) -> StageResult:
    review = ctx.ck.get("review") or {}
    if review.get("confirmed") or not ctx.input.get("require_review"):
        return StageResult(next_stage=S.STAGE_SPLITTING)
    cands = await ctx.rows(states=[S.ROW_SELECTED])
    rows = [c["row"] for c in cands]
    hard = rules.compute_hard_filter_skips(rows, {c["candidate_id"] for c in cands})
    if hard["ids"]:
        await ctx.set_states(
            [{"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "hard_filter"} for cid in hard["ids"]],
            from_states=[S.ROW_SELECTED],
        )
    count = len(cands) - len(hard["ids"])
    if count <= 0:
        # Nothing to review: the browser says "No candidates available to launch."
        return StageResult(next_stage=S.STAGE_SPLITTING)
    await ctx.event("review_required", stage=S.STAGE_REVIEW, count=count)
    return StageResult(wait_input=True, checkpoint={"review": {"requested": True, "count": count}})
