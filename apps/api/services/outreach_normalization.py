"""Shared normalization helpers for outreach phase and communication channel.

Used across routers (e.g. launch_report, voice_agent) to map PairBot status,
phase, and channel variants onto canonical values (phase1/phase2/phase3 and call/sms/web).
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PENDING_STATUSES = {"pending", "scheduled", "queued", "contact_check", "not_started"}

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
_PHASE_ALIASES = {
    "phase_1": "phase1",
    "phase 1": "phase1",
    "1": "phase1",
    "stage1": "phase1",
    "contact_check": "phase1",
    "queued": "phase1",
    "scheduled": "phase1",
    "not_started": "phase1",
    "phase_2": "phase2",
    "phase 2": "phase2",
    "2": "phase2",
    "stage2": "phase2",
    "phase_3": "phase3",
    "phase 3": "phase3",
    "3": "phase3",
    "stage3": "phase3",
}


def normalize_phase(raw: Optional[str], *, allow_pending_aliases: bool = True) -> Optional[str]:
    """Map phase variants onto phase1/phase2/phase3."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in ("phase1", "phase2", "phase3"):
        return value
    if not allow_pending_aliases and value in _PENDING_STATUSES:
        return None
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
