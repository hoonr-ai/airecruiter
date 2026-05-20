"""Async Redis cache for LLM responses.

Used by Tribunal verdicts, parsed candidate profiles, rubric/screening
outputs, and location-proximity verdicts to avoid re-charging OpenAI for
inputs we've already seen. When ``REDIS_URL`` is empty or
``LLM_CACHE_ENABLED`` is false the module is a no-op — every ``get`` is
a miss and every ``set`` is a swallow. Redis client failures (timeout,
unreachable) degrade the same way: log + return None, never raise into
the call site.

Key scheme (versioned so a prompt change invalidates without an explicit
flush): ``llm:<namespace>:v<n>:<input-hash>``. Build keys with
``make_key`` and the matching ``hash_inputs`` helper to keep hashing
canonical across call sites.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from core import config as _cfg

logger = logging.getLogger(__name__)

_redis_client: Optional[Any] = None
_redis_unavailable: bool = False  # latched after first failure to stop log spam


def _get_client() -> Optional[Any]:
    """Lazy-init the redis.asyncio client. Returns None when caching is
    disabled or the import / connection setup fails."""
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    # Read config dynamically so tests can flip flags without re-importing.
    if not getattr(_cfg, "LLM_CACHE_ENABLED", True) or not getattr(_cfg, "REDIS_URL", ""):
        _redis_unavailable = True
        return None
    try:
        import redis.asyncio as redis_asyncio  # imported lazily so the rest of the API boots without the dep present
        _redis_client = redis_asyncio.from_url(
            _cfg.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        logger.info("llm_cache: redis client initialized")
        return _redis_client
    except Exception as exc:
        logger.warning(f"llm_cache: redis init failed, caching disabled: {exc}")
        _redis_unavailable = True
        return None


def hash_inputs(*parts: Any) -> str:
    """Canonical sha256 over a tuple of inputs. ``None`` and empty strings
    are normalized so callers can pass optional kwargs without forking
    the cache key for trivial differences."""
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"\x00")
            continue
        if isinstance(p, (dict, list)):
            h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8"))
        else:
            h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator between fields
    return h.hexdigest()


def make_key(namespace: str, version: int, *parts: Any) -> str:
    return f"llm:{namespace}:v{version}:{hash_inputs(*parts)}"


async def get_json(key: str) -> Optional[Any]:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
    except Exception as exc:
        logger.debug(f"llm_cache get failed for {key}: {exc}")
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.warning(f"llm_cache: corrupt json at {key}, evicting")
        try:
            await client.delete(key)
        except Exception:
            pass
        return None


async def set_json(key: str, value: Any, ttl_seconds: Optional[int]) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        payload = json.dumps(value, default=str)
        if ttl_seconds and ttl_seconds > 0:
            await client.set(key, payload, ex=ttl_seconds)
        else:
            await client.set(key, payload)
    except Exception as exc:
        logger.debug(f"llm_cache set failed for {key}: {exc}")


async def get_str(key: str) -> Optional[str]:
    client = _get_client()
    if client is None:
        return None
    try:
        return await client.get(key)
    except Exception as exc:
        logger.debug(f"llm_cache get_str failed for {key}: {exc}")
        return None


async def set_str(key: str, value: str, ttl_seconds: Optional[int]) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        if ttl_seconds and ttl_seconds > 0:
            await client.set(key, value, ex=ttl_seconds)
        else:
            await client.set(key, value)
    except Exception as exc:
        logger.debug(f"llm_cache set_str failed for {key}: {exc}")


def is_enabled() -> bool:
    """For diagnostics — returns whether the cache can serve hits right now."""
    return _get_client() is not None
