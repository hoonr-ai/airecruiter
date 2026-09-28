"""Person-level contact cache: never pay twice for the same person.

Contact lookups are keyed by nothing durable. The same LinkedIn profile turns up
on many jobs and in every Step-5 re-run, and each time the chain (ZoomInfo ->
Apollo -> paid Exa) was asked again, including for people whose lookup had
already come back empty. This table remembers, per LinkedIn profile:

  - the email / phone a provider found, and which provider (a hit, reused for
    CONTACT_CACHE_HIT_TTL_DAYS);
  - that a COMPLETE lookup found no email / phone (a miss, honoured for
    CONTACT_CACHE_MISS_TTL_DAYS so the paid step is not re-bought).

Only provider-found values are written, never the contact a row arrived with
(LLM-extracted profile text is unreliable). A miss is recorded only when the
last paid step (Exa) actually answered for that field; a timeout, an error or a
budget skip is not a miss.

Everything fails open: a DB problem means "no cache", never a failed lookup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, Iterable, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#\s]+)", re.IGNORECASE)

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS contact_enrichment_cache (
        linkedin_slug TEXT PRIMARY KEY,
        email TEXT,
        email_provider TEXT,
        email_found_at TIMESTAMPTZ,
        email_missed_at TIMESTAMPTZ,
        phone TEXT,
        phone_provider TEXT,
        phone_found_at TIMESTAMPTZ,
        phone_missed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""

READ_SQL = """
    SELECT email, email_provider, phone, phone_provider,
           email IS NOT NULL AND email_found_at > NOW() - make_interval(days => %(hit_days)s) AS email_fresh,
           phone IS NOT NULL AND phone_found_at > NOW() - make_interval(days => %(hit_days)s) AS phone_fresh,
           email_missed_at > NOW() - make_interval(days => %(miss_days)s) AS email_missed_recent,
           phone_missed_at > NOW() - make_interval(days => %(miss_days)s) AS phone_missed_recent
    FROM contact_enrichment_cache
    WHERE linkedin_slug = %(slug)s
"""

# A found value always wins over an older miss; a new miss never erases a
# value (it only stamps *_missed_at, which READ ignores while a fresh value
# exists).
UPSERT_SQL = """
    INSERT INTO contact_enrichment_cache AS c (
        linkedin_slug, email, email_provider, email_found_at, email_missed_at,
        phone, phone_provider, phone_found_at, phone_missed_at, updated_at
    ) VALUES (
        %(slug)s,
        %(email)s, %(email_provider)s,
        CASE WHEN %(email)s::text IS NOT NULL THEN NOW() END,
        CASE WHEN %(email_missed)s THEN NOW() END,
        %(phone)s, %(phone_provider)s,
        CASE WHEN %(phone)s::text IS NOT NULL THEN NOW() END,
        CASE WHEN %(phone_missed)s THEN NOW() END,
        NOW()
    )
    ON CONFLICT (linkedin_slug) DO UPDATE SET
        email = COALESCE(EXCLUDED.email, c.email),
        email_provider = CASE WHEN EXCLUDED.email IS NOT NULL THEN EXCLUDED.email_provider ELSE c.email_provider END,
        email_found_at = COALESCE(EXCLUDED.email_found_at, c.email_found_at),
        email_missed_at = COALESCE(EXCLUDED.email_missed_at, c.email_missed_at),
        phone = COALESCE(EXCLUDED.phone, c.phone),
        phone_provider = CASE WHEN EXCLUDED.phone IS NOT NULL THEN EXCLUDED.phone_provider ELSE c.phone_provider END,
        phone_found_at = COALESCE(EXCLUDED.phone_found_at, c.phone_found_at),
        phone_missed_at = COALESCE(EXCLUDED.phone_missed_at, c.phone_missed_at),
        updated_at = NOW()
"""


def _enabled() -> bool:
    return os.getenv("CONTACT_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)).strip() or default))
    except ValueError:
        return default


def hit_ttl_days() -> int:
    """Found contact goes stale (people change jobs): reuse for this long."""
    return _int_env("CONTACT_CACHE_HIT_TTL_DAYS", 180)


def miss_ttl_days() -> int:
    """Don't re-buy a lookup that came back empty for this long."""
    return _int_env("CONTACT_CACHE_MISS_TTL_DAYS", 30)


def linkedin_slug(url: Any) -> str:
    """Stable per-person key from any LinkedIn profile URL form
    (www./in./country subdomains, trailing slash, query, %-encoding, case)."""
    m = _SLUG_RE.search(str(url or ""))
    if not m:
        return ""
    return unquote(m.group(1)).strip().strip("/").lower()


_table_ready = False


def _connect():
    # Looked up at call time so tests (conftest blocks real connections) and
    # callers see the same seam.
    from core import db as _db
    return _db.get_db_connection()


def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    with conn.cursor() as cur:
        # 8 workers can race on first use; CREATE TABLE IF NOT EXISTS is not
        # concurrency-safe on its own (same pattern as unipile_account_usage).
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('contact_enrichment_cache_ddl'))")
        cur.execute(CREATE_SQL)
    conn.commit()
    _table_ready = True
    # Seed from lookups paid for before the cache existed. Once per database
    # (done-marker row); a failure is logged and retried on the next start.
    from services import contact_cache_backfill
    contact_cache_backfill.run_once(conn)


def _get_sync(slug: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(READ_SQL, {"slug": slug, "hit_days": hit_ttl_days(), "miss_days": miss_ttl_days()})
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        conn.commit()
    finally:
        conn.close()
    if not row:
        return None
    return dict(zip(cols, row)) if not isinstance(row, dict) else dict(row)


def _record_sync(params: Dict[str, Any]) -> None:
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, params)
        conn.commit()
    finally:
        conn.close()


def empty() -> Dict[str, Any]:
    return {"email": "", "phone": "", "email_provider": "", "phone_provider": "",
            "email_missed": False, "phone_missed": False}


async def get(linkedin_url: Any) -> Dict[str, Any]:
    """What we already know about this person. Always returns the full shape
    (see ``empty``); ``*_missed`` is True only for a recent miss with no fresh
    value."""
    out = empty()
    slug = linkedin_slug(linkedin_url)
    if not slug or not _enabled():
        return out
    try:
        row = await asyncio.to_thread(_get_sync, slug)
    except Exception as e:
        logger.warning("contact_cache read failed for %s: %s", slug, e)
        return out
    if not row:
        return out
    if row.get("email_fresh"):
        out["email"] = str(row.get("email") or "")
        out["email_provider"] = str(row.get("email_provider") or "")
    if row.get("phone_fresh"):
        out["phone"] = str(row.get("phone") or "")
        out["phone_provider"] = str(row.get("phone_provider") or "")
    out["email_missed"] = bool(row.get("email_missed_recent")) and not out["email"]
    out["phone_missed"] = bool(row.get("phone_missed_recent")) and not out["phone"]
    return out


async def record(
    linkedin_url: Any,
    *,
    email: str = "",
    phone: str = "",
    email_provider: str = "",
    phone_provider: str = "",
    missed: Iterable[str] = (),
) -> None:
    """Remember what a lookup found (and which fields a complete lookup did not
    find). No-op when there is nothing to say. Never raises."""
    slug = linkedin_slug(linkedin_url)
    if not slug or not _enabled():
        return
    email = (email or "").strip().lower()
    if "@" not in email:
        email = ""
    phone = (phone or "").strip()
    if sum(1 for ch in phone if ch.isdigit()) < 7:
        phone = ""
    missed = set(missed or ())
    email_missed = "email" in missed and not email
    phone_missed = "phone" in missed and not phone
    if not (email or phone or email_missed or phone_missed):
        return
    params = {
        "slug": slug,
        "email": email or None,
        "email_provider": (email_provider or None) if email else None,
        "email_missed": email_missed,
        "phone": phone or None,
        "phone_provider": (phone_provider or None) if phone else None,
        "phone_missed": phone_missed,
    }
    try:
        await asyncio.to_thread(_record_sync, params)
    except Exception as e:
        logger.warning("contact_cache write failed for %s: %s", slug, e)
