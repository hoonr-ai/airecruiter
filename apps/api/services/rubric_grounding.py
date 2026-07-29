"""
rubric_grounding.py
-------------------
JD-grounding helpers for the extracted rubric.

Everything here answers one of three questions about a term the LLM proposed,
using the job description itself as the source of truth:

  1. "Is this synonym real for THIS job?"  -> ground_skill_synonyms /
     mine_parenthetical_members. Turns a bare skill chip into the synonym
     cluster a recruiter would write by hand — "Adobe Creative Cloud" becomes
     (Adobe Creative Cloud OR Photoshop OR Illustrator OR InDesign) because the
     JD literally spells that out. The same gate rejects a hallucinated domain
     ("Telecom" on a healthcare JD) because nothing supports it in the text.

  2. "Is this actually required, or merely preferred?" -> classify_by_jd_section.
     JDs almost always carry explicit "Required Qualifications" /
     "Preferred Qualifications" headers, and the section a term appears under is
     far more reliable than the LLM's own guess. Observed on 26-22970: HTML,
     CSS and SharePoint were labelled `required` though the JD lists them only
     under Preferred, while eLearning content and storyboarding — both Key
     Responsibilities — came back `preferred`. Exactly inverted.

  3. "Is this similar title the same seniority?" -> seniority_level /
     seniority_compatible. Taxonomy siblings and LLM suggestions happily mix
     levels: "Creative Designer" pulled in "Global Creative Director",
     "Creative Department Head" and "Visual Design Intern". OR-ing those into a
     title group destroys precision.

Deliberately deterministic — no LLM calls, no network. Cheap enough to run on
every extraction.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from services.jobdiva_boolean_translator import term_appears_as_token


# ── Seniority ─────────────────────────────────────────────────────────────
# Ordered rungs. Index is the comparable level; None means "unmarked", which is
# treated as the individual-contributor band (most titles carry no marker).
_SENIORITY_BANDS: List[Tuple[int, Tuple[str, ...]]] = [
    (0, ("intern", "internship", "trainee", "apprentice")),
    (1, ("junior", "jr", "entry level", "entry-level", "associate")),
    (2, ("mid", "mid level", "mid-level", "intermediate")),
    (3, ("senior", "sr", "specialist iii", "iii")),
    (4, ("lead", "principal", "staff", "architect")),
    (5, ("manager", "supervisor")),
    (6, ("head", "director", "chief", "vp", "vice president", "president", "cxo", "officer")),
]

# The unmarked band. "Graphic Designer" and "Senior Graphic Designer" are one
# rung apart; "Graphic Designer" and "Creative Director" are four.
_UNMARKED_LEVEL = 2


def seniority_level(title: str) -> int:
    """Coarse seniority rung for a title. Unmarked titles get the IC band.

    Highest matching band wins, so "Senior Director" reads as director rather
    than senior.
    """
    text = f" {str(title or '').lower().strip()} "
    text = re.sub(r"[^a-z0-9]+", " ", text)
    best = None
    for level, markers in _SENIORITY_BANDS:
        for marker in markers:
            if f" {marker} " in text:
                best = level if best is None else max(best, level)
                break
    return _UNMARKED_LEVEL if best is None else best


def seniority_compatible(base_title: str, candidate: str, *, tolerance: int = 1) -> bool:
    """True when `candidate` sits within `tolerance` rungs of `base_title`.

    Default tolerance of 1 keeps the natural neighbours ("Graphic Designer" ↔
    "Senior Graphic Designer") while dropping the jumps that made title groups
    useless — an intern or a global director is not an alternative for a
    mid-level contract designer.
    """
    return abs(seniority_level(base_title) - seniority_level(candidate)) <= max(0, tolerance)


# ── JD grounding ──────────────────────────────────────────────────────────

def _has_term(text: str, term: str) -> bool:
    return term_appears_as_token(term, text)


def mine_parenthetical_members(skill: str, jd_text: str, *, max_members: int = 6) -> List[str]:
    """Members a JD spells out in parentheses right after an umbrella skill.

    `"Adobe Creative Cloud (Photoshop, Illustrator, InDesign)"` -> the three
    tools. This is the highest-precision synonym source available: the JD author
    explicitly equated them, which is exactly why a recruiter writes
    `("ADOBE CREATIVE CLOUD" OR PHOTOSHOP OR ILLUSTRATOR OR INDESIGN)`.
    """
    base = str(skill or "").strip()
    text = str(jd_text or "")
    if not base or not text:
        return []
    pattern = re.compile(
        re.escape(base) + r"\s*\(([^)]{2,200})\)", flags=re.IGNORECASE
    )
    members: List[str] = []
    seen = set()
    for match in pattern.finditer(text):
        for chunk in re.split(r"[,/;]| and | or ", match.group(1)):
            member = chunk.strip().strip(".").strip()
            # Drop prose fragments and anything that is really a sentence.
            if not member or len(member) > 40 or len(member.split()) > 4:
                continue
            key = member.lower()
            if key == base.lower() or key in seen:
                continue
            seen.add(key)
            members.append(member)
            if len(members) >= max_members:
                return members
    return members


_LIST_SPLIT_RE = re.compile(r",| and | or |/|;")
# A plausible tool/skill name: 1-3 words, each starting uppercase or all-caps,
# optionally carrying +/#/. ("After Effects", "Adobe Captivate", "HTML", "C++").
_PROPER_TERM_RE = re.compile(r"^(?:[A-Z][A-Za-z0-9+#.\-]*)(?: [A-Z][A-Za-z0-9+#.\-]*){0,2}$")


def mine_list_siblings(skill: str, jd_text: str, *, max_members: int = 5) -> List[str]:
    """Sibling tools named in the same enumeration as `skill`.

    JDs group interchangeable tools in one breath — *"Experience with Articulate
    Storyline, Adobe Captivate, After Effects … or Maya"* — which is precisely
    the reasoning behind a recruiter's `(ARTICULATE OR STORYLINE OR CAPTIVATE OR
    ELEARNING)` cluster: any one of them evidences the same capability. Members
    are kept only if they look like proper tool names, so surrounding prose
    ("Experience with", "Knowledge of") is not mistaken for a skill.
    """
    base = str(skill or "").strip()
    text = str(jd_text or "")
    if not base or not text:
        return []
    out: List[str] = []
    seen = {base.lower()}
    for line in re.split(r"[.\n]", text):
        if not term_appears_as_token(base, line):
            continue
        for chunk in _LIST_SPLIT_RE.split(line):
            member = chunk.strip().strip(".,;:").strip()
            # Strip leading connective prose so "Experience with Articulate
            # Storyline" yields the tool, not the sentence.
            member = re.sub(
                r"^(?:experience|experienced|knowledge|proficiency|skilled|strong|familiarity)"
                r"\s+(?:with|in|of)\s+", "", member, flags=re.IGNORECASE
            ).strip()
            key = member.lower()
            if not member or key in seen or not _PROPER_TERM_RE.match(member):
                continue
            seen.add(key)
            out.append(member)
            if len(out) >= max_members:
                return out
    return out


# Sentence-initial prose that survives the proper-name shape test ("Build and
# maintain resource libraries using SharePoint" → "Build"). Not a general
# stopword list — only words that plausibly start a JD bullet.
_PROSE_LEAD_WORDS = frozenset({
    "build", "create", "design", "develop", "produce", "partner", "maintain",
    "deploy", "edit", "manage", "support", "experience", "knowledge",
    "proficiency", "familiarity", "strong", "portfolio", "understanding",
    "ability", "responsibilities", "qualifications", "required", "preferred",
    "years", "work", "team", "other", "duties", "including", "etc",
})


def ground_skill_synonyms(
    skill: str,
    candidates: Iterable[str],
    jd_text: str,
    *,
    max_results: int = 5,
    peers: Optional[Iterable[str]] = None,
) -> List[str]:
    """Keep only the proposed synonyms the JD itself supports.

    A candidate survives when it appears in the JD as a whole token. That single
    rule is what makes the expansion trustworthy: every OR'd alternative is a
    term the job actually mentions, so the group can only widen recall along
    axes the JD named — never into an unrelated tool the model free-associated.

    Parenthetical members (see `mine_parenthetical_members`) are folded in
    first because the JD equated them explicitly.
    """
    base = str(skill or "").strip()
    text = str(jd_text or "")
    if not base or not text:
        return []

    out: List[str] = []
    seen = {base.lower()}
    # Peers are the job's OTHER skill chips. A peer found beside `skill` in the
    # JD is a sibling requirement, not a synonym for it — "2-3 years in Creative
    # Design, Graphic Design, or Instructional Design" would otherwise make each
    # of those a stand-in for the others, so a candidate with only one would
    # satisfy all three. They already have their own chips.
    peer_keys = {str(p or "").strip().lower() for p in (peers or []) if str(p or "").strip()}
    peer_keys.discard(base.lower())

    def _take(term: str) -> None:
        value = str(term or "").strip().strip(".,;/")
        key = value.lower()
        if not value or key in seen or key in peer_keys or len(value) > 40:
            return
        if key in _PROSE_LEAD_WORDS:
            return
        if not _has_term(text, value):
            return
        seen.add(key)
        out.append(value)

    # Precision order: an explicit parenthetical equation beats an LLM
    # suggestion, which beats a same-list sibling.
    for member in mine_parenthetical_members(base, text):
        _take(member)
        if len(out) >= max_results:
            return out
    for cand in candidates or []:
        if isinstance(cand, str):
            _take(cand)
        if len(out) >= max_results:
            return out
    for sibling in mine_list_siblings(base, text):
        _take(sibling)
        if len(out) >= max_results:
            break
    return out[:max_results]


def is_grounded_term(value: str, jd_text: str) -> bool:
    """Whether a standalone rubric term (e.g. an extracted domain) is supported
    by the JD at all.

    Guards against confident hallucinations: 26-22970 came back with domain
    "Telecom" for a healthcare-finance JD, and because domain terms are AND'ed
    into the sourcing query a single bogus one zeroes the whole search.
    Multi-word values pass when any of their significant words is present, so
    "Healthcare Finance" still matches a JD that only says "healthcare".
    """
    text = str(jd_text or "")
    term = str(value or "").strip()
    if not text or not term:
        return False
    if _has_term(text, term):
        return True
    words = [w for w in re.split(r"[^A-Za-z0-9+#.]+", term) if len(w) > 3]
    return any(_has_term(text, w) for w in words)


# ── Required vs Preferred, from the JD's own sections ─────────────────────
# Header cues. "Responsibilities" counts as required: what the person will be
# doing every day is a requirement even when the qualifications list omits it.
_REQUIRED_HEADERS = (
    "required qualification", "requirements", "required skills", "must have",
    "minimum qualification", "basic qualification", "key responsibilit",
    "responsibilities", "duties", "what you'll do", "what you will do",
)
_PREFERRED_HEADERS = (
    "preferred qualification", "preferred skills", "nice to have",
    "nice-to-have", "desired", "bonus", "plus", "good to have",
)


def _split_sections(jd_text: str) -> List[Tuple[Optional[str], str]]:
    """Split a JD into (kind, body) chunks where kind is 'required',
    'preferred' or None. Header detection is line-based and forgiving of the
    ``Header:`` / ``**Header**`` / bare-line styles JDs actually use."""
    lines = str(jd_text or "").splitlines()
    sections: List[Tuple[Optional[str], List[str]]] = [(None, [])]
    for line in lines:
        probe = re.sub(r"[^a-z ']+", " ", line.lower()).strip()
        probe = re.sub(r"\s+", " ", probe)
        kind: Optional[str] = None
        # Only short lines are treated as headers, so a sentence that merely
        # contains "requirements" doesn't start a section.
        if probe and len(probe.split()) <= 6:
            if any(h in probe for h in _PREFERRED_HEADERS):
                kind = "preferred"
            elif any(h in probe for h in _REQUIRED_HEADERS):
                kind = "required"
        if kind:
            sections.append((kind, []))
        else:
            sections[-1][1].append(line)
    return [(kind, "\n".join(body)) for kind, body in sections]


def classify_by_jd_section(term: str, jd_text: str) -> Optional[str]:
    """'required' | 'preferred' | None for a term, judged by the JD section(s)
    that name it.

    Returns None when the JD never names the term (an inferred skill) or when
    the JD has no usable section headers — callers keep the LLM's own label in
    that case, so this only ever overrides on real evidence.

    Required wins ties: a term in both lists is genuinely needed, and the
    preferred mention is just elaboration.
    """
    value = str(term or "").strip()
    text = str(jd_text or "")
    if not value or not text:
        return None
    found_required = False
    found_preferred = False
    for kind, body in _split_sections(text):
        if not kind or not body.strip():
            continue
        if _has_term(body, value):
            if kind == "required":
                found_required = True
            else:
                found_preferred = True
    if found_required:
        return "required"
    if found_preferred:
        return "preferred"
    return None


def skill_rank_key(skill: Dict, jd_text: str = "") -> Tuple[int, int, int]:
    """Sort key that puts the skills a recruiter would AND first.

    (importance, evidence, first mention) — required before preferred, directly
    stated before inferred, earlier in the JD before later. Without this the
    downstream boolean cap falls back to whatever order the extractor emitted,
    which is alphabetical: on 26-22970 that made CSS and HTML hard requirements
    while "Instructional Design" — the JD's headline ask — got demoted.
    """
    importance = str(skill.get("importance") or skill.get("required") or "").lower()
    evidence = str(skill.get("evidence_type") or "").lower()
    value = str(skill.get("value") or "")
    position = 10**6
    if value and jd_text:
        match = re.search(re.escape(value), jd_text, flags=re.IGNORECASE)
        if match:
            position = match.start()
    return (
        0 if importance.startswith("required") else 1,
        0 if evidence == "direct" else 1,
        position,
    )
