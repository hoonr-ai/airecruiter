"""Company-name matching for the "currently employed by the hiring client"
exclusion.

Used in two places with the SAME semantics so search-time filtering and
launch-time gating can't disagree:

  - sourcing (services/unified_candidate_search.py, services/unipile.py,
    services/exa_service.py): drop/flag Exa + Unipile results whose CURRENT
    company is the hiring client, per the hard requirement that we never
    source a client's own employees;
  - launch (routers/engagement.py `is_candidate_excluded_from_pair`): final
    server-side gate before PAIR outreach.

Matching is deliberately token-based, not substring-based: client "Meta"
must match "Meta Platforms" but never "Metadata Solutions".
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_LEGAL_NOISE_TOKENS = {
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "pvt",
    "private",
    "limited",
    "technologies",
    "technology",
    "solutions",
    "consulting",
    "services",
    "group",
    "holdings",
}

# Placeholder client names that must never drive an exclusion.
# "unknown customer" is the JobDiva sync's own fallback when a req carries no
# customer (services/jobdiva.py: `str(raw_customer or "").title() or "Unknown
# Customer"`). Without it here the normalized form is treated as a real client,
# so a candidate whose company is literally "Unknown" token-matches it and gets
# excluded as "Employed by Hiring Client".
_PLACEHOLDER_CLIENTS = {
    "external", "unknown", "n/a", "na", "none", "internal",
    "unknown customer", "unknown client", "unknown company",
}


def normalize_company_name(name: str) -> str:
    """Lowercase, strip punctuation and legal/marketing suffix noise."""
    s = str(name or "").lower()
    for char in ".,-_'\"()/&":
        s = s.replace(char, " ")
    words = [w for w in s.split() if w and w not in _LEGAL_NOISE_TOKENS]
    return " ".join(words).strip()


def _is_contiguous_sublist(needle: List[str], haystack: List[str]) -> bool:
    n = len(needle)
    if not n or n > len(haystack):
        return False
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def is_placeholder_client(client_name: str) -> bool:
    norm = normalize_company_name(client_name)
    return not norm or len(norm) < 3 or norm in _PLACEHOLDER_CLIENTS


def is_same_company(company: str, client_name: str) -> bool:
    """True when `company` names the hiring client: exact normalized match,
    or one name appearing as a contiguous run of whole tokens in the other
    ("Meta" ⊂ "Meta Platforms", but "Meta" ⊄ "Metadata Solutions")."""
    if is_placeholder_client(client_name):
        return False
    comp_norm = normalize_company_name(company)
    if not comp_norm:
        return False
    client_norm = normalize_company_name(client_name)
    comp_tokens = comp_norm.split()
    client_tokens = client_norm.split()
    return (
        comp_tokens == client_tokens
        or _is_contiguous_sublist(client_tokens, comp_tokens)
        or _is_contiguous_sublist(comp_tokens, client_tokens)
    )


# LinkedIn headline shapes: "Senior Engineer at Google", "PM @ Stripe",
# "Data Engineer at Meta | ex-Amazon". The company chunk ends at the first
# separator; a leading "the" is dropped ("Engineer at the Home Depot").
_HEADLINE_AT_RE = re.compile(
    r"(?:\s+at\s+|\s+@\s*)(?P<company>[^|,;•·(–—]+)",
    flags=re.IGNORECASE,
)


def extract_company_from_headline(headline: str) -> str:
    """Best-effort CURRENT company from a LinkedIn-style headline/title.

    Uses the LAST "at X" / "@ X" occurrence — headlines like
    "Ex-Google | Engineer at Stripe" name the current employer last.
    Returns "" when no pattern matches; callers must treat "" as unknown
    (never as a match)."""
    text = str(headline or "").strip()
    if not text or len(text) > 300:
        return ""
    matches = list(_HEADLINE_AT_RE.finditer(text))
    if not matches:
        return ""
    company = matches[-1].group("company").strip()
    company = re.sub(r"^(the)\s+", "", company, flags=re.IGNORECASE)
    # Trim trailing decorations ("Google!", "Stripe 🚀")
    company = company.strip(" .!-–—")
    if not company or len(company.split()) > 6:
        return ""
    return company


def _entry_is_current(exp: Dict[str, Any]) -> bool:
    end_raw = str(
        exp.get("end_date") or exp.get("endDate") or exp.get("to") or exp.get("end") or ""
    ).strip()
    return (
        exp.get("is_current") is True
        or exp.get("current") is True
        or not end_raw
        or "present" in end_raw.lower()
        or "current" in end_raw.lower()
    )


def collect_current_companies(candidate: Dict[str, Any]) -> List[str]:
    """Every signal of the candidate's CURRENT employer available on a
    stored/sourced candidate row, across all sources:

      - flat fields (current_company / company / company_name),
      - current entries of company_experience (JobDiva/Unipile enrichment),
      - current entries of exa_recent_companies (Exa deep-search schema:
        {company, title, start, end}),
      - an "… at X" parse of the headline/title (Unipile & Exa search rows
        carry the employer only in headline text).
    """
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}

    companies: List[str] = []
    for c_str in [
        data.get("current_company"),
        enhanced.get("current_company"),
        data.get("company"),
        data.get("company_name"),
    ]:
        if c_str and str(c_str).strip():
            companies.append(str(c_str).strip())

    for exp_list in [
        data.get("company_experience") or enhanced.get("company_experience") or [],
        data.get("exa_recent_companies") or enhanced.get("exa_recent_companies") or [],
    ]:
        if not isinstance(exp_list, list):
            continue
        for exp in exp_list:
            if isinstance(exp, dict) and _entry_is_current(exp):
                comp = (
                    exp.get("company")
                    or exp.get("company_name")
                    or exp.get("employer")
                    or exp.get("name")
                )
                if comp and str(comp).strip():
                    companies.append(str(comp).strip())

    for headline_field in [data.get("headline"), data.get("title"), candidate.get("headline"), candidate.get("title")]:
        parsed = extract_company_from_headline(str(headline_field or ""))
        if parsed:
            companies.append(parsed)

    # Preserve order, drop duplicates
    seen = set()
    out = []
    for comp in companies:
        key = comp.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(comp)
    return out


def currently_employed_by_client(candidate: Dict[str, Any], client_name: str) -> Optional[str]:
    """The matching company string when any current-employer signal names
    the hiring client, else None."""
    if is_placeholder_client(client_name):
        return None
    for comp in collect_current_companies(candidate):
        if is_same_company(comp, client_name):
            return comp
    return None
