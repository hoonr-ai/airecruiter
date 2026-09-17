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
    "extra": "phase1_extra",
    "extra outreach": "phase1_extra",
    "extra outreach (>80% match)": "phase1_extra",
    "high_score_extra": "phase1_extra",
    "extra 1": "phase1_extra",
    "extra 2": "phase1_6hr_extra",
    "extra 3": "phase2_extra",
}


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
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _is_high_score_extra_job(job: Dict[str, Any]) -> bool:
    payload = _parse_job_payload(job.get("payload"))
    if _truthy_flag(payload.get("is_high_score_extra")):
        return True
    reminder = str(payload.get("reminder_type") or job.get("reminder_type") or "").strip().lower()
    return reminder == "high_score_extra"


def _iter_outreach_jobs(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("scheduled_jobs", "jobs", "outreach_jobs"):
        items = payload.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    yield item
    nested = payload.get("outreach")
    if isinstance(nested, dict):
        for key in ("scheduled_jobs", "jobs", "outreach_jobs"):
            items = nested.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield item


def promote_high_score_extra_phase(
    payload: Optional[Dict[str, Any]],
    raw_phase: Optional[str],
) -> Optional[str]:
    """Append `_extra` when PairBot analytics would show Extra 1/2/3.

    PairBot's interview list uses get_canonical_outreach_phase_sql: stored
    `outreach_phase` can stay `phase1` while a processing/completed high-score
    extra job is active. The outreach-status API returns that raw column plus
    scheduled_jobs — without this promotion, rankings/launch report count P1
    for E1.
    """
    phase = (raw_phase or "").strip().lower()
    if not phase:
        return raw_phase
    if "extra" in phase or phase in {"pass", "fail", "completed", "passed", "failed"}:
        return phase
    if not isinstance(payload, dict):
        return phase

    extra_token = f"{phase}_extra"
    nested = payload.get("outreach") if isinstance(payload.get("outreach"), dict) else {}
    for source in (payload, nested):
        for bucket_key in ("communications", "events"):
            for item in source.get(bucket_key) or []:
                if not isinstance(item, dict):
                    continue
                item_phase = str(item.get("phase") or "").strip().lower()
                if item_phase == extra_token:
                    return extra_token

    pending_match = False
    for job in _iter_outreach_jobs(payload):
        if not _is_high_score_extra_job(job):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status not in {"completed", "processing", "pending"}:
            continue
        high = str(
            _parse_job_payload(job.get("payload")).get("high_score_phase") or "phase1"
        ).strip().lower()
        if high != phase:
            continue
        if status in {"completed", "processing"}:
            return extra_token
        pending_match = True
    # outreach-status omits completed jobs; a still-queued extra retry is the
    # only remaining signal that PairBot already moved this candidate to Extra.
    return extra_token if pending_match else phase
