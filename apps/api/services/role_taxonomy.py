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


def _resolve(title: str) -> dict | None:
    """Resolve a free-text title to its taxonomy record.

    Order:
      1. Exact K17000 leaf match (raw and stripped) — always accepted.
      2. Exact match at K10000/K5000 family level — accepted only if the
         representative leaf shares a significant token with the input.
      3. Fuzzy K17000 leaf match (WRatio, cutoff 85) — same significant-token
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


def expand_title(base_title: str, *, max_results: int = 30) -> list[dict]:
    """
    Expand a free-text title into similar/related titles using the taxonomy hierarchy.

    Returns a list of dicts ordered by relevance:
        [{"title": "Strategic Project Manager", "relevance": "similar", "level": "ROLE_K1500"}, ...]

    If the title can't be resolved (no exact, no fuzzy match above threshold), returns [].
    """
    rec = _resolve(base_title)
    if not rec:
        return []

    seed_leaf = rec.get("ROLE_K17000")
    excluded = {_norm(seed_leaf)} if seed_leaf else set()
    out: list[dict] = []

    # Only the tighter hierarchy levels feed similar-title expansion.
    # K1500 / K1000 / K500 mix in cross-domain roles (e.g. K1500 for
    # "Program Director" is "Community Program Coordinator" — pulls in
    # community-services roles that aren't real PM peers). Restricting to
    # K10000 + K5000 keeps siblings inside the same tight role family.
    tier_for_level = {
        "ROLE_K10000": "similar",
        "ROLE_K5000": "similar",
    }

    for level, tier in tier_for_level.items():
        if len(out) >= max_results:
            break
        for leaf in _collect(rec, level, excluded):
            if len(out) >= max_results:
                break
            out.append({"title": leaf, "relevance": tier, "level": level})
            excluded.add(_norm(leaf))

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
