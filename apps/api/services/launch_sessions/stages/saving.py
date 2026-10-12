"""S6 — saving: the ready rows through /candidates/save, in batches.

The payload is the browser's (rules.build_launch_save_payload: overrides
replace the row's contact, then selectSaveCandidates — no exclusion, not
no_contact, not a hiring-client conflict, phone not on the DNC list — then
buildSaveCandidatePayload, match_score lock included), and the call is
``routers.candidates.save_candidates`` itself with the requester's identity,
so every gate runs: job access, outreach stopped (409), the recruiter-email
gate, launched-by attribution, pair_launched_at, DNC and no-contact skips.

Per batch of PAIR_LAUNCH_SAVE_BATCH (assert_owner first): ``saved_ids`` →
saved; ``dnc_skipped`` / ``no_contact_skipped`` → skipped; anything else →
failed (failure_stage save). Rows still ``ready`` are the checkpoint: a
resumed stage saves only those (a re-save is an idempotent upsert anyway).

Errors: 409 → OutreachStopped (session cancelled); 403 → AccessDenied
(retried, then failed); another 4xx — or a payload the request model refuses
(pydantic ValidationError / TypeError: what the browser gets as a 422) —
→ that batch's rows failed, no retry, the next batch goes on (only the
error's type is stored: a validation message quotes the row's values);
429 / 5xx / network → PAIR_LAUNCH_SAVE_RETRIES tries with back-off, then the
stage is retried later.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from pydantic import ValidationError

from services.launch_sessions import config as cfg
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.contacts import effective_row, overrides_for
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.errors import AccessDenied, OutreachStopped

logger = logging.getLogger(__name__)


def save_skip_reason(row: Dict[str, Any], job: Any, dnc_phones: Any) -> str:
    """Why rules.select_save_candidates left a row out (same order)."""
    exclusion = rules.get_candidate_exclusion_reason(row)
    if exclusion:
        return f"exclusion:{exclusion}"
    if row.get("no_contact") is True:
        return "no_contact"
    if (rules.with_client_conflict_flag(row, job) or {}).get("client_conflict") is True:
        return "client_conflict"
    if dnc_phones:
        np = rules.normalize_dnc_phone(row.get("phone"))
        if np and np in dnc_phones:
            return "dnc"
    return "not_saved"


async def _save(ctx: StageContext, payload: List[Dict[str, Any]], user: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(response, None) or (None, error) for a non-retryable 4xx. Raises
    OutreachStopped / AccessDenied, or the last transient error."""
    tries = cfg.save_retries()
    last: Optional[BaseException] = None
    for attempt in range(tries):
        try:
            return await ctx.fx.save(ctx.job_ref, payload, user), None
        except HTTPException as exc:
            code = int(getattr(exc, "status_code", 500) or 500)
            if code == 409:
                raise OutreachStopped(str(exc.detail))
            if code == 403:
                raise AccessDenied(str(exc.detail))
            if 400 <= code < 500 and code != 429:
                return None, str(exc.detail)[:300]
            last = exc
        except (ValidationError, TypeError) as exc:
            # The request model refused a row (the browser's 422): permanent.
            return None, f"invalid save payload ({type(exc).__name__})"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — network / DB: retry
            last = exc
        if attempt < tries - 1:
            await asyncio.sleep(min(8.0, 1.0 * (2 ** attempt)))
            await ctx.assert_owner()
    raise RuntimeError(f"save failed after {tries} tries: {last}")


async def run(ctx: StageContext) -> StageResult:
    cands = await ctx.rows(states=[S.ROW_READY])
    if not ctx.session.get("save_started_at"):
        await ctx.update(coalesce_now=["save_started_at"])
    if cands:
        job = await ctx.db(ctx.store.resolve_job, ctx.job_id)
        dnc_phones = await ctx.db(ctx.fx.dnc_phones)
        user = ctx.memo.get("user")
        if user is None:
            user = await ctx.db(ctx.fx.resolve_user, str(ctx.session.get("requested_by") or ""))
            ctx.memo["user"] = user
        size = cfg.save_batch_size()
        offset = int((ctx.ck.get("s6") or {}).get("batches") or 0)
        for n, i in enumerate(range(0, len(cands), size)):
            await ctx.boundary()
            batch = cands[i:i + size]
            batch_index = offset + n
            ids = [c["candidate_id"] for c in batch]
            effective = [effective_row(c) for c in batch]
            overrides = overrides_for(batch)
            payload = rules.build_launch_save_payload(
                effective, launch_ids=set(ids), overrides=overrides, job=job, dnc_phones=dnc_phones,
            )
            in_payload = {str(p["candidate_id"]) for p in payload}
            applied = {rules.candidate_row_id(r): r for r in rules.apply_contact_overrides(effective, overrides)}
            items: List[Dict[str, Any]] = [
                {"candidate_id": cid, "state": S.ROW_SKIPPED,
                 "reason": save_skip_reason(applied.get(cid) or {}, job, dnc_phones)}
                for cid in ids if cid not in in_payload
            ]
            if payload:
                await ctx.assert_owner()
                resp, error = await _save(ctx, payload, user)
                if resp is None:
                    items += [
                        {"candidate_id": cid, "state": S.ROW_FAILED, "reason": "save_rejected",
                         "failure_stage": "save", "batch_index": batch_index, "error": error}
                        for cid in in_payload
                    ]
                else:
                    saved = {str(x) for x in resp.get("saved_ids") or []}
                    dnc = {str(x.get("candidate_id")) for x in resp.get("dnc_skipped") or [] if isinstance(x, dict)}
                    nc = {str(x.get("candidate_id")) for x in resp.get("no_contact_skipped") or [] if isinstance(x, dict)}
                    for cid in in_payload:
                        if cid in saved:
                            items.append({"candidate_id": cid, "state": S.ROW_SAVED, "reason": None,
                                          "batch_index": batch_index})
                        elif cid in dnc:
                            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "dnc"})
                        elif cid in nc:
                            items.append({"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "no_contact"})
                        else:
                            items.append({"candidate_id": cid, "state": S.ROW_FAILED, "reason": "not_saved",
                                          "failure_stage": "save", "batch_index": batch_index,
                                          "error": "not saved"})
            await ctx.set_states(items, from_states=[S.ROW_READY])
            await ctx.save_ck("s6", {"batches": batch_index + 1})
            await ctx.event(
                "save_batch", stage=S.STAGE_SAVING, index=batch_index,
                saved=sum(1 for it in items if it["state"] == S.ROW_SAVED),
                failed=sum(1 for it in items if it["state"] == S.ROW_FAILED),
                skipped=sum(1 for it in items if it["state"] == S.ROW_SKIPPED),
            )
    counts = await ctx.refresh_counts()
    saved_total = counts["states"].get(S.ROW_SAVED, 0)
    await ctx.update(coalesce_now=["save_done_at"], counts={"saved": saved_total, "saved_total": saved_total})
    return StageResult(next_stage=S.STAGE_LAUNCHING)
