"""Shared normalization helpers for outreach phase and communication channel.

Used across routers (e.g. launch_report, voice_agent, jobs outreach-stats) to
map PairBot status, phase, and channel variants onto canonical values:

  contact_check,
  phase1 / phase1_6hr / phase2 / phase3  (PairBot analytics labels P1–P4),
  phase1_extra / phase1_6hr_extra / phase2_extra  (Extra 1–3, resume score ≥ 80),
  and call/sms/web.

PairBot persists the canonical `phase1` / `phase1_6hr` / `phase*_extra` tokens,
not the table shorthand P1/E1. Those shorthand strings are UI labels only.
"""
import json
import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_PENDING_STATUSES = {"pending", "scheduled", "queued", "not_started"}

_CHANNEL_COLUMNS = {"call": "call", "sms": "sms", "email": "web"}
_CHANNEL_ALIASES = {
    "phone": "call",
    "voice": "call",
    "telephony": "call",
    "text": "sms",
    "whatsapp": "sms",
    "mail": "web",
    "web": "web",
}

# Canonical phases: contact check, initial outreach (phase1), retry phases (phase1_6hr, phase2, phase3),
# and extra outreach phases (>80% match: phase1_extra, phase1_6hr_extra, phase2_extra).
_CANONICAL_PHASES = {
    "contact_check",
    "phase1",
    "phase1_6hr",
    "phase2",
    "phase3",
    "phase1_extra",
    "phase1_6hr_extra",
    "phase2_extra",
    "phase3_extra",
}

# Mirrors Pair Bot `OUTREACH_PHASE_RANK` so Extra promotion never regresses.
_OUTREACH_PHASE_RANK = {
    "contact_check": 0,
    "phase1": 10,
    "phase1_extra": 20,
    "phase1_6hr": 30,
    "phase1_6hr_extra": 40,
    "phase2": 50,
    "phase2_extra": 60,
    "phase3": 70,
    "phase3_extra": 80,
}

_BASE_TO_EXTRA = {
    "phase1": "phase1_extra",
    "phase1_6hr": "phase1_6hr_extra",
    "phase2": "phase2_extra",
    "phase3": "phase3_extra",
}

_PHASE_ALIASES = {
    # Contact Check
    "contact_check": "contact_check",
    "contact check": "contact_check",
    "pre_screen": "contact_check",
    "queued": "contact_check",
    "scheduled": "contact_check",
    "not_started": "contact_check",
    # Phase 1
    "phase_1": "phase1",
    "phase 1": "phase1",
    "1": "phase1",
    "stage1": "phase1",
    # Phase 2 (6hr follow-up)
    "phase1_6hr": "phase1_6hr",
    "phase 2": "phase1_6hr",
    "phase_2": "phase1_6hr",
    "6hr": "phase1_6hr",
    "phase1_6hr_reminder": "phase1_6hr",
    # Phase 3 (Day 2 follow-up)
    "phase2": "phase2",
    "phase_3": "phase2",
    "phase 3": "phase2",
    "2": "phase2",
    "stage2": "phase2",
    # Phase 4 (Day 3 follow-up / terminal)
    "phase3": "phase3",
    "phase_4": "phase3",
    "phase 4": "phase3",
    "3": "phase3",
    "4": "phase3",
    "stage3": "phase3",
    "stage4": "phase3",
    # Extra Outreach (>80% Match)
    "phase1_extra": "phase1_extra",
    "extra_phase1": "phase1_extra",
    "extra outreach phase 1": "phase1_extra",
    "extra outreach 1": "phase1_extra",
    "phase1_6hr_extra": "phase1_6hr_extra",
    "extra_phase2": "phase1_6hr_extra",
    "extra outreach phase 2": "phase1_6hr_extra",
    "extra outreach 2": "phase1_6hr_extra",
    "phase2_extra": "phase2_extra",
    "extra_phase3": "phase2_extra",
    "extra outreach phase 3": "phase2_extra",
    "extra outreach 3": "phase2_extra",
    "phase3_extra": "phase3_extra",
    "extra": "phase1_extra",
    "extra outreach": "phase1_extra",
    "extra outreach (>80% match)": "phase1_extra",
    "high_score_extra": "phase1_extra",
    "extra 1": "phase1_extra",
    "extra 2": "phase1_6hr_extra",
    "extra 3": "phase2_extra",
}


def _phase_rank(phase: Optional[str]) -> int:
    return _OUTREACH_PHASE_RANK.get((phase or "").strip().lower(), 0)


def _extra_token_for_base(base_phase: str) -> Optional[str]:
    return _BASE_TO_EXTRA.get((base_phase or "").strip().lower())


def normalize_phase(raw: Optional[str], *, allow_pending_aliases: bool = True) -> Optional[str]:
    """Map phase variants onto canonical phases (contact_check, phase1..phase3, extra phases)."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    if not allow_pending_aliases and (value in _PENDING_STATUSES or value == "contact_check"):
        return None
    if value in _CANONICAL_PHASES:
        return value
    aliased = _PHASE_ALIASES.get(value)
    if aliased:
        return aliased
    logger.warning(f"OUTREACH-NORMALIZATION: unrecognised outreach phase {value!r} — not counted")
    return None


def normalize_channel(raw: Optional[str]) -> Optional[str]:
    """Map communication channel/source variants onto call/sms/web columns."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in _CHANNEL_COLUMNS:
        return _CHANNEL_COLUMNS[value]
    mapped = _CHANNEL_ALIASES.get(value)
    if mapped:
        return mapped
    logger.warning(f"OUTREACH-NORMALIZATION: unrecognised communication channel/source {value!r} — not counted")
    return None


def _parse_job_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truthy_flag(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _is_high_score_extra_job(job: Dict[str, Any], job_payload: Optional[Dict[str, Any]] = None) -> bool:
    payload = job_payload if job_payload is not None else _parse_job_payload(job.get("payload"))
    if _truthy_flag(payload.get("is_high_score_extra")):
        return True
    reminder = str(payload.get("reminder_type") or job.get("reminder_type") or "").strip().lower()
    return reminder == "high_score_extra"


def _job_dedupe_key(job: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "job_type": job.get("job_type"),
            "scheduled_at": str(job.get("scheduled_at") or ""),
            "status": job.get("status"),
            "payload": _parse_job_payload(job.get("payload")),
        },
        sort_keys=True,
        default=str,
    )


def _iter_outreach_jobs(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    seen = set()
    nested = payload.get("outreach") if isinstance(payload.get("outreach"), dict) else {}
    for source in (payload, nested):
        for key in ("scheduled_jobs", "jobs", "outreach_jobs"):
            items = source.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                marker = _job_dedupe_key(item)
                if marker in seen:
                    continue
                seen.add(marker)
                yield item


def promote_high_score_extra_phase(
    payload: Optional[Dict[str, Any]],
    raw_phase: Optional[str],
) -> Optional[str]:
    """Promote to Extra 1/2/3 when PairBot analytics would show E1/E2/E3.

    PairBot's interview list uses get_canonical_outreach_phase_sql, which can
    resolve Extra from completed/processing high-score jobs even when the
    stored `outreach_phase` column still says phase1/phase1_6hr/phase2.
    outreach-status returns that raw column and only pending/processing jobs,
    so PAIR must also read communications/events (which keep completed Extra
    sends) and apply the same anti-regression rank rules.
    """
    phase = (raw_phase or "").strip().lower()
    if not phase:
        return raw_phase
    if phase in {"pass", "fail", "completed", "passed", "failed"}:
        return phase
    if not isinstance(payload, dict):
        return phase

    stored_rank = _phase_rank(phase)
    best_extra: Optional[str] = None
    best_rank = stored_rank

    def _consider(token: Optional[str]) -> None:
        nonlocal best_extra, best_rank
        if not token:
            return
        rank = _phase_rank(token)
        if rank > best_rank:
            best_extra = token
            best_rank = rank

    # Already-persisted extra tokens win immediately.
    if "extra" in phase:
        return phase

    nested = payload.get("outreach") if isinstance(payload.get("outreach"), dict) else {}
    for source in (payload, nested):
        for bucket_key in ("communications", "events"):
            for item in source.get(bucket_key) or []:
                if not isinstance(item, dict):
                    continue
                item_phase = str(item.get("phase") or "").strip().lower()
                if "extra" not in item_phase:
                    continue
                # Prefer canonical extra tokens; ignore unknown labels.
                if item_phase in _OUTREACH_PHASE_RANK:
                    _consider(item_phase)
                else:
                    aliased = normalize_phase(item_phase)
                    if aliased and "extra" in aliased:
                        _consider(aliased)

    # PairBot SQL: latest high-score job's high_score_phase → that phase's
    # `_extra`, unless stored is already at/past that extra. We cannot see
    # completed jobs here, but pending/processing still carry the signal, and
    # we never regress past a higher stored/comms rank.
    pending_extra: Optional[str] = None
    for job in _iter_outreach_jobs(payload):
        job_payload = _parse_job_payload(job.get("payload"))
        if not _is_high_score_extra_job(job, job_payload):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status not in {"completed", "processing", "pending"}:
            continue
        high = str(job_payload.get("high_score_phase") or "").strip().lower()
        if not high:
            logger.warning(
                "OUTREACH-NORMALIZATION: high-score extra job missing high_score_phase "
                "— not promoting stored phase %r",
                phase,
            )
            continue
        target = _extra_token_for_base(high)
        if not target:
            continue
        if status in {"completed", "processing"}:
            _consider(target)
        elif _phase_rank(target) > stored_rank:
            # Keep the highest pending target; apply only if nothing stronger
            # was found from comms/completed/processing.
            if pending_extra is None or _phase_rank(target) > _phase_rank(pending_extra):
                pending_extra = target

    if best_extra:
        return best_extra
    if pending_extra:
        return pending_extra
    return phase
