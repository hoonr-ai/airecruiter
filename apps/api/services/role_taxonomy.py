"""
Role taxonomy lookup.

Loads `apps/api/data/job_role_taxonomy.json` (17k roles × 9-level hierarchy)
once at module import and exposes two helpers:

    expand_title(base_title) -> list of similar/related titles with relevance tier
    compare(title_a, title_b) -> "exact" | "similar" | "related" | "none"

Replaces the LLM-driven similar-titles expansion in azure_agent_service. Same
data lives in Postgres `roles_master`; this module reads the JSON to avoid a
DB round-trip on the hot path (rubric extraction and per-candidate scoring).

Hierarchy levels, most specific → broadest:
    K17000  exact role
    K10000  near-identical role
    K5000   tight family
    K1500   family
    K1000   broader family
    K500    class
    K150    broader class
    K50     category
    K10     top-level category

Relevance tiers map to shared depth:
    exact    same K17000
    similar  same K10000, K5000, or K1500
    related  same K1000 or K500
    none     diverge at K150 or above
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from functools import lru_cache
from typing import Literal

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

Relevance = Literal["exact", "similar", "related", "none"]

_TAXONOMY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "job_role_taxonomy.json",
)

_LEVELS = ("ROLE_K17000", "ROLE_K10000", "ROLE_K5000", "ROLE_K1500", "ROLE_K1000", "ROLE_K500", "ROLE_K150", "ROLE_K50", "ROLE_K10")

# Populated at module import.
_RECORDS: list[dict] = []
_BY_LEAF: dict[str, dict] = {}                      # lower(K17000) -> first record with that leaf
_INDEX: dict[str, dict[str, list[str]]] = {}        # level_key -> lower(value) -> list of K17000 leaf names
_LEAF_NAMES: list[str] = []                         # all unique K17000 names (original case) for fuzzy match


def _norm(s: str) -> str:
    return (s or "").strip().lower()


_QUALIFIERS = (
    "senior", "sr.", "sr", "junior", "jr.", "jr", "lead", "principal", "staff",
    "ii", "iii", "iv", "v", "vi", "vii",
)


def _strip_qualifiers(title: str) -> str:
    """Drop seniority words / roman numerals so 'Senior Program Manager II' → 'program manager'."""
    tokens = [t for t in _norm(title).replace(",", " ").split() if t and t not in _QUALIFIERS]
    return " ".join(tokens)


def _load() -> None:
    global _RECORDS, _BY_LEAF, _INDEX, _LEAF_NAMES
    if _RECORDS:
        return
    try:
        with open(_TAXONOMY_PATH, encoding="utf-8") as f:
            _RECORDS = json.load(f)
    except FileNotFoundError:
        logger.warning("role_taxonomy: %s not found; expansion will return empty", _TAXONOMY_PATH)
        _RECORDS = []
        return

    _INDEX = {lvl: defaultdict(list) for lvl in _LEVELS}
    seen_leaves: set[str] = set()
    leaf_names: list[str] = []
    for rec in _RECORDS:
        leaf = rec.get("ROLE_K17000")
        if not leaf:
            continue
        key = _norm(leaf)
        if key not in _BY_LEAF:
            _BY_LEAF[key] = rec
        if key not in seen_leaves:
            seen_leaves.add(key)
            leaf_names.append(leaf)
        for lvl in _LEVELS:
            val = rec.get(lvl)
            if val:
                _INDEX[lvl][_norm(val)].append(leaf)
    _LEAF_NAMES = leaf_names
    logger.info("role_taxonomy: loaded %d roles, %d unique leaves", len(_RECORDS), len(_LEAF_NAMES))


_load()


@lru_cache(maxsize=4096)
def _fuzzy_leaf(title: str, *, min_score: int = 85) -> str | None:
    """Find the closest K17000 leaf for a free-text title. None if below threshold.

    WRatio at 85 penalises token-bag matches that share only generic words like
    "Developer" or "Manager" — those were the regression vector behind the
    "Front End Developer" → loan-roles bug. token_sort_ratio at 72 (the prior
    setting) accepted any pair that shared a couple of common tokens.
    """
    if not _LEAF_NAMES or not title:
        return None
    match = process.extractOne(title, _LEAF_NAMES, scorer=fuzz.WRatio, score_cutoff=min_score)
    return match[0] if match else None


def _tokenise(text: str) -> set[str]:
    return {t for t in _norm(text).replace(",", " ").replace("-", " ").split() if t}


@lru_cache(maxsize=4096)
def _canonical_substring_leaf(title: str) -> str | None:
    """Shortest K17000 leaf whose token set is a superset of the input's tokens.

    Prevents fuzzy from picking a niche leaf when the input is a generic title:
    "Data Scientist" used to resolve to "Data Science Writer" via WRatio, and
    its K10000/K5000 family then inherited the writer-flavoured siblings. The
    canonical branch instead requires every input token to appear as a token
    in the leaf, and picks the leaf with the fewest extra tokens.
    """
    if not _LEAF_NAMES or not title:
        return None
    input_tokens = _tokenise(title)
    if not input_tokens:
        return None
    # Require at least one significant token in the input; otherwise we'd match
    # any leaf containing a single generic word like "manager" alone.
    if not any(t not in _GENERIC_TOKENS and len(t) >= 4 for t in input_tokens):
        return None

    best_leaf: str | None = None
    best_count = 10**9
    for leaf in _LEAF_NAMES:
        leaf_tokens = _tokenise(leaf)
        if not input_tokens.issubset(leaf_tokens):
            continue
        count = len(leaf_tokens)
        if count < best_count:
            best_count = count
            best_leaf = leaf
            if count == len(input_tokens):
                return leaf
    return best_leaf


def _exact_at_level(value: str) -> dict | None:
    """If `value` appears as an exact value at K10000 or K5000, return a representative record.

    Restricted to the same levels `expand_title` uses for sibling collection.
    K1500/K1000 were previously included but those broad families pick an
    arbitrary representative leaf whose K10000/K5000 can be from a wholly
    different sub-domain (the "Front End Developer" → loan-roles regression).
    """
    if not value:
        return None
    key = _norm(value)
    for level in ("ROLE_K10000", "ROLE_K5000"):
        leaves = _INDEX.get(level, {}).get(key)
        if leaves:
            return _BY_LEAF.get(_norm(leaves[0]))
    return None


_GENERIC_TOKENS = {
    "manager", "engineer", "specialist", "developer", "analyst", "lead",
    "senior", "junior", "associate", "director", "officer", "head", "chief",
    "principal", "staff", "consultant", "coordinator", "administrator",
    "executive", "representative", "assistant",
}


def _significant_tokens(text: str) -> set[str]:
    return {
        t for t in _norm(text).replace(",", " ").replace("-", " ").split()
        if len(t) >= 4 and t not in _GENERIC_TOKENS
    }


def _share_significant_token(input_title: str, resolved_leaf: str) -> bool:
    """True iff the input and the resolved leaf share a non-generic concept.

    A "concept" is either:
      - an identical ≥4-char non-generic token (e.g. "lending" in both), or
      - one token is a prefix of the other and both have ≥4 chars (handles
        "front end"/"frontend", "back end"/"backend", "ecomm"/"ecommerce").

    Guards against e.g. "Front End Developer" resolving to a loan-family leaf
    where the only shared token is "developer" (generic, on the stoplist).
    """
    a = _significant_tokens(input_title)
    b = _significant_tokens(resolved_leaf)
    if a & b:
        return True
    for x in a:
        for y in b:
            if x.startswith(y) or y.startswith(x):
                return True
    return False


def _accept(rec: dict | None, input_title: str) -> dict | None:
    """Return `rec` only if its K17000 leaf shares a significant token with the input."""
    if not rec:
        return None
    leaf = rec.get("ROLE_K17000") or ""
    if not leaf:
        return None
    if _share_significant_token(input_title, leaf):
        return rec
    return None


def _is_relevant_sibling(input_title: str, sibling_leaf: str) -> bool:
    """A sibling is kept only if it shares a real concept with the input.

    Two K10000/K5000 family members can share zero meaningful overlap with the
    input (e.g. "Last Mile Coordinator" sits in the IT Project Manager K5000
    family alongside legitimate PMs). The resolver-side `_share_significant_token`
    gate only checks input vs resolved leaf, not input vs each expanded sibling
    — which is why those families leaked through.

    Accept if either:
      - a non-generic ≥4-char token (or prefix-equal pair) is shared, or
      - rapidfuzz token_set_ratio ≥ 60 (handles spelling variants like
        "frontend"/"front end" where significant tokens differ on the surface).
    """
    if not sibling_leaf:
        return False
    if _share_significant_token(input_title, sibling_leaf):
        return True
    return fuzz.token_set_ratio(input_title, sibling_leaf) >= 60


def _resolve(title: str) -> dict | None:
    """Resolve a free-text title to its taxonomy record.

    Order:
      1. Exact K17000 leaf match (raw and stripped) — always accepted.
      2. Exact match at K10000/K5000 family level — accepted only if the
         representative leaf shares a significant token with the input.
      3. Canonical-substring K17000 leaf — shortest leaf whose tokens are a
         superset of the input's. Prefers a generic-looking leaf over a fuzzy
         pick into a niche variant ("Data Scientist" → "Data Science Writer").
      4. Fuzzy K17000 leaf match (WRatio, cutoff 85) — same significant-token
         gate. Tried on raw then on the qualifier-stripped form.

    The significant-token gate is what prevents accidental cross-domain
    resolution; the LRU cache on `_fuzzy_leaf` keeps the hot path cheap.
    """
    if not title:
        return None
    raw = _norm(title)
    stripped = _strip_qualifiers(title)

    for key in (raw, stripped):
        if key and (rec := _BY_LEAF.get(key)):
            return rec
        if key and (rec := _accept(_exact_at_level(key), title)):
            return rec

    for candidate in (title, _strip_qualifiers(title)):
        leaf = _canonical_substring_leaf(candidate)
        if leaf and (rec := _accept(_BY_LEAF.get(_norm(leaf)), title)):
            return rec

    for candidate in (raw, stripped):
        leaf = _fuzzy_leaf(candidate)
        if leaf and (rec := _accept(_BY_LEAF.get(_norm(leaf)), title)):
            return rec

    logger.info("role_taxonomy: no confident match for %r", title)
    return None


def _collect(rec: dict, level: str, exclude: set[str]) -> list[str]:
    """Return all K17000 leaves sharing `rec`'s value at `level`, excluding given set."""
    val = rec.get(level)
    if not val:
        return []
    leaves = _INDEX.get(level, {}).get(_norm(val), [])
    return [leaf for leaf in leaves if _norm(leaf) not in exclude]


def _direct_family_leaves(title: str) -> list[tuple[str, str]]:
    """Leaves whose K10000 or K5000 EQUALS the input (raw or qualifier-stripped).

    For generic titles like "Software Engineer" / "Business Analyst" there is
    no K17000 leaf to resolve to, but the input IS the family name — those
    families collectively have 41 / 68 members respectively. Going through
    `_resolve` would either fail (NO MATCH) or pick a niche leaf via fuzzy
    whose K10000/K5000 belongs to a different family entirely.

    K10000 members come before K5000; duplicates across levels are skipped.
    """
    if not title:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for key_form in (_norm(title), _norm(_strip_qualifiers(title))):
        if not key_form:
            continue
        for level in ("ROLE_K10000", "ROLE_K5000"):
            for leaf in _INDEX.get(level, {}).get(key_form, []):
                k = _norm(leaf)
                if k not in seen:
                    seen.add(k)
                    out.append((leaf, level))
    return out


def expand_title(base_title: str, *, max_results: int = 10) -> list[dict]:
    """
    Expand a free-text title into similar/related titles using the taxonomy hierarchy.

    Returns a list of dicts ordered by relevance:
        [{"title": "Strategic Project Manager", "relevance": "similar", "level": "ROLE_K10000"}, ...]

    Two paths feed the candidate pool:
      a. If the input itself is a K10000/K5000 family name, take the whole family
         directly (generic-title path).
      b. Otherwise resolve the input to a K17000 leaf via `_resolve` and collect
         its K10000 then K5000 siblings.

    A per-sibling relevance filter then drops members that don't share a real
    concept with the input — this is what prevents K5000 noise like
    "Last Mile Coordinator" leaking into a "Project Manager" expansion.

    Returns [] if neither path produces candidates.
    """
    if not base_title:
        return []

    # Seed exclusion with the input itself (raw + qualifier-stripped) so we
    # never recommend the user's own title back at them.
    excluded: set[str] = {_norm(base_title)}
    stripped_input = _norm(_strip_qualifiers(base_title))
    if stripped_input:
        excluded.add(stripped_input)
    candidates: list[tuple[str, str]] = []  # (leaf, level)

    # Path A — input itself is a K10000/K5000 family name (covers generic
    # titles like "Software Engineer" that aren't K17000 leaves but ARE
    # canonical family names).
    for leaf, level in _direct_family_leaves(base_title):
        k = _norm(leaf)
        if k not in excluded:
            candidates.append((leaf, level))
            excluded.add(k)

    # Path B — resolve the input to a K17000 leaf and take its K10000 then
    # K5000 family. Run regardless of Path A: for titles like "Project Manager"
    # that ARE K17000 leaves, the leaf-family path produces a bigger and
    # better-curated set than the input-as-family-name path alone (which only
    # finds K5000 members where the input is the exact family name).
    rec = _resolve(base_title)
    if rec:
        seed_leaf = rec.get("ROLE_K17000")
        if seed_leaf:
            excluded.add(_norm(seed_leaf))
        # K10000 (near-identical tier) before K5000 (broader, noisier).
        for level in ("ROLE_K10000", "ROLE_K5000"):
            for leaf in _collect(rec, level, excluded):
                candidates.append((leaf, level))
                excluded.add(_norm(leaf))

    if not candidates:
        return []

    out: list[dict] = []
    for leaf, level in candidates:
        if len(out) >= max_results:
            break
        if not _is_relevant_sibling(base_title, leaf):
            continue
        out.append({"title": leaf, "relevance": "similar", "level": level})

    return out


def compare(title_a: str, title_b: str) -> Relevance:
    """
    Compare two titles via the taxonomy hierarchy. Only K17000 (exact) and
    K10000/K5000 (tight family) count — K1500 and below are too broad and
    drift into unrelated domains (e.g. K1500 conflates Program Directors
    with Community Program Coordinators).
        exact    same K17000
        similar  same K10000 or K5000
        none     otherwise
    """
    rec_a = _resolve(title_a)
    rec_b = _resolve(title_b)
    if not rec_a or not rec_b:
        return "none"
    if _norm(rec_a.get("ROLE_K17000", "")) == _norm(rec_b.get("ROLE_K17000", "")):
        return "exact"
    for level in ("ROLE_K10000", "ROLE_K5000"):
        if rec_a.get(level) and _norm(rec_a[level]) == _norm(rec_b.get(level, "")):
            return "similar"
    return "none"


def is_loaded() -> bool:
    return bool(_RECORDS)
