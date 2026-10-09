"""Durable, versioned parsed-resume cache (Fix 4 — parse each resume once).

Sits in front of the LLM resume extraction in
``sourced_candidates_storage._process_candidate_common`` (which both the
sourcing enrichment path and launch-time ``employer_resolution`` go through).

Layering relative to existing caches:
  * ``core/llm_cache.py`` (Redis) caches *scoring* LLM calls, not resume parses.
  * ``candidate_enhanced_info.resume_hash`` is the legacy, unversioned
    per-candidate store; it is still consulted after this table (only while
    ``RESUME_PARSER_VERSION`` is 1) so pre-existing parses keep paying off.
  * ``parsed_resumes`` (this module) stores the raw LLM extraction keyed by
    (normalized-text sha256, parser_version, kind). Bump
    ``RESUME_PARSER_VERSION`` whenever the crisp/extract prompts change.

Every DB error fails open: lookups return None, stores are dropped.
DB calls are sync psycopg2 and always run via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

KIND_ENHANCED_INFO = "enhanced_info_llm"
MIN_TEXT_LENGTH = 50

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS parsed_resumes (
        resume_sha256  char(64)    NOT NULL,
        parser_version int         NOT NULL,
        kind           text        NOT NULL DEFAULT 'enhanced_info_llm',
        parsed         jsonb       NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (resume_sha256, parser_version, kind)
    )
    """
)

_schema_lock = threading.Lock()
_schema_ready = False


def _settings() -> tuple[bool, int]:
    try:
        from core import config as _cfg  # noqa: PLC0415
        return (
            bool(getattr(_cfg, "PARSED_RESUME_CACHE_ENABLED", True)),
            int(getattr(_cfg, "RESUME_PARSER_VERSION", 1) or 1),
        )
    except Exception:  # noqa: BLE001
        return True, 1


def normalize_resume_text(text: str) -> str:
    """Collapse whitespace and strip control chars so trivially different
    copies of one resume hash identically."""
    if not text:
        return ""
    cleaned = "".join(ch for ch in text if ord(ch) >= 32 or ch in "\n\r\t")
    return re.sub(r"\s+", " ", cleaned).strip()


def resume_sha256(text: str) -> Optional[str]:
    normalized = normalize_resume_text(text)
    if len(normalized) < MIN_TEXT_LENGTH:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _get_conn():
    from core.db import get_db_connection  # noqa: PLC0415
    return get_db_connection()


def ensure_schema() -> None:
    """Idempotent DDL. Called from the sourced_candidates schema bootstrap at
    startup and lazily before first use. Raises on failure."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def _lookup_sync(sha: str, version: int, kind: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parsed FROM parsed_resumes "
                "WHERE resume_sha256 = %s AND parser_version = %s AND kind = %s",
                (sha, version, kind),
            )
            row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        return None
    value = row[0]
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def _store_sync(sha: str, version: int, kind: str, parsed: Dict[str, Any]) -> None:
    ensure_schema()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO parsed_resumes (resume_sha256, parser_version, kind, parsed) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (sha, version, kind, json.dumps(parsed, default=str)),
            )
        conn.commit()
    finally:
        conn.close()


def _is_cacheable(parsed: Any) -> bool:
    """Only cache clean structured extractions — never errors or the
    ``{"raw": ...}`` JSON-decode fallback."""
    return isinstance(parsed, dict) and bool(parsed) and not parsed.get("error") and "raw" not in parsed


class ResumeProfileService:
    """get_or_parse(text) → the parse_fn output for this resume, served from
    ``parsed_resumes`` when possible."""

    @staticmethod
    async def lookup(text: str, kind: str = KIND_ENHANCED_INFO) -> Optional[Dict[str, Any]]:
        enabled, version = _settings()
        if not enabled:
            return None
        sha = resume_sha256(text)
        if not sha:
            return None
        try:
            return await asyncio.to_thread(_lookup_sync, sha, version, kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("parsed_resumes lookup failed (fail-open): %s", exc)
            return None

    @staticmethod
    async def store(text: str, parsed: Dict[str, Any], kind: str = KIND_ENHANCED_INFO) -> None:
        enabled, version = _settings()
        if not enabled or not _is_cacheable(parsed):
            return
        sha = resume_sha256(text)
        if not sha:
            return
        try:
            await asyncio.to_thread(_store_sync, sha, version, kind, parsed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("parsed_resumes store failed (fail-open): %s", exc)

    @classmethod
    async def get_or_parse(
        cls,
        text: str,
        parse_fn: Callable[[str], Awaitable[Dict[str, Any]]],
        kind: str = KIND_ENHANCED_INFO,
    ) -> Dict[str, Any]:
        """Return ``parse_fn(text)``'s result, from cache when possible. Cache
        hits carry ``"_parsed_resume_cache": "hit"``."""
        cached = await cls.lookup(text, kind)
        if cached is not None:
            return {**cached, "_parsed_resume_cache": "hit"}
        parsed = await parse_fn(text)
        await cls.store(text, parsed, kind)
        return parsed
