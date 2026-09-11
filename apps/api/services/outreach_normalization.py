"""Shared normalization helpers for outreach phase and communication channel.

Used across routers (e.g. launch_report, voice_agent) to map PairBot status,
phase, and channel variants onto canonical values (contact_check, phase1/phase1_6hr/phase2/phase3,
extra outreach phases, and call/sms/web).
"""
import logging
from typing import Any, Dict, Optional

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
