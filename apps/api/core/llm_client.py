"""Singleton accessor for the AsyncOpenAI client and a tiny usage logger.

Pre-tier-2, nine separate services each instantiated their own
``AsyncOpenAI(api_key=OPENAI_API_KEY)``. Each carried its own
``httpx.AsyncClient`` and connection pool, multiplying socket usage
under load. This module exposes a single shared instance and a small
``log_usage`` helper so every call site can emit a consistent cost line
without re-implementing the token-count / cached-tokens extraction.

The wrapper is intentionally thin — no ``chat_complete()`` re-wrapping
of the SDK API. Migration is "swap ``AsyncOpenAI(...)`` for
``get_openai_client()``" and that's it.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from openai import AsyncOpenAI

from core import config as _cfg

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def get_openai_client() -> Optional[AsyncOpenAI]:
    """Return the shared AsyncOpenAI client, or None when no API key is
    configured. Same semantics every legacy call site already handles
    (``self.client = AsyncOpenAI(api_key=key) if key else None``)."""
    global _client
    if _client is not None:
        return _client
    key = getattr(_cfg, "OPENAI_API_KEY", "")
    if not key:
        return None
    _client = AsyncOpenAI(api_key=key)
    logger.info("openai client singleton initialized")
    return _client


def reset_client_for_tests() -> None:
    """Drop the singleton. Tests only."""
    global _client
    _client = None


def model_for(purpose: str, default: str) -> str:
    """Resolve the model to use for a logical call site.

    Layered:
      1. ``LLM_MODEL_<PURPOSE>`` env override (e.g. ``LLM_MODEL_BOOLEAN``)
      2. The ``default`` argument

    Per the tier-3 model-tiering decision (see
    docs/chatgpt-optimization-roadmap.md), mechanical schema-fill calls
    default to ``gpt-4.1-nano`` and reasoning-light calls default to
    ``gpt-4o-mini``. If a downgrade regresses quality, the operator sets
    ``LLM_MODEL_<PURPOSE>=gpt-4o-mini`` (or back to ``gpt-4o``) without a
    redeploy.
    """
    return os.getenv(f"LLM_MODEL_{purpose.upper()}", default)


def log_usage(
    label: str,
    completion,
    *,
    cache_hit: bool = False,
    duration_ms: Optional[float] = None,
) -> None:
    """Emit one line per LLM completion: model, token counts, prompt-cache
    hits (from OpenAI's automatic prefix cache), and whether our own
    response cache served the result. All errors are swallowed — never
    let telemetry break a call site."""
    try:
        model = getattr(completion, "model", "?")
        usage = getattr(completion, "usage", None)
        if usage is None:
            logger.info(f"llm[{label}] model={model} cache_hit={cache_hit} (no usage)")
            return
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", 0) if details else 0
        duration_str = f"{duration_ms:.0f}" if isinstance(duration_ms, (int, float)) else "?"
        logger.info(
            f"llm[{label}] model={model} "
            f"prompt={prompt_tokens} cached_prefix={cached_tokens} "
            f"completion={completion_tokens} "
            f"cache_hit={cache_hit} duration_ms={duration_str}"
        )
    except Exception as exc:
        logger.debug(f"log_usage failed for {label}: {exc}")
