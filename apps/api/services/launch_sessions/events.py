"""Progress events of a launch session: the browser's log and New Relic.

Two sinks, both best-effort (an event never aborts a stage):

  launch_session_events   rows the browser polls (GET …/events?after_seq=N):
                          ``{"type", "stage", "status", "at", …}`` with ids,
                          enums and counters only.
  New Relic               one ``PairLaunchSession`` custom event per stage
                          transition: session_id, job_id, stage, status,
                          ms_in_stage, attempt, claims, trigger, mode and the
                          numeric counts — never a name, email, phone or text.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_MAX_COUNT_ATTRS = 40
_LABEL_RE = re.compile(r"[^a-z0-9_]")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def payload(kind: str, **fields: Any) -> Dict[str, Any]:
    """An event row: ``type`` + the given fields + ``at`` (UTC ISO)."""
    out: Dict[str, Any] = {"type": kind}
    out.update({k: v for k, v in fields.items() if v is not None})
    out["at"] = now_iso()
    return out


def _label(value: Any, limit: int = 40) -> Optional[str]:
    text = _LABEL_RE.sub("_", str(value or "").strip().lower())[:limit]
    return text or None


def numeric_counts(counts: Any, prefix: str = "count_") -> Dict[str, int]:
    """Numbers only, nested dicts flattened one level (``count_<k>_<sub>``)."""
    out: Dict[str, int] = {}
    if not isinstance(counts, Mapping):
        return out

    def put(name: str, value: Any) -> None:
        if len(out) >= _MAX_COUNT_ATTRS or isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            label = _label(name)
            if label:
                out[f"{prefix}{label}"] = int(value)

    for key, value in counts.items():
        if isinstance(value, Mapping):
            for sub, sub_value in value.items():
                put(f"{key}_{sub}", sub_value)
        else:
            put(str(key), value)
    return out


def nr_session_event(
    session: Mapping[str, Any], *, stage: str, status: str, ms_in_stage: Optional[int] = None,
    outcome: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Emit ``PairLaunchSession`` (no PII). Returns the attributes (tests)."""
    try:
        attrs: Dict[str, Any] = {
            "session_id": str(session.get("id") or ""),
            "job_id": str(session.get("job_id") or ""),
            "stage": _label(stage),
            "status": _label(status),
            "trigger": _label(session.get("trigger")),
            "mode": _label(session.get("mode")),
            "attempt": int(session.get("attempt") or 0),
            "claims": int(session.get("claims") or 0),
        }
        if ms_in_stage is not None:
            attrs["ms_in_stage"] = int(max(0, ms_in_stage))
        if outcome:
            attrs["outcome"] = _label(outcome)
        attrs.update(numeric_counts(session.get("counts")))
        attrs = {k: v for k, v in attrs.items() if v is not None and v != ""}
    except Exception as exc:  # noqa: BLE001
        logger.debug("launch_session nr event build failed: %s", exc)
        return None
    try:
        from core.newrelic import record_custom_event

        record_custom_event("PairLaunchSession", attrs)
    except Exception as exc:  # noqa: BLE001
        logger.debug("launch_session nr event failed: %s", exc)
    return attrs
