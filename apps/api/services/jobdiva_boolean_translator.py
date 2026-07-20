"""
jobdiva_boolean_translator.py
-----------------------------
Boolean-string helpers for JobDiva Talent Search.

Since the v2 payload fix (2026-07-19), production NO LONGER sends boolean
strings to TalentSearch at all — the endpoint takes a plain array of AND'd
terms (`extract_and_terms` / `sanitize_talent_term` below produce it from
a wizard boolean). OR / NOT / years clauses cannot be expressed
server-side and stay client-side in the scorer.

The legacy rewrites (`translate_for_jobdiva`'s `OVER N YRS` years dialect,
`rewrite_location_clauses_to_zip_dialect`) are retained for the probe
scripts and back-compat callers only.
"""

from __future__ import annotations

from typing import Dict, List, Optional
import re
import logging

logger = logging.getLogger(__name__)

# `"X" AND "N+ years"` (two quoted terms) → one `OVER N YRS` clause.
_QUOTED_YEARS_RE = re.compile(
    r'"(?P<term>[^"]+)"\s+AND\s+"(?P<years>\d+)\+?\s*(?:years|yrs?)"',
    flags=re.IGNORECASE,
)

# `"X N years"` (years inside the same quote) → `"X" OVER N YRS`.
_COMBINED_TERM_YEARS_RE = re.compile(
    r'"([^"]+?)\s+(\d+)\+?\s*(?:years|yrs?)"',
    flags=re.IGNORECASE,
)


def _combine_term_and_years(match: re.Match) -> str:
    term = match.group("term").strip()
    years = int(match.group("years"))
    return f'"{term}" OVER {years} YRS'


def _combine_inside_quote(match: re.Match) -> str:
    term = match.group(1).strip()
    years = int(match.group(2))
    return f'"{term}" OVER {years} YRS'


_OVER_YRS_CLAUSE_RE = re.compile(r"\s*OVER\s+\d+\s+YRS\b", flags=re.IGNORECASE)

# Frontend location clause: `"Tempe, AZ 85281" within 25 mi` (optionally
# preceded by AND). Captures the quoted phrase and the radius.
_LOCATION_CLAUSE_RE = re.compile(
    r'(?:\s+AND\s+)?"(?P<phrase>[^"]+)"\s+within\s+(?P<miles>\d+)\s*mi\b',
    flags=re.IGNORECASE,
)


def count_location_clauses(boolean_str: str) -> int:
    """Number of `"..." within N mi` location clauses in the boolean.

    Step 5 OR-joins multiple location chips into the boolean while the
    structured request only carries chip #1 — callers use this to avoid
    anchoring a server-side zip radius to one chip of a multi-location
    search."""
    if not boolean_str or '"' not in boolean_str:
        return 0
    return len(_LOCATION_CLAUSE_RE.findall(boolean_str))


def rewrite_location_clauses_to_zip_dialect(boolean_str: str) -> str:
    """Rewrite `"City, ST ZIP" within N mi` clauses into JobDiva's native
    boolean geo dialect: `Within N miles of ZIP`.

    The quoted form is a *keyword* to JobDiva — it only matches resumes
    containing the literal string — while the dialect form is resolved as a
    real geo filter (per scripts/jobdiva_mainframe_search.py field notes).
    Clauses whose quoted phrase carries no zip fall back to the zip nearest
    the city's centroid via the offline zip index; if that fails too, the
    clause is left untouched.

    Only called when core.sourcing_config.JOBDIVA_BOOLEAN_ZIP_DIALECT_ENABLED
    is set — validate with scripts/jobdiva_zip_radius_probe.py first.
    """
    if not boolean_str or '"' not in boolean_str:
        return boolean_str

    from services import zip_index

    def _rewrite(match: re.Match) -> str:
        phrase = match.group("phrase").strip()
        miles = int(match.group("miles"))
        parts = [p.strip() for p in phrase.split(",") if p.strip()]

        # State hint from the phrase ("Tempe, AZ 85281" → AZ) to reject
        # street-number/zip collisions ("10001 W Main St, Mesa, AZ" must
        # not become "Within 25 miles of 10001" — that's Manhattan).
        state_hint = None
        try:
            from services.us_state_index import resolve_state_code
            for part in reversed(parts[1:]):
                first_token = part.split()[0] if part.split() else ""
                state_hint = resolve_state_code(first_token)
                if state_hint:
                    break
        except Exception:
            state_hint = None

        zip5 = zip_index.extract_zip(phrase)
        if zip5 and state_hint:
            entry = zip_index.lookup_zip(zip5)
            if entry and entry["state"].upper() != state_hint.upper():
                zip5 = None
        if not zip5 and len(parts) >= 2:
            zip5 = zip_index.city_state_default_zip(parts[0], parts[1].split()[0])
        if not zip5:
            return match.group(0)
        prefix = " AND " if match.group(0).lstrip().upper().startswith("AND") else " "
        return f"{prefix}Within {miles} miles of {zip5}"

    rewritten = _LOCATION_CLAUSE_RE.sub(_rewrite, boolean_str)
    return re.sub(r"\s+", " ", rewritten).strip()


def translate_for_jobdiva(
    boolean_str: str,
    *,
    skill_years: Optional[Dict[str, int]] = None,
    recent_days: Optional[int] = None,  # accepted for back-compat; ignored
) -> str:
    """
    Apply the years-experience rewrites and return the string.

    Everything else (operator casing, parentheses, term casing, ordering)
    is preserved exactly as the caller passed it in.

    `skill_years` is honored for skills that didn't already get an inline
    `... AND "N+ years"` clause — this lets the frontend ship the years
    as metadata rather than inlined text.

    `recent_days` is accepted to avoid breaking existing call sites but
    is no longer applied here. Freshness, when reintroduced, belongs in
    a structured `talentSearchDef` field, not in string mutation.

    core.sourcing_config.STRIP_YEARS_FROM_BOOLEAN: when set, every
    `OVER N YRS` clause (both the rewritten ones and any the caller
    inlined) is removed before returning. JobDiva's server-side YOE
    parse is unreliable; deferring YOE entirely to our scorer surfaces
    more real candidates and lets borderline matches degrade gracefully
    instead of disappearing.
    """
    if not boolean_str or not boolean_str.strip():
        return ""

    translated = boolean_str.strip()
    translated = _QUOTED_YEARS_RE.sub(_combine_term_and_years, translated)
    translated = _COMBINED_TERM_YEARS_RE.sub(_combine_inside_quote, translated)

    if skill_years:
        for skill_name, years in skill_years.items():
            if not skill_name or not years or years <= 0:
                continue
            pattern = re.compile(
                r'"' + re.escape(skill_name) + r'"(?!\s+OVER\s+\d+)',
                flags=re.IGNORECASE,
            )
            translated = pattern.sub(
                f'"{skill_name}" OVER {int(years)} YRS',
                translated,
                count=1,
            )

    # Local import so the diagnostic can monkey-patch sourcing_config
    # between runs without restarting the interpreter.
    from core import sourcing_config
    if sourcing_config.STRIP_YEARS_FROM_BOOLEAN:
        translated = _OVER_YRS_CLAUSE_RE.sub("", translated)
        translated = re.sub(r"\s+", " ", translated).strip()

    return translated


# NOT groups: `NOT ("A" OR "B")` or `NOT "A"`.
_NOT_GROUP_RE = re.compile(r'\bNOT\s*(?:\([^)]*\)|"[^"]*")', flags=re.IGNORECASE)
# Parenthesized OR groups: `("A" OR "B" ...)`.
_PAREN_GROUP_RE = re.compile(r"\([^()]*\)")
_QUOTED_TERM_RE = re.compile(r'"([^"]+)"')
_YEARS_TERM_RE = re.compile(r"^\d+\+?\s*(?:years?|yrs?)(?:\s+.*)?$", flags=re.IGNORECASE)
_OPERATOR_TOKENS = {"and", "or", "not", "within", "mi", "miles"}


def sanitize_talent_term(term: str) -> str:
    """One plain keyword for the v2 TalentSearch `skills` array.

    The v2 endpoint does NOT parse boolean syntax — a term containing
    quotes/operators makes the server error out mid-response (probe S2,
    2026-07-19) — so terms must be bare phrases. Returns "" for terms
    that cannot be safely sent (operator words, years clauses, numbers).
    """
    cleaned = re.sub(r'["()]', " ", str(term or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or len(cleaned) > 60:
        return ""
    if cleaned.lower() in _OPERATOR_TOKENS:
        return ""
    if cleaned.replace("+", "").replace(".", "").isdigit():
        return ""
    if _YEARS_TERM_RE.match(cleaned):
        return ""
    return cleaned


def extract_and_terms(boolean_str: str, max_terms: int = 4) -> List[str]:
    """Top-level AND-connected quoted terms from a wizard boolean, for the
    v2 TalentSearch `skills` array (which ANDs its elements server-side).

    Deliberately conservative — the server has no OR, so anything we can't
    express as a required term is DROPPED rather than over-restricting:
      - NOT groups are removed (exclusions stay client-side in the scorer),
      - `"City, ST ZIP" within N mi` location clauses are removed (geo goes
        in the structured zipCode/withinMiles/states fields),
      - years clauses are removed (YOE is scored client-side),
      - parenthesized/naked OR groups are skipped entirely (requiring one
        alternative would silently exclude the others).
    """
    if not boolean_str or '"' not in boolean_str:
        return []
    text = _NOT_GROUP_RE.sub(" ", boolean_str)
    text = _LOCATION_CLAUSE_RE.sub(" ", text)
    text = _QUOTED_YEARS_RE.sub(_combine_term_and_years, text)
    text = _COMBINED_TERM_YEARS_RE.sub(_combine_inside_quote, text)
    text = _OVER_YRS_CLAUSE_RE.sub(" ", text)
    # Drop parenthesized groups that contain OR; unwrap the rest.
    def _drop_or_groups(match: re.Match) -> str:
        inner = match.group(0)
        return " " if re.search(r"\bOR\b", inner, flags=re.IGNORECASE) else inner.strip("()")
    prev = None
    while prev != text:
        prev = text
        text = _PAREN_GROUP_RE.sub(_drop_or_groups, text)

    terms: List[str] = []
    seen = set()
    for fragment in re.split(r"\bAND\b", text, flags=re.IGNORECASE):
        if re.search(r"\bOR\b", fragment, flags=re.IGNORECASE):
            continue  # naked OR alternatives — same drop rule as groups
        for quoted in _QUOTED_TERM_RE.findall(fragment):
            term = sanitize_talent_term(quoted)
            key = term.lower()
            if not term or key in seen:
                continue
            seen.add(key)
            terms.append(term)
            if len(terms) >= max_terms:
                return terms
    return terms


def extract_skill_years(
    skills_with_years: List[Dict[str, int]],
) -> Dict[str, int]:
    """
    Helper: turn the frontend's `[{ value, minYears }, ...]` payload
    into the `{ skill: years }` map `translate_for_jobdiva` wants.
    """
    out: Dict[str, int] = {}
    for s in skills_with_years or []:
        if not isinstance(s, dict):
            continue
        name = s.get("value") or s.get("name") or ""
        years = s.get("minYears") or s.get("min_years") or 0
        if name and int(years or 0) > 0:
            out[str(name).strip()] = int(years)
    return out
