"""In-process embedding cache for semantic skill matching.

Public API:
    await warm_terms(terms: list[str])    -- async, batches OpenAI calls
    best_cosine(query_term, candidate_terms) -> float  -- sync, cache-only

The cache is a global `OrderedDict` keyed by the lowercased + whitespace-
collapsed term, with LRU eviction at `EMBEDDING_CACHE_MAX` entries.
Failures cache an empty list so we don't retry the same broken batch on
every request — `best_cosine` treats empty as "no embedding" and falls
back silently to the caller's keyword score.

Used by `unified_candidate_search._fuzzy_term_score` when the
`EMBEDDING_SKILL_MATCH` env flag is on. Off by default; flip on
deliberately and verify with `scripts/eval_embedding_skills.py`.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import OrderedDict
from typing import List, Optional

from openai import AsyncOpenAI

from core.config import (
    EMBEDDING_CACHE_MAX,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)

logger = logging.getLogger(__name__)

_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_CLIENT: Optional[AsyncOpenAI] = None
_BATCH_SIZE = 256


def _normalize(term: str) -> str:
    return " ".join(str(term or "").lower().split())


def _client() -> Optional[AsyncOpenAI]:
    global _CLIENT
    if _CLIENT is None and OPENAI_API_KEY:
        _CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _CLIENT


async def warm_terms(terms: List[str]) -> None:
    """Ensure embeddings exist in the cache for every term.

    No-op for terms already cached or for empty/blank input. Failures are
    swallowed (logged at warning level) and the affected terms are
    cached as an empty vector so subsequent calls don't retry.
    """
    client = _client()
    if not client:
        return

    seen = set()
    needed: List[str] = []
    for raw in terms:
        norm = _normalize(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if norm not in _CACHE:
            needed.append(norm)

    if not needed:
        return

    async with _CACHE_LOCK:
        # Re-check inside the lock — another coroutine may have warmed
        # the same terms while we were queued.
        still_needed = [t for t in needed if t not in _CACHE]
        if not still_needed:
            return

        for i in range(0, len(still_needed), _BATCH_SIZE):
            chunk = still_needed[i : i + _BATCH_SIZE]
            try:
                resp = await client.embeddings.create(
                    model=OPENAI_EMBEDDING_MODEL,
                    input=chunk,
                )
                for term, item in zip(chunk, resp.data):
                    _CACHE[term] = list(item.embedding)
            except Exception as exc:
                logger.warning(
                    "embedding warm failed for batch of %d (model=%s): %s",
                    len(chunk),
                    OPENAI_EMBEDDING_MODEL,
                    exc,
                )
                for term in chunk:
                    # Empty vector marks "tried, failed" so we don't retry.
                    _CACHE.setdefault(term, [])

            while len(_CACHE) > EMBEDDING_CACHE_MAX:
                _CACHE.popitem(last=False)


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def best_cosine(query_term: str, candidate_terms: List[str]) -> float:
    """Return the max cosine similarity between query and candidate.

    Cache-only: if either side hasn't been warmed via `warm_terms`,
    returns 0.0. Caller decides whether to ignore (below threshold) or
    use as a score.
    """
    q_norm = _normalize(query_term)
    if not q_norm:
        return 0.0
    q_vec = _CACHE.get(q_norm)
    if not q_vec:
        return 0.0

    best = 0.0
    for raw in candidate_terms or []:
        c_norm = _normalize(raw)
        if not c_norm:
            continue
        c_vec = _CACHE.get(c_norm)
        if not c_vec:
            continue
        sim = _cosine(q_vec, c_vec)
        if sim > best:
            best = sim
            if best >= 0.999:
                break
    return best


def cache_size() -> int:
    return len(_CACHE)


def clear_cache() -> None:
    """Diagnostic helper — wipes the in-process cache. Tests only."""
    _CACHE.clear()
