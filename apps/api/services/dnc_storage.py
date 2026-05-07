"""DNC (Do Not Contact) list storage.

Mirrors the bootstrap pattern used by sourced_candidates_storage.py: a sync
``_ensure_dnc_schema()`` runs once from main.py lifespan, and a cached
``load_dnc_phone_set()`` helper feeds the per-request DNC check at save time
without re-querying for every candidate.

Schema:
- ``dnc_list(phone PRIMARY KEY, source, notes, created_at)`` — phones stored
  in normalized 11-digit form (e.g. ``"14408405137"``).
- ``sourced_candidates.dnc_stopped_at`` — set retroactively by the importer
  when a phone is added to DNC after the candidate was already launched. The
  outreach path filters on this column.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional, Set

import sqlalchemy
from sqlalchemy import text

from core.config import DATABASE_URL, SUPABASE_DB_URL

logger = logging.getLogger(__name__)


_ENGINE: Optional[sqlalchemy.engine.Engine] = None


def _get_engine() -> sqlalchemy.engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        url = DATABASE_URL or SUPABASE_DB_URL
        if not url:
            raise RuntimeError("DATABASE_URL not configured for dnc_storage")
        _ENGINE = sqlalchemy.create_engine(
            url,
            pool_size=2,
            max_overflow=4,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _ENGINE


def _ensure_dnc_schema() -> None:
    url = DATABASE_URL or SUPABASE_DB_URL
    if not url:
        logger.warning("dnc_schema_init_skipped: no DATABASE_URL")
        return
    try:
        engine = _get_engine()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS dnc_list (
                    phone TEXT PRIMARY KEY,
                    source TEXT NOT NULL DEFAULT 'zoom',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_dnc_phone ON dnc_list(phone)"
            ))
            conn.execute(text(
                "ALTER TABLE sourced_candidates "
                "ADD COLUMN IF NOT EXISTS dnc_stopped_at TIMESTAMP NULL"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_sc_dnc_stopped_at "
                "ON sourced_candidates(dnc_stopped_at) "
                "WHERE dnc_stopped_at IS NOT NULL"
            ))
        logger.info("dnc schema ready")
    except Exception as e:  # noqa: BLE001
        logger.error(f"dnc schema init failed: {e}")


async def init_dnc_schema() -> None:
    """Async wrapper called from main.py lifespan."""
    await asyncio.to_thread(_ensure_dnc_schema)


# 5-minute in-process cache of the DNC phone set. The list is small (~95
# rows) but every Launch PAIR save, every page load with a sourcing table,
# and every backend filter would otherwise hit the DB. Cache invalidates on
# TTL; the importer can call invalidate_dnc_cache() to force a refresh.
_CACHE_TTL_SECONDS = 300
_cache_lock = threading.Lock()
_cache: Optional[Set[str]] = None
_cache_loaded_at: float = 0.0


def invalidate_dnc_cache() -> None:
    """Drop the in-process cache. Call after writes to dnc_list."""
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = None
        _cache_loaded_at = 0.0


def load_dnc_phone_set(force_refresh: bool = False) -> Set[str]:
    """Return the set of normalized DNC phones, cached for 5 minutes.

    Returns an empty set if the DB is unreachable so the caller's behavior
    fails open (no false-positive blocks). DNC enforcement is best-effort by
    design — the importer is the source of truth, and a missed cache hit
    just means we re-query.
    """
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if not force_refresh and _cache is not None and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
        return _cache
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT phone FROM dnc_list")).fetchall()
        phones: Set[str] = {str(r[0]) for r in rows if r and r[0]}
        with _cache_lock:
            _cache = phones
            _cache_loaded_at = now
        return phones
    except Exception as e:  # noqa: BLE001
        logger.warning(f"load_dnc_phone_set failed (returning empty set): {e}")
        return set()
