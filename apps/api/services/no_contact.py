"""No-contact company list.

Candidates whose CURRENT or LAST (most recent past) employer loosely matches
a configured company keyword are surfaced in Step 5 search results but:

  - never LLM-scored (no candidate-detail scoring spend on them),
  - never persisted to sourced_candidates (display-only, per search),
  - greyed out in the UI with every action disabled,
  - blocked server-side at /candidates/save and the launch gate.

The list lives in code (core.sourcing_config.NO_CONTACT_COMPANIES) —
deliberately not DB-backed yet. Admins get a read-only view via
routers/no_contact.py; adds/removes happen through code only.

Matching is LOOSE by request ("Kaiser" must catch "Kaiser Permanente",
"Citi Bank", "citybank") but bounded so it stays predictable:

  - whole-token containment via company_match.is_same_company semantics
    ("Kaiser" ⊂ "Kaiser Permanente", but "Intuit" ⊄ "Intuitive Surgical");
  - collapsed n-gram equality for split/joined spellings ("Citi Bank");
  - Damerau-Levenshtein distance ≤ 1 (≤ 2 for keywords ≥ 9 chars) on the
    collapsed n-grams, catching one-typo variants ("citybank", "kasier").
    Fuzzy is skipped for keywords shorter than 5 chars.

Over-matching greys out a contactable candidate (annoying); under-matching
contacts a no-contact company's employee (compliance breach) — thresholds err
toward the former.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rapidfuzz.distance import DamerauLevenshtein

from services.company_match import (
    _end_sort_key,  # noqa: F401 — re-exported for existing importers
    _entry_is_current,
    _experience_lists,  # noqa: F401 — re-exported for existing importers
    _is_contiguous_sublist,
    collect_current_companies,
    collect_last_companies,
    normalize_company_name,
)

logger = logging.getLogger(__name__)


_DEFAULT_KEYWORDS = ("Kaiser", "Citibank", "Intuit")


def get_no_contact_companies() -> List[str]:
    """The configured keyword list, trimmed and de-duplicated, order kept.

    Read via getattr at call time (matching how services read other
    sourcing_config flags) so a missing constant degrades to the default
    rather than crashing the pipeline."""
    try:
        from core import sourcing_config as _sc
        configured = getattr(_sc, "NO_CONTACT_COMPANIES", _DEFAULT_KEYWORDS)
    except Exception:  # noqa: BLE001
        configured = _DEFAULT_KEYWORDS
    if isinstance(configured, str):
        configured = configured.split(",")
    seen = set()
    out: List[str] = []
    for raw in configured:
        kw = str(raw or "").strip()
        key = kw.lower()
        if not kw or key in seen:
            continue
        seen.add(key)
        out.append(kw)
    return out


def _fuzzy_budget(collapsed_kw: str) -> int:
    """Allowed Damerau-Levenshtein distance for a collapsed keyword."""
    if len(collapsed_kw) < 5:
        return 0  # short keywords match exactly only
    return 2 if len(collapsed_kw) >= 9 else 1


def matches_no_contact_company(
    company: str, keywords: Optional[List[str]] = None
) -> Optional[str]:
    """The configured keyword that `company` matches, else None."""
    comp_norm = normalize_company_name(company)
    if not comp_norm:
        return None
    comp_tokens = comp_norm.split()
    for kw in keywords if keywords is not None else get_no_contact_companies():
        kw_norm = normalize_company_name(kw)
        if not kw_norm:
            continue
        kw_tokens = kw_norm.split()
        if comp_tokens == kw_tokens or _is_contiguous_sublist(kw_tokens, comp_tokens):
            return kw
        collapsed_kw = "".join(kw_tokens)
        budget = _fuzzy_budget(collapsed_kw)
        # Compare collapsed keyword against collapsed runs of company tokens
        # so "Citi Bank" (and one-typo variants) match "Citibank".
        max_n = len(kw_tokens) + 1
        for i in range(len(comp_tokens)):
            for j in range(i + 1, min(i + max_n, len(comp_tokens)) + 1):
                gram = "".join(comp_tokens[i:j])
                if gram == collapsed_kw:
                    return kw
                if budget and DamerauLevenshtein.distance(gram, collapsed_kw) <= budget:
                    return kw
    return None


# ── last (most recent past) employer ──────────────────────────────────────
#
# Moved to company_match.py once the hiring-client exclusion started
# needing the same last-employer signal. Re-exported here so existing
# `from services.no_contact import collect_last_companies` callers and
# tests keep working.

# ── candidate-level check + flag stamping ─────────────────────────────────

def check_no_contact(
    candidate: Dict[str, Any], keywords: Optional[List[str]] = None
) -> Optional[Dict[str, str]]:
    """{"company", "keyword", "relation": "current"|"last"} when the candidate's
    current or last employer matches the no-contact list, else None."""
    kws = keywords if keywords is not None else get_no_contact_companies()
    if not kws:
        return None
    for comp in collect_current_companies(candidate):
        kw = matches_no_contact_company(comp, kws)
        if kw:
            return {"company": comp, "keyword": kw, "relation": "current"}
    for comp in collect_last_companies(candidate):
        kw = matches_no_contact_company(comp, kws)
        if kw:
            return {"company": comp, "keyword": kw, "relation": "last"}
    return None


def apply_no_contact_flag(candidate: Dict[str, Any]) -> bool:
    """Recompute and stamp `no_contact` / `no_contact_reason` /
    `no_contact_company` on the candidate dict. Returns the flag value.

    Recomputation is authoritative in BOTH directions so read paths self-heal:
    a stored row that started matching after a list change gets flagged, and a
    stale flag clears if the list shrinks. Never raises — on error the
    existing flag is left untouched (fail toward current state, not toward
    contacting)."""
    try:
        hit = check_no_contact(candidate)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"no_contact check failed (flag left as-is): {exc}")
        return bool(candidate.get("no_contact"))
    if hit:
        relation = "Current" if hit["relation"] == "current" else "Last"
        candidate["no_contact"] = True
        candidate["no_contact_reason"] = (
            f"{relation} employer '{hit['company']}' is on the no-contact list"
            f" ({hit['keyword']})"
        )
        candidate["no_contact_company"] = hit["keyword"]
        return True
    if candidate.get("no_contact"):
        candidate["no_contact"] = False
        candidate.pop("no_contact_reason", None)
        candidate.pop("no_contact_company", None)
    return False
