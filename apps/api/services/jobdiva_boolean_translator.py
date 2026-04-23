"""
jobdiva_boolean_translator.py
-----------------------------
Convert the frontend's human-readable Boolean string into the syntax
JobDiva's Talent Search actually understands.

Frontend emits strings like:
    "Databricks" AND "5+ years" AND ("Python" OR "Scala") NOT "Java"

JobDiva's Talent Search wants:
    "DATABRICKS" OVER 5 YRS AND ("PYTHON" OR "SCALA") NOT "JAVA"

The critical differences:
  - Years-of-experience: `"Databricks" AND "5+ years"` (two quoted terms)
    must become `"DATABRICKS" OVER 5 YRS` (one clause attached to the
    skill). A bare `"5+ years"` term is meaningless to JobDiva and
    silently degrades matches.
  - Term casing: JobDiva is case-insensitive but the conventional wire
    form is UPPERCASE — it also sidesteps a few quirks around
    lowercase operator collisions.
  - Date freshness: JobDiva supports `LASTMODIFIED > YYYY-MM-DD` and
    `LASTACTIVITY > YYYY-MM-DD` inline in the searchValue. Neither
    appears in the frontend boolean, so the caller passes a
    `recent_days` hint and we prepend it.

The translator is defensive: if the input doesn't parse cleanly it
returns the original string uppercased, so the search never breaks
outright.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import re
import logging

logger = logging.getLogger(__name__)

# Matches a quoted term plus an optional trailing AND "N+ years" clause.
# We capture the term (T) and the years integer (Y).
_QUOTED_YEARS_RE = re.compile(
    r'"(?P<term>[^"]+)"\s+AND\s+"(?P<years>\d+)\+\s*(?:years|yrs?)"',
    flags=re.IGNORECASE,
)

# Matches a bare "N+ years" clause that wasn't paired with a skill —
# we scrub these so they don't appear as literal quoted phrases in the
# JobDiva query (which would match ~nothing).
_BARE_YEARS_RE = re.compile(
    r'(?:\s+AND\s+|^)"\d+\+\s*(?:years|yrs?)"',
    flags=re.IGNORECASE,
)

# Matches "N years" / "N yrs" inside a single quoted phrase like
# "Databricks 5 years" — some frontends emit this form.
_COMBINED_TERM_YEARS_RE = re.compile(
    r'"([^"]+?)\s+(\d+)\+?\s*(?:years|yrs?)"',
    flags=re.IGNORECASE,
)


def _combine_term_and_years(match: re.Match) -> str:
    term = match.group("term").strip().upper()
    years = int(match.group("years"))
    return f'"{term}" OVER {years} YRS'


def _combine_inside_quote(match: re.Match) -> str:
    term = match.group(1).strip().upper()
    years = int(match.group(2))
    return f'"{term}" OVER {years} YRS'


def _uppercase_quoted_terms(text: str) -> str:
    """Uppercase every quoted term in the string."""
    return re.sub(r'"([^"]+)"', lambda m: f'"{m.group(1).strip().upper()}"', text)


def _normalize_operators(text: str) -> str:
    """Normalize AND/OR/NOT casing and collapse whitespace."""
    out = re.sub(r'\s+', ' ', text).strip()
    # Word-boundary replacements so we don't touch "AND" inside quotes.
    def replacer(m: re.Match) -> str:
        return m.group(0).upper()
    out = re.sub(r'\b(and|or|not)\b', replacer, out, flags=re.IGNORECASE)
    return out


def translate_for_jobdiva(
    boolean_str: str,
    *,
    skill_years: Optional[Dict[str, int]] = None,
    recent_days: Optional[int] = None,
) -> str:
    """
    Convert a frontend boolean into JobDiva Talent Search syntax.

    Args:
        boolean_str: the human-readable Boolean string from the wizard
            (e.g. '"Databricks" AND "5+ years" AND "Python"').
        skill_years: optional `{ "Databricks": 5, "Python": 3 }` map.
            Any skill in this map that appears as a bare quoted term
            in the input gets the matching `OVER N YRS` clause attached.
        recent_days: if set, prepend `LASTMODIFIED > <cutoff>` to
            limit results to candidates whose records were touched in
            the last N days. Pass None or 0 to skip.

    Returns:
        The translated query string ready for JobDiva's `searchValue`.
    """
    if not boolean_str or not boolean_str.strip():
        return ""

    translated = boolean_str.strip()

    # 1. `"X" AND "N+ years"` pattern → `"X" OVER N YRS`
    translated = _QUOTED_YEARS_RE.sub(_combine_term_and_years, translated)

    # 2. `"X 5 years"` pattern (years inside the quote) → `"X" OVER 5 YRS`
    translated = _COMBINED_TERM_YEARS_RE.sub(_combine_inside_quote, translated)

    # 3. Strip any remaining bare "N+ years" fragments — they're nonsense
    #    to JobDiva and would silently kill matches.
    translated = _BARE_YEARS_RE.sub('', translated)

    # 4. Apply skill_years map for skills that didn't already get a
    #    years clause. This handles the case where the frontend sends
    #    years as metadata instead of inlining them in the boolean.
    if skill_years:
        for skill_name, years in skill_years.items():
            if not skill_name or not years or years <= 0:
                continue
            # Only attach OVER if the skill is present AND doesn't
            # already have an OVER clause.
            pattern = re.compile(
                r'"' + re.escape(skill_name) + r'"(?!\s+OVER\s+\d+)',
                flags=re.IGNORECASE,
            )
            translated = pattern.sub(
                f'"{skill_name.upper()}" OVER {int(years)} YRS',
                translated,
                count=1,  # Only the first occurrence per skill.
            )

    # 5. Uppercase all remaining quoted terms.
    translated = _uppercase_quoted_terms(translated)

    # 6. Normalize operator casing and collapse whitespace.
    translated = _normalize_operators(translated)

    # 7. Clean up dangling operators left by step 3 (e.g. "AND AND", leading AND).
    translated = re.sub(r'\b(AND|OR|NOT)\s+(AND|OR|NOT)\b', r'\1', translated)
    translated = re.sub(r'^\s*(AND|OR)\s+', '', translated)
    translated = re.sub(r'\s+(AND|OR|NOT)\s*$', '', translated)
    translated = re.sub(r'\(\s*(AND|OR)\s+', '(', translated)
    translated = re.sub(r'\s+(AND|OR)\s*\)', ')', translated)
    translated = re.sub(r'\s+', ' ', translated).strip()

    # 8. Optional freshness filter.
    if recent_days and recent_days > 0:
        cutoff = (datetime.utcnow() - timedelta(days=recent_days)).strftime("%Y-%m-%d")
        freshness = f'LASTMODIFIED > {cutoff}'
        translated = f'({freshness}) AND ({translated})' if translated else freshness

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
