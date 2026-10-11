"""S2 — selecting: who this session will try to launch.

Pool (parity, docs/launch-sessions.md "Selection pool"):
  full_search  ``StreamRun.selection_pool()`` — the de-duplicated table, or
               every attempt's rows when the table is shorter — exactly what
               the browser's Source & Launch selects from; every later stage
               runs on TABLE rows whose id was selected (a selected id that
               was merged away launches nobody). Identity duplicates the
               merge absorbed are recorded ``identity_duplicate:<survivor>``.
  provided     the uploaded rows, de-duplicated the same way
               (``deduplicate_candidates_ui``).

Rule: ``rules.compute_auto_launch_selection`` with the session's cap
(``clamp_launch_count``; 250 by default for a full search and "launch
remaining"), floor (``_floor``: input.min_score, default 60, for a full
search and "launch remaining"; none for QA quick launch and child sessions),
the server's "already launched" sets and blocked reasons, and
``with_reasons`` — every row not selected gets its reason.

"Already launched" is server truth for THIS job (both job keys), never mere
row existence (SPEC-AMENDMENTS):
  launched          an interview id: an audit row, or data.engage_interview_id;
                    a launched row's data.jobdiva_candidate_id (the profile
                    PAIR created for it) counts as launched too (twins)
  in_flight         engage_status queued / processing / sending (and its twin)
  blocked:<reason>  data.engage_block_reason (a Pairbot duplicate: never re-post)
  applicant_backlog never engaged ('' status) JobDiva applicant rows — the
                    auto-sync backlog stays blocked (applicant-backlog policy)
  re-selectable     engage_status 'failed'; saved-but-never-engaged rows of
                    any other source (an orphan save)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Tuple

from services.engage_status import LAUNCH_IN_FLIGHT_STATUSES
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.context import StageContext, StageResult
from services.launch_sessions.stages import sourcing

logger = logging.getLogger(__name__)


def launch_predicate(rows: Iterable[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(launched sets ``{"keys", "ids"}``, ``{id: blocked reason}``) from
    store.job_launch_state rows (one per sourced row / audit-only interview)."""
    by_cid: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        cid = str(r.get("candidate_id") or "").strip()
        if cid:
            by_cid.setdefault(cid, []).append(r)
    launched_items: List[Dict[str, Any]] = []
    blocked: Dict[str, str] = {}
    for cid, group in by_cid.items():
        if any(str(r.get("interview_id") or "").strip() or r.get("audit_launched") for r in group):
            for r in group:
                launched_items.append({
                    "candidate_id": cid, "source": r.get("source"),
                    "jobdiva_candidate_id": str(r.get("jobdiva_candidate_id") or "").strip() or None,
                })
            continue
        block = next((str(r["block_reason"]) for r in group if r.get("block_reason")), "")
        if block:
            blocked[cid] = f"blocked:{block}"
            continue
        statuses = {str(r.get("engage_status") or "").strip().lower() for r in group}
        if statuses & set(LAUNCH_IN_FLIGHT_STATUSES):
            blocked[cid] = "in_flight"
            for r in group:
                twin = str(r.get("jobdiva_candidate_id") or "").strip()
                if twin and twin not in blocked:
                    blocked[twin] = "in_flight"
            continue
        if statuses <= {""} and any("applicant" in str(r.get("source") or "").lower() for r in group):
            blocked[cid] = "applicant_backlog"
    return rules.launched_sets_from_response(launched_items), blocked


def _numeric(raw: Any) -> bool:
    return isinstance(raw, (int, float)) and not isinstance(raw, bool)


def _floor(ctx: StageContext) -> Any:
    """The score floor (no outreach below 60):

      full_search       input.min_score, default LAUNCH_MIN_SCORE
      launch_remaining  the same — the browser's "launch remaining" applies
                        the 60% floor to the rows on screen
      qa_quick / needs_contact / retry
                        none (the recruiter picked these rows by hand / they
                        were selected by the parent session already)
      other provided    input.min_score when the request gives one, else none
    """
    raw = ctx.input.get("min_score")
    trigger = ctx.session.get("trigger")
    if ctx.session.get("mode") == S.MODE_FULL_SEARCH or trigger == "launch_remaining":
        return raw if _numeric(raw) else rules.LAUNCH_MIN_SCORE
    if trigger == "qa_quick" or trigger in S.CHILD_TRIGGERS:
        return None
    return raw if _numeric(raw) else None


def _limit(ctx: StageContext, pool_size: int) -> int:
    raw = ctx.input.get("launch_count")
    if raw is None:
        default_cap = (
            ctx.session.get("mode") == S.MODE_FULL_SEARCH or ctx.session.get("trigger") == "launch_remaining"
        )
        raw = rules.AUTO_LAUNCH_DEFAULT_COUNT if default_cap else max(1, pool_size)
    return rules.clamp_launch_count(raw)


async def _pool(ctx: StageContext) -> Tuple[List[Any], List[Any], Dict[str, str], List[Dict[str, Any]]]:
    """(selection pool, table rows, absorbed {loser: survivor}, row updates)."""
    if ctx.session.get("mode") == S.MODE_FULL_SEARCH:
        run_ = await sourcing.stream_run_for(ctx)
        return run_.selection_pool(), run_.table, run_.absorbed(), []
    cands = await ctx.rows(states=[S.ROW_POOLED])
    raw = [c["row"] for c in cands]
    absorbed: Dict[str, str] = {}
    table = rules.deduplicate_candidates_ui(raw, None, absorbed)
    absorbed = rules.resolve_absorbed(absorbed, table)
    # A merge folds the losers' best info into the survivor: store it.
    before = {c["candidate_id"]: c["row"] for c in cands}
    updates = []
    for row in table:
        cid = rules.candidate_row_id(row)
        if cid in before and before[cid] is not row:
            updates.append({"candidate_id": cid, "row": row})
    return table, table, absorbed, updates


async def run(ctx: StageContext) -> StageResult:
    s2 = ctx.ck.get("s2") or {}
    if s2.get("done"):
        return StageResult(next_stage=S.STAGE_ENRICHING)
    await ctx.boundary()
    pool, table, absorbed, row_updates = await _pool(ctx)
    facts = await ctx.db(ctx.store.job_launch_state, ctx.job_keys)
    launched, blocked = launch_predicate(facts)
    limit, floor = _limit(ctx, len(table)), _floor(ctx)
    sel = rules.compute_auto_launch_selection(
        pool, launched_keys=launched["keys"], launched_ids=launched["ids"],
        limit=limit, min_score=floor, blocked=blocked, with_reasons=True,
    )
    table_ids = [rules.candidate_row_id(r) for r in table]
    in_table = set(table_ids)
    selected = [cid for cid in sel["ids"] if cid in in_table]
    selected_set = set(selected)

    items: Dict[str, Dict[str, Any]] = {}
    for upd in row_updates:
        items[upd["candidate_id"]] = dict(upd)
    for cid in selected:
        items.setdefault(cid, {"candidate_id": cid}).update(state=S.ROW_SELECTED, reason=None)
    for skip in sel.get("skipped") or []:
        cid = skip["id"]
        if cid in selected_set or cid not in in_table:
            continue
        entry = items.setdefault(cid, {"candidate_id": cid})
        if entry.get("state") != S.ROW_SKIPPED:
            entry.update(state=S.ROW_SKIPPED, reason=str(skip["reason"]))
    for loser, survivor in absorbed.items():
        if loser in selected_set:
            continue
        items.setdefault(loser, {"candidate_id": loser}).update(
            state=S.ROW_SKIPPED, reason=f"identity_duplicate:{survivor}",
        )
    await ctx.set_states(list(items.values()))

    skipped_by_reason: Dict[str, int] = {}
    for it in items.values():
        if it.get("state") == S.ROW_SKIPPED:
            kind = str(it.get("reason") or "").split(":", 1)[0]
            skipped_by_reason[kind] = skipped_by_reason.get(kind, 0) + 1
    selection = {
        "pool": len(pool), "table": len(table), "selected": len(selected),
        "selected_absent": len(sel["ids"]) - len(selected),
        "eligible_total": sel["eligibleTotal"], "below_floor": sel["belowFloor"], "unscored": sel["unscored"],
        "limit": limit, "min_score": floor, "skipped": skipped_by_reason,
    }
    await ctx.update(
        coalesce_now=["selection_done_at"],
        checkpoint={"s2": {"done": True, "selected": selected, **{k: v for k, v in selection.items() if k != "skipped"}}},
        counts={"selection": selection, "selected": len(selected)},
    )
    await ctx.event("selected", stage=S.STAGE_SELECTING, selected=len(selected), pool=len(pool))
    try:
        await ctx.db(ctx.store.stream_delete, ctx.fence)
    except Exception as exc:  # noqa: BLE001 — housekeeping deletes it later
        from services.launch_sessions.errors import LaunchSessionStop

        if isinstance(exc, LaunchSessionStop):
            raise
        logger.warning("launch_session stream cleanup failed session=%s: %s", ctx.sid, exc)
    ctx.memo.pop("stream_run", None)
    return StageResult(next_stage=S.STAGE_ENRICHING)
