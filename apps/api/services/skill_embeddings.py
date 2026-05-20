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
from typing import Dict, List, Optional

from openai import AsyncOpenAI

from core.config import (
    EMBEDDING_CACHE_MAX,
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_MODEL,
)
from core.llm_client import get_openai_client
from core import llm_cache as _llm_cache

logger = logging.getLogger(__name__)

_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_CACHE_LOCK = asyncio.Lock()
_BATCH_SIZE = 256


def _normalize(term: str) -> str:
    return " ".join(str(term or "").lower().split())


def _client() -> Optional[AsyncOpenAI]:
    return get_openai_client()


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

        # L2: check Redis before calling OpenAI. Embeddings are
        # deterministic for a given (model, input) pair so we cache
        # without a TTL. Net: a fresh worker boots warm against Redis
        # instead of re-embedding every term that crossed any worker
        # before its restart.
        from_redis: Dict[str, List[float]] = {}
        for term in still_needed:
            vec = await _embed_get_from_redis(term)
            if vec is not None:
                _CACHE[term] = vec
                from_redis[term] = vec

        api_needed = [t for t in still_needed if t not in from_redis]
        if from_redis:
            logger.info(f"embedding warm: redis L2 served {len(from_redis)}/{len(still_needed)}")

        for i in range(0, len(api_needed), _BATCH_SIZE):
            chunk = api_needed[i : i + _BATCH_SIZE]
            try:
                resp = await client.embeddings.create(
                    model=OPENAI_EMBEDDING_MODEL,
                    input=chunk,
                )
                for term, item in zip(chunk, resp.data):
                    vec = list(item.embedding)
                    _CACHE[term] = vec
                    # Write-through to Redis so the next worker / restart
                    # benefits without paying the OpenAI cost again.
                    await _embed_put_to_redis(term, vec)
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


def _embed_redis_key(term: str) -> str:
    # Include model in the namespace so swapping the embedding model
    # naturally invalidates the cache (vectors from different models
    # aren't comparable).
    return _llm_cache.make_key("embed", 1, OPENAI_EMBEDDING_MODEL, term)


async def _embed_get_from_redis(term: str) -> Optional[List[float]]:
    raw = await _llm_cache.get_str(_embed_redis_key(term))
    if not raw:
        return None
    try:
        import json
        vec = json.loads(raw)
        return vec if isinstance(vec, list) else None
    except Exception:
        return None


async def _embed_put_to_redis(term: str, vec: List[float]) -> None:
    if not vec:
        return
    try:
        import json
        await _llm_cache.set_str(
            _embed_redis_key(term), json.dumps(vec), ttl_seconds=None
        )
    except Exception as exc:
        logger.debug(f"embed redis write failed for {term!r}: {exc}")


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
