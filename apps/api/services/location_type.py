"""Shared work-arrangement (location type) detection for JobDiva jobs.

Single source of truth for reconciling the JobDiva API's location-type field
with the job-description text. Used by services/jobdiva.py (job import) and
routers/campaigns.py (child-job seeding) — previously each carried its own
copy and they drifted.

The core subtlety: a plain ``\\bremote\\b`` search also matches the word
inside a negation ("this is NOT a remote role"), so a positive-mention flag
must be computed on text with the negation phrases stripped first. Without
that, "negated and not mentioned" conditions can never fire for any negation
that contains the word "remote" itself.
"""

import re
from typing import Tuple

# Negation phrases: "not remote", "no remote", "non-remote", "never remote",
# "not a WFH/remote role", "no wfh", "not work from home". The compound
# "wfh/remote" alternative is FIRST so it wins over the bare "wfh" match and
# the whole phrase gets consumed when stripping.
_REMOTE_NEGATION_RE = re.compile(
    r"\b(?:not|no|non|never)(?:-|\s+)(?:a\s+|an\s+)?"
    r"(?:(?:wfh/)?remote|wfh|work\s+from\s+home)\b"
)
_REMOTE_MENTION_RE = re.compile(r"\bremote\b")

_ONSITE_RE = re.compile(
    r"\b(?:onsite|on-site|work\s+on\s+site|working\s+on\s+site|"
    r"on\s+site\s+(?:work|role|position|basis|location|office|presence|"
    r"environment|days|requirement|required|mandatory|essential|only))\b"
)

# Only treat "hybrid" as a work-arrangement signal when it appears near
# work-context words. Avoid false positives from tech JDs that say
# "hybrid cloud", "hybrid architecture", etc.
_HYBRID_WORK_PHRASES = (
    "hybrid role", "hybrid position", "hybrid work", "hybrid schedule",
    "hybrid model", "hybrid arrangement", "hybrid option",
    "hybrid setting", "hybrid basis", "hybrid format",
    "hybrid working", "hybrid opportunity", "hybrid flexibility",
)
_HYBRID_TECH_PHRASES = (
    "hybrid cloud", "hybrid environment", "hybrid architecture",
    "hybrid infrastructure", "hybrid network", "hybrid system",
    "hybrid solution", "hybrid deployment", "hybrid setup",
    "hybrid approach", "hybrid technology", "hybrid platform",
    "hybrid data", "hybrid storage",
)


def detect_remote_signals(text: str) -> Tuple[bool, bool, bool]:
    """Return ``(mention, negated, has_remote)`` for a lowercased JD text.

    - ``mention``: "remote" appears OUTSIDE any negation phrase (positive
      mention).
    - ``negated``: a negation phrase ("not remote", "no WFH", ...) appears.
    - ``has_remote``: positive mention with no negation anywhere — the
      conservative "the JD affirms remote" flag (a negation vetoes even a
      separate positive mention).
    """
    lowered = str(text or "").lower()
    negated = bool(_REMOTE_NEGATION_RE.search(lowered))
    stripped = _REMOTE_NEGATION_RE.sub(" ", lowered)
    mention = bool(_REMOTE_MENTION_RE.search(stripped))
    return mention, negated, (mention and not negated)


def resolve_location_type(api_field_value: str, description: str) -> str:
    """Reconcile JobDiva's location-type field with the JD text.

    Returns ``"Remote" | "Hybrid" | "Onsite" | ""`` — empty when neither the
    API field nor the JD carries a usable signal (callers apply their own
    default, historically "Onsite").

    Precedence (API "Remote" is frequently a wrong JobDiva default, so the
    JD can override it, but only with an explicit contrary signal):
      1. API says Remote: stays Remote unless the JD signals hybrid, or the
         JD denies/contradicts remote without ever affirming it.
      2. API says Onsite and the JD agrees (and doesn't say hybrid): Onsite.
      3. Otherwise the JD text decides; when the JD is silent, the API field
         is trusted as-is.
    """
    val_lower = str(api_field_value or "").lower().strip()
    desc_lower = str(description or "").lower()

    has_hybrid = False
    if "hybrid" in desc_lower:
        if any(p in desc_lower for p in _HYBRID_WORK_PHRASES):
            has_hybrid = True
        elif any(p in desc_lower for p in _HYBRID_TECH_PHRASES):
            has_hybrid = False
        else:
            # Ambiguous standalone "hybrid" mention — trust the API field.
            has_hybrid = "hybrid" in val_lower

    has_onsite = bool(_ONSITE_RE.search(desc_lower))
    remote_mention, remote_negated, has_remote = detect_remote_signals(desc_lower)

    api_loc = ""
    if any(k in val_lower for k in ("remote", "wfh", "virtual", "telecommute")):
        api_loc = "Remote"
    elif "hybrid" in val_lower:
        api_loc = "Hybrid"
    elif "onsite" in val_lower or "on-site" in val_lower:
        api_loc = "Onsite"

    if api_loc == "Remote":
        if has_hybrid:
            return "Hybrid"
        if remote_negated and not remote_mention:
            # JD explicitly denies remote ("not a remote role", "no WFH")
            # and never affirms it — the JD overrides the API default.
            return "Onsite"
        if has_onsite and not remote_mention:
            # JD only talks about onsite and never mentions remote at all —
            # API "Remote" is the known-bad default; trust the JD.
            return "Onsite"
        return "Remote"
    if api_loc == "Onsite" and has_onsite and not has_hybrid:
        return "Onsite"
    if has_hybrid:
        return "Hybrid"
    if has_onsite and has_remote:
        # Mentions both Onsite and Remote -> usually implies a Hybrid arrangement.
        return "Hybrid"
    if has_remote:
        return "Remote"
    if has_onsite:
        return "Onsite"
    # JD is silent about location keywords, trust the API field.
    return api_loc
