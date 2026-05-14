"""
jobdiva_boolean_translator.py
-----------------------------
Years-of-experience translator for JobDiva Talent Search.

The user-authored boolean is sent to JobDiva verbatim — JobDiva already
understands AND / OR / NOT and quoted terms natively. The only rewrite
this module performs is years-of-experience: JobDiva expects the
`OVER N YRS` dialect attached to a skill, but recruiters write
`"Skill" AND "5+ years"` or `"Skill 5 years"`.

  "Databricks" AND "5+ years"  → "Databricks" OVER 5 YRS
  "Python 5 years"             → "Python" OVER 5 YRS

Anything else (operators, casing, parentheses, freeform terms) is
preserved as the user wrote it.
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
