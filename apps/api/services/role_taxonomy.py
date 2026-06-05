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


def _content_tokens(text: str) -> set[str]:
    """Distinctive content tokens (≥2 chars) used for JD-grounding tests.

    Keeps short domain/tech acronyms (AI, ML, HR, QA, BI, UX, IoT, ERP, EMR,
    SAP, AWS) that distinguish one role variant from another, but drops generic
    role words (`_GENERIC_TOKENS`) and seniority qualifiers (`_QUALIFIERS`) that
    carry no domain meaning.
    """
    return {
        t for t in _tokenise(text)
        if len(t) >= 2 and t not in _GENERIC_TOKENS and t not in _QUALIFIERS
    }


def _context_tokens(context_text: str) -> set[str]:
    """Content-token set of the JD context (grounding text + domain + skill names)."""
    return _content_tokens(context_text)


def _distinctive_tokens_supported(
    base_title: str, candidate: str, context_tokens: set[str]
) -> bool:
    """True iff every distinctive content token `candidate` adds over `base_title`
    is present in the JD context.

        distinctive = _content_tokens(candidate) - _content_tokens(base_title)

    - No distinctive tokens (near-exact variant differing only by generics or
      seniority, e.g. "Senior Business Analyst" over "Business Analyst") → supported.
    - Otherwise EVERY distinctive token must appear verbatim in `context_tokens`.
      Matching is exact (no prefix matching) on purpose: prefix matching let
      "Hospitality Business Analyst" match a healthcare JD via "hospital", and
      the AND-over-distinctive rule keeps off-domain variants like "Mortgage" /
      "Robotics" / "Payment" out unless the JD actually names that domain.
    - Empty context → unsupported unless distinctive is empty, so a context-free
      call contributes only near-exact variants, never the full noisy family.
    """
    distinctive = _content_tokens(candidate) - _content_tokens(base_title)
    if not distinctive:
        return True
    return all(tok in context_tokens for tok in distinctive)


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


def _collect_family_candidates(base_title: str) -> list[tuple[str, str]]:
    """Gather (leaf, level) family candidates for a title, before relevance filtering.

    Two paths feed the candidate pool:
      a. Path A — the input itself is a K10000/K5000 family name (covers generic
         titles like "Software Engineer" that aren't K17000 leaves but ARE
         canonical family names).
      b. Path B — resolve the input to a K17000 leaf via `_resolve` and collect
         its K10000 then K5000 siblings. Run regardless of Path A: for titles
         like "Project Manager" that ARE K17000 leaves, the leaf-family path
         produces a bigger and better-curated set than the input-as-family-name
         path alone.

    The input (raw + qualifier-stripped) and the resolved seed leaf are excluded
    so we never recommend the user's own title back at them. K10000 (near-
    identical tier) comes before K5000 (broader, noisier).
    """
    if not base_title:
        return []

    excluded: set[str] = {_norm(base_title)}
    stripped_input = _norm(_strip_qualifiers(base_title))
    if stripped_input:
        excluded.add(stripped_input)
    candidates: list[tuple[str, str]] = []  # (leaf, level)

    for leaf, level in _direct_family_leaves(base_title):
        k = _norm(leaf)
        if k not in excluded:
            candidates.append((leaf, level))
            excluded.add(k)

    rec = _resolve(base_title)
    if rec:
        seed_leaf = rec.get("ROLE_K17000")
        if seed_leaf:
            excluded.add(_norm(seed_leaf))
        for level in ("ROLE_K10000", "ROLE_K5000"):
            for leaf in _collect(rec, level, excluded):
                candidates.append((leaf, level))
                excluded.add(_norm(leaf))

    return candidates


def expand_title(base_title: str, *, max_results: int = 10) -> list[dict]:
    """
    Expand a free-text title into similar/related titles using the taxonomy hierarchy.

    Returns a list of dicts ordered by relevance:
        [{"title": "Strategic Project Manager", "relevance": "similar", "level": "ROLE_K10000"}, ...]

    Candidates come from `_collect_family_candidates` (the input's own
    K10000/K5000 family and/or its resolved K17000 leaf's siblings). A
    per-sibling relevance filter then drops members that don't share a real
    concept with the input — this is what prevents K5000 noise like
    "Last Mile Coordinator" leaking into a "Project Manager" expansion.

    Returns [] if no candidates are produced.
    """
    if not base_title:
        return []

    candidates = _collect_family_candidates(base_title)
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


def expand_title_grounded(
    base_title: str, context_text: str = "", *, max_results: int = 10
) -> list[dict]:
    """Context-filtered variant of `expand_title`.

    Same candidate collection and same `_is_relevant_sibling` gate, but
    additionally drops any sibling whose distinctive qualifier tokens are not
    supported by `context_text` (the JD grounding text + extracted domain +
    skill names). This keeps a generic "Business Analyst" from pulling in
    "Mortgage / Robotics / Payment Business Analyst" when the JD never mentions
    those domains.

    Returns the same [{"title", "relevance", "level"}] shape as `expand_title`.
    With empty `context_text` only near-exact variants survive (tight, never noisy).
    """
    if not base_title:
        return []

    context_tokens = _context_tokens(context_text)
    candidates = _collect_family_candidates(base_title)
    if not candidates:
        return []

    out: list[dict] = []
    for leaf, level in candidates:
        if len(out) >= max_results:
            break
        if not _is_relevant_sibling(base_title, leaf):
            continue
        if not _distinctive_tokens_supported(base_title, leaf, context_tokens):
            continue
        out.append({"title": leaf, "relevance": "similar", "level": level})

    return out


def is_grounded_variant(base_title: str, candidate: str, context_text: str) -> bool:
    """Gate for an externally-proposed (e.g. LLM-generated) similar title.

    Kept iff it is both related to the main title (the DB "clubbing" check) and
    on-domain for this job (the off-domain guard):

      - related: `compare(base, candidate) != "none"` OR it shares a significant
        token with the base title. This validates/clubs the LLM title against
        the taxonomy DB and the main title.
      - on-domain: it introduces no distinctive content token, OR at least one
        distinctive token it adds over `base_title` is named in the JD context.

    The off-domain guard closes the hole where relatedness alone would admit an
    off-domain variant that merely shares the same taxonomy K5000 family (e.g.
    "Mortgage Business Analyst" vs "Business Analyst" when the JD never mentions
    mortgage). It is looser than the taxonomy-side AND rule because the LLM has
    already read the JD: a genuinely adjacent title (e.g. "Healthcare Data
    Analyst") may add a descriptive word the JD doesn't state verbatim, so we
    require only that some distinctive token is grounded, not all of them.
    """
    if not base_title or not candidate:
        return False
    if _norm(candidate) == _norm(base_title):
        return False
    related = compare(base_title, candidate) != "none" or _share_significant_token(
        base_title, candidate
    )
    if not related:
        return False
    distinctive = _content_tokens(candidate) - _content_tokens(base_title)
    if not distinctive:
        return True
    context_tokens = _context_tokens(context_text)
    return any(tok in context_tokens for tok in distinctive)


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
