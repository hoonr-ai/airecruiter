"""S5 — splitting: ready vs needs-contact (rules.split_ready_needs_contact).

DNC is re-checked first on the EFFECTIVE phone (override, else the row's own
top-level phone — rules.dnc_dropped_ids) because a lookup may have found a
listed number: matches are skipped ``dnc``. Ready = a usable phone OR a real
email, where a channel counts only if valid AND not shared with another row of
this selection (minus DNC / hard-filter skips): a duplicated phone with a
unique valid email is ready. The rest are PARKED as needs_contact — the
session does not wait for them; POST …/needs-contact (after the session
finishes) launches them in a child session.
"""

from __future__ import annotations

from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.contacts import effective_row, overrides_for
from services.launch_sessions.context import StageContext, StageResult


async def run(ctx: StageContext) -> StageResult:
    if (ctx.ck.get("s5") or {}).get("done"):
        return StageResult(next_stage=S.STAGE_SAVING)
    await ctx.boundary()
    cands = await ctx.rows(states=[S.ROW_SELECTED])
    rows = [effective_row(c) for c in cands]
    overrides = overrides_for(cands)
    selected_ids = {c["candidate_id"] for c in cands}
    hard = rules.compute_hard_filter_skips(rows, selected_ids)
    dnc_phones = await ctx.db(ctx.fx.dnc_phones)
    dnc = rules.dnc_dropped_ids(rows, selected_ids, overrides, dnc_phones)
    split = rules.split_ready_needs_contact(
        rows, selected_ids=selected_ids, skip_ids=set(dnc) | set(hard["ids"]),
        overrides=overrides, jobdiva_id=ctx.job_ref,
    )
    dup_email, dup_phone = set(split["duplicateEmailIds"]), set(split["duplicatePhoneIds"])
    items = [{"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "dnc"} for cid in dnc]
    items += [
        {"candidate_id": cid, "state": S.ROW_SKIPPED, "reason": "hard_filter"}
        for cid in hard["ids"] if cid not in set(dnc)
    ]
    items += [{"candidate_id": cid, "state": S.ROW_READY, "reason": None} for cid in split["readyIds"]]
    for entry in split["needsInfo"]:
        cid = entry["candidate_id"]
        needs = dict(entry, duplicateEmail=cid in dup_email, duplicatePhone=cid in dup_phone)
        items.append({
            "candidate_id": cid, "state": S.ROW_NEEDS_CONTACT,
            "reason": "duplicate_contact" if (cid in dup_email or cid in dup_phone) else "missing_contact",
            "contact": {"needs": needs},
        })
    await ctx.set_states(items, from_states=[S.ROW_SELECTED])
    split_counts = {
        "ready": len(split["readyIds"]), "needs_contact": len(split["needsInfo"]), "dnc": len(dnc),
        "hard_filter": len(hard["ids"]), "duplicate_email": len(dup_email), "duplicate_phone": len(dup_phone),
    }
    await ctx.update(checkpoint={"s5": {"done": True}}, counts={"split": split_counts})
    await ctx.event("split", stage=S.STAGE_SPLITTING, **split_counts)
    return StageResult(next_stage=S.STAGE_SAVING)
