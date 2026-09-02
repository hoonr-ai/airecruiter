"""Post-interview employer backstop.

Phase 2 of the employer-identification work (2026-09-02): the launch-time
resolution pass (services/employer_resolution.py) can still finish with an
UNVERIFIED employer — no resume, placeholder resume, extraction miss. Those
candidates launch anyway (holding back every no-resume candidate forever is
worse), so the interview itself becomes the last check:

  1. Every PAIR launch payload carries one extra pre-screen question asking
     where the candidate works right now (`append_employer_question`, called
     from generate_engage_payload; category "logistics" so the L0.5 boolean
     rewrite preserves it verbatim).
  2. The PairBot webhook (routers/voice_agent.py) finds the answer in the
     interview transcriptions (`extract_stated_employer`), persists it to
     sourced_candidates.data as `stated_current_employer` — a durable signal
     every FUTURE launch gate and no-contact flag now reads — and re-runs the
     no-contact + hiring-client checks on it (`stated_employer_conflict`),
     logging `post_interview_employer_conflict` and stamping
     `stated_employer_conflict` on the row when they hit.

The answer is free text ("I'm with TCS, working at a Walmart project"), so it
is only ever matched one-directionally: no-contact keywords scan it with the
keyword-⊂-text matcher, and the hiring client with client_appears_in_text
(client ⊂ text, never the reverse).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Spoken by the screener, answered in free text. The stable "which company do
# you currently work" stem is the round-trip marker: injection dedups on it
# and the webhook finds the answer row by it, so reworded variants stay
# recognizable as long as the stem survives.
EMPLOYER_QUESTION_TEXT = (
    "Which company do you currently work for? "
    "If you are between jobs right now, just say so."
)
_QUESTION_MARKER = "which company do you currently work"

_MAX_STATED_LEN = 300


def _employer_question_enabled() -> bool:
    try:
        from core import sourcing_config as _sc
        return bool(getattr(_sc, "EMPLOYER_QUESTION_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def is_employer_question(text: Any) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return _QUESTION_MARKER in normalized


def append_employer_question(
    questions: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """`questions` plus the current-employer question, unless it is already
    present or the feature is off. Appended in the PAIR schema shape the
    sanitizer emits, and last — it's a logistics closer, not a screener."""
    out = list(questions or [])
    if not _employer_question_enabled():
        return out
    if any(is_employer_question((q or {}).get("question_text")) for q in out if isinstance(q, dict)):
        return out
    out.append({
        "question_text": EMPLOYER_QUESTION_TEXT,
        "pass_criteria": "",
        "is_default": True,
        "category": "logistics",
        "is_hard_filter": False,
    })
    return out


_NO_ANSWER_RE = re.compile(r"^(n/?a|no|none|nothing|skip|-+)$", re.I)


def extract_stated_employer(transcriptions: Any) -> Optional[str]:
    """The candidate's answer to the employer question, from the webhook's
    transcription rows, else None. Whitespace-collapsed and capped; kept
    verbatim otherwise ("between jobs" answers included — they are a real
    signal that simply matches nothing)."""
    if not isinstance(transcriptions, list):
        return None
    for row in transcriptions:
        if not isinstance(row, dict):
            continue
        if not is_employer_question(row.get("question")):
            continue
        answer = " ".join(str(row.get("answer") or "").split()).strip()
        if len(answer) < 2 or _NO_ANSWER_RE.match(answer):
            continue
        return answer[:_MAX_STATED_LEN]
    return None


def stated_employer_conflict(answer: str, client_name: str = "") -> Optional[str]:
    """Human-readable reason when the stated employer hits the no-contact list
    or the hiring client, else None. Never raises."""
    text = str(answer or "").strip()
    if not text:
        return None
    try:
        from services.no_contact import matches_no_contact_company
        kw = matches_no_contact_company(text)
        if kw:
            return f"No-Contact Company ({kw}) — stated in interview"
    except Exception as exc:  # noqa: BLE001
        logger.warning("stated-employer no-contact check failed: %s", exc)
    try:
        from services.company_match import client_appears_in_text
        if client_name and client_appears_in_text(text, client_name):
            return f"Employed by Hiring Client ({client_name}) — stated in interview"
    except Exception as exc:  # noqa: BLE001
        logger.warning("stated-employer client check failed: %s", exc)
    return None
