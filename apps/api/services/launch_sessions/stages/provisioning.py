"""S7b — provisioning: the JobDiva applications of this session's launches.

``routers.engagement.provision_sent_candidates`` (awaited, claim-stamped per
row, so two runs can never both create a person's profile — PR #615) in
chunks of PAIR_LAUNCH_PROVISION_CHUNK, ``assert_owner`` before each chunk;
``checkpoint.s7b.next`` is the resume point. A chunk that raises (a claim DB
error) is retried PAIR_LAUNCH_PROVISION_TRIES times, then counted
(``counts.provision.failed`` / ``errors``) and skipped: provisioning never
fails a session. JOBDIVA_PROVISIONING_ENABLED=false skips it (local
end-to-end runs must never write JobDiva).

The session no longer holds its job here (S.LAUNCH_COMPLETE_STAGES): the
launch is over and its side effects fired at the end of S7, so a new launch
or a needs-contact child may start while this runs.

A session that ends cancelled / failed after launching people runs the same
loop from its finalize path (``provision_launched(ctx, finalizing=True)``),
and the housekeeping re-check provisions rows it proves launched later
(runner.recheck_unconfirmed) — legacy provisioned inline per batch, so no
launched candidate may be left without its JobDiva application.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from services.launch_sessions import config as cfg
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.errors import LaunchSessionStop

logger = logging.getLogger(__name__)

_KEYS = ("requested", "claimed", "not_claimed", "success", "skipped", "failed",
         "duplicate_suspected", "blank_profile_refused", "errors")


async def _provision_chunk(ctx: StageContext, part: list) -> Dict[str, Any]:
    tries = cfg.provision_tries()
    for attempt in range(tries):
        try:
            return await ctx.fx.provision(ctx.job_ref, part) or {}
        except (asyncio.CancelledError, LaunchSessionStop):
            raise
        except Exception as exc:  # noqa: BLE001 — a claim DB error: retry, then count it
            logger.warning("launch_session provisioning chunk failed session=%s try=%d/%d: %s",
                           ctx.sid, attempt + 1, tries, exc)
            if attempt < tries - 1:
                await asyncio.sleep(min(8.0, 2.0 * (2 ** attempt)))
    return {"requested": len(part), "failed": len(part), "errors": 1}


async def provision_launched(ctx: StageContext, *, finalizing: bool = False) -> Dict[str, int]:
    """Provision every launched row from ``checkpoint.s7b.next`` on; marks
    ``s7b.done``. ``finalizing``: a cancelled session's finalize path (the
    pending cancel does not stop it)."""
    s7b: Dict[str, Any] = dict(ctx.ck.get("s7b") or {})
    totals: Dict[str, int] = {k: int((s7b.get("totals") or {}).get(k, 0)) for k in _KEYS}
    if s7b.get("done"):
        return totals
    ids = await ctx.db(ctx.store.candidate_ids, ctx.sid, [S.ROW_LAUNCHED])
    if not ctx.fx.provisioning_enabled():
        await ctx.update(checkpoint={"s7b": {"done": True, "disabled": True}},
                         counts={"provision": {"disabled": True, "requested": len(ids)}})
        return totals
    start = int(s7b.get("next") or 0)
    chunk = cfg.provision_chunk()
    for i in range(start, len(ids), chunk):
        await ctx.assert_owner(allow_cancel=finalizing)
        part = ids[i:i + chunk]
        res = await _provision_chunk(ctx, part)
        for k in _KEYS:
            totals[k] += int(res.get(k, 0) or 0)
        await ctx.save_ck("s7b", {"next": i + chunk, "totals": totals}, counts={"provision": totals})
    await ctx.update(checkpoint={"s7b": {"done": True, "next": len(ids), "totals": totals}},
                     counts={"provision": totals})
    return totals


async def run(ctx: StageContext) -> StageResult:
    await provision_launched(ctx)
    return StageResult(next_stage=S.STAGE_DONE)
