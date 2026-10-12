"""JSON views of a session and its rows for the browser (no input snapshot,
no résumé text in the table view)."""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.contacts import current_contact, effective_row, override_of
from services.launch_sessions.store import jsonable, light_row

_TIMESTAMPS = (
    "clicked_at", "stage_started_at", "sourcing_started_at", "sourcing_done_at", "selection_done_at",
    "enrich_done_at", "review_started_at", "review_done_at", "save_started_at", "save_done_at",
    "launch_started_at", "first_interview_created_at", "last_batch_done_at", "first_dial_at",
    "completed_at", "created_at", "updated_at",
)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).isoformat()
    return str(value) if value else None


def _aware(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    return None


def _plan(attempts: Any) -> List[Dict[str, Any]]:
    """The relaxation chain (label + boolean string per attempt), so a
    reattached browser can rebuild its attempts panel."""
    out = []
    for a in attempts or []:
        if not isinstance(a, dict):
            continue
        payload = a.get("payload") if isinstance(a.get("payload"), dict) else {}
        out.append({"label": a.get("label") or "", "boolean_string": str(payload.get("boolean_string") or "")})
    return out


def session_view(
    row: Dict[str, Any], *, events_tail: Optional[List[Dict[str, Any]]] = None,
    children: Optional[List[Dict[str, Any]]] = None, queue_position: Optional[int] = None,
) -> Dict[str, Any]:
    """The browser's view of a session.

    ``launch_complete``: the launch is over — terminal, or past S7 (side
    effects fired, every row final; only JobDiva provisioning / the wrap-up
    left). The browser may show the result, open the needs-contact modal and
    start children or a new launch then, without waiting for ``terminal``.
    ``queue_position``: while waiting for a worker, how many sessions of the
    same phase are ahead of it (0 = next); None otherwise.
    Redirect rule (review-parity #10, as the browser counts "failed": save
    and batch failures, not Pairbot's per-person "no interview"):
    counts.failed - counts.no_interview == 0 and
    (counts.sent + counts.already_launched) > 0 and counts.needs_contact == 0.
    ``counts.saved`` / ``saved_total`` are S6's total; ``rows_in_saved`` the
    rows still saved at the end; ``no_interview`` the people Pairbot created
    no interview for; ``failed_batches`` the launch batches that failed (the
    job-posting email's clean-launch rule).
    """
    ck = row.get("checkpoint") or {}
    counts = row.get("counts") or {}
    states = counts.get("states") or {}
    inp = row.get("input") or {}
    status = row.get("status")
    now = _dt.datetime.now(_dt.timezone.utc)
    lease_until, not_before = _aware(row.get("lease_until")), _aware(row.get("not_before"))
    lease_active = status == S.STATUS_RUNNING and lease_until is not None and lease_until > now
    waiting = status == S.STATUS_RUNNING and not lease_active
    s1 = ck.get("s1") or {}
    try:
        from services.launch_timing import session_timing

        timing = {k: (_iso(v) if isinstance(v, _dt.datetime) else v) for k, v in session_timing(row).items()}
    except Exception:  # noqa: BLE001
        timing = {}
    view = {
        "id": row.get("id"),
        "job_id": row.get("job_id"),
        "jobdiva_ref": row.get("jobdiva_ref"),
        "parent_session_id": row.get("parent_session_id"),
        "trigger": row.get("trigger"),
        "mode": row.get("mode"),
        "status": status,
        "stage": row.get("stage"),
        "terminal": status in S.TERMINAL_STATUSES,
        "launch_complete": S.launch_complete(status, row.get("stage")),
        "queue_position": queue_position,
        # running = a worker holds it; waiting = released (retry back-off,
        # Pairbot still creating, or a deploy) — the next tick resumes it.
        "phase": ("running" if lease_active else "waiting") if status == S.STATUS_RUNNING else status,
        "retry_at": _iso(not_before) if waiting and not_before else None,
        "error": row.get("error"),
        "requested_by": row.get("requested_by"),
        "require_review": bool(inp.get("require_review")),
        "attempt": int(row.get("attempt") or 0),
        "claims": int(row.get("claims") or 0),
        "cancel_requested": bool(row.get("cancel_requested")),
        "cancel_reason": row.get("cancel_reason"),
        "counts": counts,
        "needs_contact_count": int(states.get(S.ROW_NEEDS_CONTACT, 0)),
        "failed_count": int(states.get(S.ROW_FAILED, 0)),
        "unconfirmed_count": int(states.get(S.ROW_UNCONFIRMED, 0)),
        "launched_count": int(states.get(S.ROW_LAUNCHED, 0)),
        "sourcing": {
            "attempts_total": s1.get("attempts_total", len(inp.get("search_attempts") or [])),
            "next_attempt": s1.get("next_attempt", 0),
            "current_attempt": s1.get("current_attempt"),
            "current_label": s1.get("current_label"),
            "pool": s1.get("pool", 0),
            "qualified": s1.get("qualified", 0),
            "done": bool(s1.get("done")),
            "attempts": [
                {k: a.get(k) for k in ("label", "began", "rows", "qualified")} for a in (s1.get("attempts") or [])
            ],
            "ui": s1.get("ui") or {},
            "plan": _plan(inp.get("search_attempts")),
        },
        "review": ck.get("review") or None,
        "input": {
            "launch_count": inp.get("launch_count"),
            "min_score": inp.get("min_score"),
            "qualified_target": inp.get("qualified_target"),
            "search_attempts": len(inp.get("search_attempts") or []),
            "options": inp.get("options") or {},
            "skip_enrichment": bool(inp.get("skip_enrichment")),
        },
        "timings": timing,
        "timestamps": {k: _iso(row.get(k)) for k in _TIMESTAMPS},
        "last_event_seq": int(row.get("event_seq") or 0),
        "cand_rev": int(row.get("cand_rev") or 0),
    }
    if events_tail is not None:
        view["events_tail"] = [
            {"seq": e.get("seq"), "payload": e.get("payload"), "created_at": _iso(e.get("created_at"))}
            for e in events_tail
        ]
    if children is not None:
        view["children"] = [
            {"id": c.get("id"), "trigger": c.get("trigger"), "status": c.get("status"), "stage": c.get("stage")}
            for c in children
        ]
    return jsonable(view)


def candidate_view(c: Dict[str, Any], *, view: str = "table") -> Dict[str, Any]:
    """table: the row without résumé text / LinkedIn profile + state and the
    contact layers; full: the complete row; failed: the browser's
    LaunchFailedCandidate shape (the failed-launch CSV)."""
    contact = c.get("contact") or {}
    if view == "failed":
        eff = effective_row(c)
        applied = rules.apply_contact_overrides([eff], {str(c["candidate_id"]): override_of(contact) or {}})[0]
        p = rules.build_save_candidate_payload(applied)
        skills = p.get("skills") or []
        return jsonable({
            "candidate_id": p["candidate_id"],
            "name": p["name"],
            "email": p["email"],
            "phone": p["phone"],
            "source": p["source"],
            "headline": p["headline"],
            "location": p["location"],
            "experience_years": p["experience_years"],
            "match_score": p["match_score"],
            "skills": "; ".join(
                s if isinstance(s, str) else str((s or {}).get("name") or (s or {}).get("skill") or "")
                for s in skills if s
            ),
            "matched_skills": "; ".join(str(s) for s in (p.get("matched_skills") or [])),
            "resume_id": p["resume_id"],
            "profile_url": p["profile_url"],
            "batch_index": c.get("batch_index") if c.get("batch_index") is not None else 0,
            "failure_stage": c.get("failure_stage") or "engage",
            "error_message": c.get("error") or c.get("reason") or "",
        })
    cur = current_contact(c)
    out = {
        "candidate_id": c.get("candidate_id"),
        "state": c.get("state"),
        "reason": c.get("reason"),
        "match_score": c.get("match_score"),
        "source": c.get("source"),
        "ord": c.get("ord"),
        "rev": c.get("rev"),
        "updated_at": _iso(c.get("updated_at")),
        "failure_stage": c.get("failure_stage"),
        "batch_index": c.get("batch_index"),
        "interview_id": c.get("interview_id"),
        "error": c.get("error"),
        "contact": contact,
        "current_phone": cur["phone"],
        "current_email": cur["email"],
        "row": c.get("row") if view == "full" else light_row(c.get("row") or {}),
    }
    return jsonable(out)
