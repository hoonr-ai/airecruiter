"""Company-name matching for the "employed by the hiring client" exclusion.

Used in three places with the SAME semantics so search-time filtering, the
candidate-list flag and launch-time gating can't disagree:

  - sourcing (services/unified_candidate_search.py, services/unipile.py,
    services/exa_service.py): drop Exa + Unipile results whose CURRENT
    company is the hiring client, per the hard requirement that we never
    source a client's own employees;
  - candidate list (`apply_client_conflict_flag`, stamped at the emit
    choke-point in unified_candidate_search.finalize_candidate): the row
    stays VISIBLE but greyed out and unselectable, carrying the reason, so a
    recruiter can see why the person is off-limits instead of the row
    silently vanishing;
  - launch (routers/engagement.py `is_candidate_excluded_from_pair`): final
    server-side gate before PAIR outreach.

"Employed by" means employed TODAY. `employed_by_client` consults the last
employer only as a FALLBACK, when no current-employer signal exists at all —
many source rows carry a history with no "Present" entry, and there the most
recent employer is the best available answer. Someone who left the client for
a named current employer stays contactable.

Matching is deliberately token-based, not substring-based: client "Meta"
must match "Meta Platforms" but never "Metadata Solutions".
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
    return not norm or norm in _PLACEHOLDER_CLIENTS


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


def client_appears_in_text(text: str, client_name: str) -> bool:
    """One-directional containment for NOISY free-text employer lines
    (JobDiva CandidatesProfileDetail EXPERIENCE.DETAILS — resume fragments
    with structured dates, attached at launch as jobdiva_profile_experience).

    True only when the client's full token run appears contiguously in the
    text ("Bank of America" ⊂ "SAS Programmer | Bank of America, Charlotte").
    Deliberately NEVER the reverse of is_same_company: a fragment like
    "India" must not match a client whose name merely contains that word —
    these lines are sentences, not company names, so text-⊂-client would
    manufacture conflicts out of stray words.
    """
    if is_placeholder_client(client_name):
        return False
    text_tokens = normalize_company_name(text).split()
    client_tokens = normalize_company_name(client_name).split()
    if not text_tokens or not client_tokens:
        return False
    return _is_contiguous_sublist(client_tokens, text_tokens)


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


def _entry_end_raw(exp: Dict[str, Any]) -> str:
    return str(
        exp.get("end_date") or exp.get("endDate") or exp.get("to") or exp.get("end") or ""
    ).strip()


def _current_entries(exp_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The entries of one experience list that describe employment TODAY.

    List-aware tightening of `_entry_is_current` (kept above for its existing
    importers): that helper reads ANY entry with a missing end date as
    current, so an LLM omission on a decade-old job manufactured a "current
    employer" and a false client-conflict/no-contact hit. Here:

      - explicit markers always win (is_current/current True, or an end text
        containing "present"/"current"); explicit False always loses;
      - a missing end date counts as current ONLY for the FIRST entry —
        every source emits reverse-chronologically, so an undated entry
        deeper in the list is an unknown, not an ongoing job.
    """
    entries = [e for e in exp_list if isinstance(e, dict)]
    out: List[Dict[str, Any]] = []
    for idx, exp in enumerate(entries):
        if exp.get("is_current") is False or exp.get("current") is False:
            continue
        end_raw = _entry_end_raw(exp).lower()
        if (
            exp.get("is_current") is True
            or exp.get("current") is True
            or "present" in end_raw
            or "current" in end_raw
            or (not end_raw and idx == 0)
        ):
            out.append(exp)
    return out


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
        for exp in _current_entries(exp_list):
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


def collect_current_end_clients(candidate: Dict[str, Any]) -> List[str]:
    """END-CLIENT signals from CURRENT experience entries only: the company a
    consultant is placed AT, when the resume names both an employer of record
    and the client it serves ("Client: Walmart", "TCS – deployed at Walmart").
    The extraction prompt writes these as `end_client` on company_experience
    entries. Current-only by design — a finished project at a company is not
    a placement conflict, but someone on-site at the client TODAY is, even
    though their employer of record is a vendor."""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}

    clients: List[str] = []
    for exp_list in [
        data.get("company_experience") or enhanced.get("company_experience") or [],
        data.get("exa_recent_companies") or enhanced.get("exa_recent_companies") or [],
    ]:
        if not isinstance(exp_list, list):
            continue
        for exp in _current_entries(exp_list):
            ec = exp.get("end_client") or exp.get("endClient")
            if ec and str(ec).strip():
                clients.append(str(ec).strip())

    seen = set()
    out = []
    for comp in clients:
        key = comp.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(comp)
    return out


# ── last (most recent past) employer ──────────────────────────────────────
#
# Lives here rather than in no_contact.py because BOTH policies now need it:
# the no-contact keyword list and the hiring-client exclusion each block on
# a candidate's current OR last employer. no_contact.py re-exports these
# names so its own import path keeps working.

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NUM_MONTH_RE = re.compile(r"\b(19|20)\d{2}[-/](\d{1,2})\b")


def _end_sort_key(exp: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """(year, month) parsed from an experience entry's end date, else None.

    Handles the shapes seen across sources: "2024-05", "05/2024" (month first
    is NOT assumed — only year-first numeric forms parse a month), "May 2024",
    "2024". Unparseable → None (caller falls back to list order).
    """
    end_raw = str(
        exp.get("end_date") or exp.get("endDate") or exp.get("to") or exp.get("end") or ""
    ).strip().lower()
    if not end_raw:
        return None
    year_m = _YEAR_RE.search(end_raw)
    if not year_m:
        return None
    year = int(year_m.group(0))
    month = 0
    num_m = _NUM_MONTH_RE.search(end_raw)
    if num_m and 1 <= int(num_m.group(2)) <= 12:
        month = int(num_m.group(2))
    else:
        for name, idx in _MONTHS.items():
            if name in end_raw:
                month = idx
                break
    return (year, month)


def _experience_lists(candidate: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}
    lists = []
    for exp_list in [
        data.get("company_experience") or enhanced.get("company_experience") or [],
        data.get("exa_recent_companies") or enhanced.get("exa_recent_companies") or [],
    ]:
        if isinstance(exp_list, list) and exp_list:
            lists.append([e for e in exp_list if isinstance(e, dict)])
    return lists


def collect_last_companies(candidate: Dict[str, Any]) -> List[str]:
    """The candidate's LAST employer signals: flat previous-company fields,
    plus the most recent non-current entry of each experience list (by parsed
    end date; entries without dates fall back to list order, which every
    source emits reverse-chronologically)."""
    data = candidate.get("data") if isinstance(candidate.get("data"), dict) else candidate
    enhanced = data.get("enhanced_info") if isinstance(data.get("enhanced_info"), dict) else {}

    companies: List[str] = []
    for c_str in [
        data.get("previous_company"),
        data.get("last_company"),
        enhanced.get("previous_company"),
        enhanced.get("last_company"),
    ]:
        if c_str and str(c_str).strip():
            companies.append(str(c_str).strip())

    for exp_list in _experience_lists(candidate):
        current_ids = {id(e) for e in _current_entries(exp_list)}
        past = [e for e in exp_list if id(e) not in current_ids]
        if not past:
            continue
        dated = [(key, e) for e in past if (key := _end_sort_key(e)) is not None]
        top = max(dated, key=lambda pair: pair[0])[1] if dated else past[0]
        comp = (
            top.get("company")
            or top.get("company_name")
            or top.get("employer")
            or top.get("name")
        )
        if comp and str(comp).strip():
            companies.append(str(comp).strip())

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
    """The matching company string when any CURRENT-employer signal names
    the hiring client, else None.

    Current-only by design: this is the search-time hard filter for external
    sources, which DROPS the row outright (services/unified_candidate_search.py
    `_drop_client_employees`). Dropping an ex-employee would hide someone we
    still want a recruiter to see — the last-employer case is flagged instead,
    via `employed_by_client` below.
    """
    if is_placeholder_client(client_name):
        return None
    for comp in collect_current_companies(candidate):
        if is_same_company(comp, client_name):
            return comp
    # Placement counts too: a consultant on-site at the client through a
    # vendor is the client's worker for sourcing purposes.
    for comp in collect_current_end_clients(candidate):
        if is_same_company(comp, client_name):
            return comp
    return None


def employed_by_client(
    candidate: Dict[str, Any], client_name: str
) -> Optional[Dict[str, str]]:
    """{"company", "relation": "current"|"last"} when the candidate works at the
    hiring client TODAY, else None.

    The bar is still present employment. The last employer is consulted ONLY as
    a fallback, when no current-employer signal exists at all — plenty of source
    rows carry an employment history with no entry marked "Present", and there
    the most recent employer is the best available answer to "where do they work
    now?". It is a fallback, never a union:

      current = Stripe, last = the client  → NOT a conflict. We know where they
                                             work today, and it isn't the client.
      current = (none),  last = the client  → conflict, relation "last".

    So someone who genuinely left the client and has since joined a named
    employer stays contactable, which is the intended behaviour — only the
    people we cannot distinguish from present employees get flagged.
    """
    if is_placeholder_client(client_name):
        return None
    current = collect_current_companies(candidate)
    for comp in current:
        if is_same_company(comp, client_name):
            return {"company": comp, "relation": "current"}
    # Placement: currently working AT the client through a vendor/employer of
    # record. A named current employer does NOT clear this rung — employed by
    # TCS, deployed at the client, is still on-site at the client today.
    for comp in collect_current_end_clients(candidate):
        if is_same_company(comp, client_name):
            return {"company": comp, "relation": "placement"}
    if current:
        # A current employer is on file and it is not the client — done.
        return None
    for comp in collect_last_companies(candidate):
        if is_same_company(comp, client_name):
            return {"company": comp, "relation": "last"}
    return None


def apply_client_conflict_flag(candidate: Dict[str, Any], client_name: str) -> bool:
    """Recompute and stamp `client_conflict` / `client_conflict_reason` /
    `client_conflict_company` / `client_conflict_relation`. Returns the flag.

    Authoritative in BOTH directions so read paths self-heal: a row that starts
    matching after the job's customer is corrected gets flagged, and a stale
    flag clears when the candidate is viewed against a different job — the
    conflict is per-job, unlike the global no-contact list, so a flag left
    behind from another req would be wrong.

    Never raises: a matcher error leaves the existing flag untouched rather
    than failing the search.
    """
    try:
        hit = employed_by_client(candidate, client_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"client-conflict check failed (flag left as-is): {exc}")
        return bool(candidate.get("client_conflict"))
    if hit:
        candidate["client_conflict"] = True
        candidate["client_conflict_reason"] = client_conflict_reason(hit, client_name)
        candidate["client_conflict_company"] = hit["company"]
        candidate["client_conflict_relation"] = hit["relation"]
        return True
    if candidate.get("client_conflict"):
        candidate["client_conflict"] = False
        candidate.pop("client_conflict_reason", None)
        candidate.pop("client_conflict_company", None)
        candidate.pop("client_conflict_relation", None)
    return False


def client_conflict_reason(hit: Dict[str, str], client_name: str) -> str:
    """Human-readable reason string for a `employed_by_client` hit.

    One helper so the flag stamped at search time and the reason shown at the
    launch gate can never drift apart.
    """
    if hit.get("relation") == "current":
        return (
            f"Current employer '{hit.get('company')}' is the hiring client "
            f"({client_name})"
        )
    if hit.get("relation") == "placement":
        return (
            f"Currently placed at the hiring client ({client_name}) — "
            f"end client '{hit.get('company')}'"
        )
    return (
        f"No current employer on file; most recent employer "
        f"'{hit.get('company')}' is the hiring client ({client_name})"
    )
