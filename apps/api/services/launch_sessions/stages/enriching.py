"""S3 — enriching: Launch PAIR's contact lookup, server-side.

Who: ``rules.select_candidates_missing_contact`` over the selected rows (not
hard-filter skipped, missing a valid phone OR a valid email); a row with no
LinkedIn URL (``rules.resolve_linkedin_url``: 7 fields + the résumé) is not
looked up — "already reachable" when it has a phone or an email, else
"missing LinkedIn". How: ``routers.candidates.enrich_contacts_for_launch``
(the grouped endpoint's internals: same chain, same spend policy via
trigger="launch", the browser's exact request fields) in groups of
LAUNCH_ENRICH_GROUP_SIZE with PAIR_LAUNCH_ENRICH_CONCURRENCY lookups in
flight (the browser: 4 groups of 5). Each group is a paid unit: fenced by
``assert_owner`` before it starts. Each answer is read by
``rules.interpret_enrich_answer`` and stored on the row as it arrives
(``contact.done`` = per-row checkpoint, so a resume never pays twice for a
row); a found contact is an override that REPLACES the row's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import Any, Dict, List

from services import exa_telemetry
from services.launch_sessions import config as cfg
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.contacts import is_kipplo_issue
from services.launch_sessions.context import StageContext, StageResult

logger = logging.getLogger(__name__)


def _next(ctx: StageContext) -> str:
    return S.STAGE_REVIEW if ctx.input.get("require_review") else S.STAGE_SPLITTING


def summarize(cands: List[Dict[str, Any]], total: int) -> Dict[str, Any]:
    """counts.enrich from the rows' stored lookup outcomes (deterministic, so
    a resumed stage reports the same numbers)."""
    out: Dict[str, Any] = {
        "total": total, "looked_up": 0, "succeeded": 0, "already_reachable": 0, "missing_linkedin": 0,
        "no_contact": 0, "not_eligible": 0, "failed": 0,
        "phone_found_by": {}, "email_found_by": {}, "kipplo_issues": {},
    }
    for c in cands:
        contact = c.get("contact") or {}
        outcome = contact.get("outcome")
        if not contact.get("done") or not outcome:
            continue
        if contact.get("looked_up"):
            out["looked_up"] += 1
        if outcome in ("succeeded", "already_reachable", "missing_linkedin", "failed"):
            out[outcome] += 1
        elif outcome in ("no_contact", "not_eligible"):
            out["no_contact"] += 1
            if outcome == "not_eligible":
                out["not_eligible"] += 1
        if outcome != "failed" and is_kipplo_issue(contact.get("kipplo")):
            key = str(contact.get("kipplo"))
            out["kipplo_issues"][key] = out["kipplo_issues"].get(key, 0) + 1
        for field, bucket in (("phone_provider", "phone_found_by"), ("email_provider", "email_found_by")):
            if outcome == "succeeded" and contact.get(field):
                by = str(contact[field])
                out[bucket][by] = out[bucket].get(by, 0) + 1
    return out


def answer_contact(answer: Dict[str, Any], launchable_now: bool) -> Dict[str, Any]:
    """The row's contact record for one lookup answer (browser rules)."""
    read = rules.interpret_enrich_answer(answer.get("result") if isinstance(answer, dict) else None, launchable_now)
    contact: Dict[str, Any] = {
        "done": True, "outcome": read["outcome"], "looked_up": bool(read.get("lookedUp")),
        "kipplo": read.get("kipplo") or "",
    }
    if read.get("override"):
        contact["override"] = read["override"]
    if read.get("phoneProvider") is not None:
        contact["phone_provider"] = read["phoneProvider"]
    if read.get("emailProvider") is not None:
        contact["email_provider"] = read["emailProvider"]
    return contact


async def run(ctx: StageContext) -> StageResult:
    if ctx.input.get("skip_enrichment"):
        await ctx.update(coalesce_now=["enrich_done_at"])
        return StageResult(next_stage=_next(ctx))
    cands = await ctx.rows(states=[S.ROW_SELECTED])
    by_id = {c["candidate_id"]: c for c in cands}
    rows = [c["row"] for c in cands]
    selected_ids = set(by_id)
    hard = rules.compute_hard_filter_skips(rows, selected_ids)
    missing = rules.select_candidates_missing_contact(rows, selected_ids, set(hard["ids"]))

    decided: List[Dict[str, Any]] = []
    to_lookup: List[Dict[str, Any]] = []
    for row in missing:
        cid = rules.candidate_row_id(row)
        cand = by_id.get(cid)
        if not cid or cand is None or (cand.get("contact") or {}).get("done"):
            continue
        linkedin = rules.resolve_linkedin_url(row)
        launchable = rules.is_launchable_now(row)
        if not linkedin:
            decided.append({
                "candidate_id": cid,
                "contact": {"done": True, "outcome": "already_reachable" if launchable else "missing_linkedin",
                            "looked_up": False, "kipplo": ""},
            })
            continue
        to_lookup.append({"cid": cid, "row": row, "linkedin": linkedin, "launchable": launchable})
    await ctx.set_states(decided)
    if to_lookup:
        await ctx.event("enrich_started", stage=S.STAGE_ENRICHING, lookups=len(to_lookup))

    group_size = rules.LAUNCH_ENRICH_GROUP_SIZE
    groups = [to_lookup[i:i + group_size] for i in range(0, len(to_lookup), group_size)]
    lanes = max(1, math.ceil(cfg.enrich_concurrency() / group_size))
    cursor = {"next": 0}

    async def lookup_group(group: List[Dict[str, Any]]) -> None:
        await ctx.assert_owner()  # a paid unit: never start one we may not own
        items = [
            {"candidate_id": g["cid"], "source": g["row"].get("source"), "linkedin_url": g["linkedin"],
             "match_score": g["row"].get("match_score")}
            for g in group
        ]
        answered: set = set()
        # ExaCall attribution; the lookups' tasks copy this context.
        with exa_telemetry.scope(trigger="launch_s3", launch_session_id=ctx.sid):
            async with contextlib.aclosing(
                ctx.fx.enrich(items, job_id=ctx.job_ref, concurrency=len(items))
            ) as stream:
                async for answer in stream:
                    idx = answer.get("index") if isinstance(answer, dict) else None
                    if not isinstance(idx, int) or idx < 0 or idx >= len(group) or idx in answered:
                        continue
                    answered.add(idx)
                    g = group[idx]
                    await ctx.set_states([{"candidate_id": g["cid"], "contact": answer_contact(answer, g["launchable"])}])
        # A lookup that never answered reads as failed (the browser's null).
        missing_answers = [
            {"candidate_id": g["cid"], "contact": answer_contact({"result": None}, g["launchable"])}
            for i, g in enumerate(group) if i not in answered
        ]
        await ctx.set_states(missing_answers)

    async def lane() -> None:
        while cursor["next"] < len(groups):
            group = groups[cursor["next"]]
            cursor["next"] += 1
            await lookup_group(group)

    tasks = [asyncio.ensure_future(lane()) for _ in range(min(lanes, len(groups)))]
    try:
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    cands = await ctx.rows(states=[S.ROW_SELECTED])
    summary = summarize(cands, len(missing))
    await ctx.update(
        coalesce_now=["enrich_done_at"], checkpoint={"s3": {"done": True}}, counts={"enrich": summary},
    )
    await ctx.event(
        "enrich_done", stage=S.STAGE_ENRICHING, succeeded=summary["succeeded"], failed=summary["failed"],
        no_contact=summary["no_contact"],
    )
    return StageResult(next_stage=_next(ctx))
