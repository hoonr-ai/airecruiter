"""Person-level contact cache: never pay twice for the same person.

Contact lookups are keyed by nothing durable. The same LinkedIn profile turns up
on many jobs and in every Step-5 re-run, and each time the chain (ZoomInfo ->
Apollo -> paid Exa) was asked again, including for people whose lookup had
already come back empty. This table remembers, per LinkedIn profile:

  - the email / phone a provider found, and which provider (a hit, reused
    for good by default: CONTACT_CACHE_HIT_TTL_DAYS, 0 = never expires);
  - that a lookup found no email / phone (a miss): a miss of the normal Exa
    run (``*_missed_at``) sends the next lookup straight to the deep search for
    CONTACT_CACHE_MISS_TTL_DAYS, and a miss of the deep search
    (``*_deep_missed_at``, the end of the chain) is final for
    CONTACT_CACHE_DEEP_MISS_TTL_DAYS: Launch PAIR and sourcing never re-buy
    it from Exa, and a recruiter's click re-buys it only once they confirm
    (routers/candidates.py, CONTACT_CLICK_HONOUR_DEEP_MISS); the on-demand
    chain still asks the cheaper providers. A normal-run miss inside that
    window is final too when Exa runs but no deep search can follow.

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
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        phone_personal BOOLEAN,
        email_deep_missed_at TIMESTAMPTZ,
        phone_deep_missed_at TIMESTAMPTZ
    )
"""

# Phones are reused only when marked personal (user 2026-09-28: "we only need
# personal phone number"). Rows written before the marker, including the
# backfill, were filled from mobile OR work numbers, so their phone is ignored
# and the person is looked up again.
# Deep-search misses (2026-09-29): a miss recorded before the deep search
# existed is a normal-run miss, so the next Launch still tries the deep search.
MIGRATE_SQL = """
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS phone_personal BOOLEAN;
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS email_deep_missed_at TIMESTAMPTZ;
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS phone_deep_missed_at TIMESTAMPTZ
"""

# Hit TTL 0 = a found value never expires. Miss TTL 0 = misses not honoured
# (a normal-run miss is then not final either: *_missed_in_deep_ttl needs both).
READ_SQL = """
    SELECT email, email_provider, phone, phone_provider,
           email_missed_at, phone_missed_at, email_deep_missed_at, phone_deep_missed_at,
           email IS NOT NULL
               AND (%(hit_days)s = 0 OR email_found_at > NOW() - make_interval(days => %(hit_days)s)) AS email_fresh,
           phone IS NOT NULL AND phone_personal IS TRUE
               AND (%(hit_days)s = 0 OR phone_found_at > NOW() - make_interval(days => %(hit_days)s)) AS phone_fresh,
           %(miss_days)s > 0 AND email_missed_at > NOW() - make_interval(days => %(miss_days)s) AS email_missed_recent,
           %(miss_days)s > 0 AND phone_missed_at > NOW() - make_interval(days => %(miss_days)s) AS phone_missed_recent,
           %(miss_days)s > 0 AND %(deep_miss_days)s > 0
               AND email_missed_at > NOW() - make_interval(days => %(deep_miss_days)s) AS email_missed_in_deep_ttl,
           %(miss_days)s > 0 AND %(deep_miss_days)s > 0
               AND phone_missed_at > NOW() - make_interval(days => %(deep_miss_days)s) AS phone_missed_in_deep_ttl,
           %(deep_miss_days)s > 0
               AND email_deep_missed_at > NOW() - make_interval(days => %(deep_miss_days)s) AS email_deep_missed_recent,
           %(deep_miss_days)s > 0
               AND phone_deep_missed_at > NOW() - make_interval(days => %(deep_miss_days)s) AS phone_deep_missed_recent
    FROM contact_enrichment_cache
    WHERE linkedin_slug = %(slug)s
"""

# A found value always wins over an older miss; a new miss never erases a
# value (it only stamps *_missed_at, which READ ignores while a fresh value
# exists).
UPSERT_SQL = """
    INSERT INTO contact_enrichment_cache AS c (
        linkedin_slug, email, email_provider, email_found_at, email_missed_at,
        phone, phone_provider, phone_found_at, phone_missed_at, updated_at, phone_personal,
        email_deep_missed_at, phone_deep_missed_at
    ) VALUES (
        %(slug)s,
        %(email)s, %(email_provider)s,
        CASE WHEN %(email)s::text IS NOT NULL THEN NOW() END,
        CASE WHEN %(email_missed)s THEN NOW() END,
        %(phone)s, %(phone_provider)s,
        CASE WHEN %(phone)s::text IS NOT NULL THEN NOW() END,
        CASE WHEN %(phone_missed)s THEN NOW() END,
        NOW(),
        CASE WHEN %(phone)s::text IS NOT NULL THEN TRUE END,
        CASE WHEN %(email_deep_missed)s THEN NOW() END,
        CASE WHEN %(phone_deep_missed)s THEN NOW() END
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
        phone_personal = CASE WHEN EXCLUDED.phone IS NOT NULL THEN TRUE ELSE c.phone_personal END,
        email_deep_missed_at = COALESCE(EXCLUDED.email_deep_missed_at, c.email_deep_missed_at),
        phone_deep_missed_at = COALESCE(EXCLUDED.phone_deep_missed_at, c.phone_deep_missed_at),
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
    """How long found contact is reused. 0 (the default, 2026-10-15: "never
    pay twice for the same person") = for good; a positive value brings a TTL
    back should stale work emails become a problem."""
    return _int_env("CONTACT_CACHE_HIT_TTL_DAYS", 0)


def miss_ttl_days() -> int:
    """How long a normal Exa run's miss sends the next lookup straight to the
    deep search (and keeps sourcing from re-buying it). 0 = not honoured at
    all (nor final when no deep search can follow)."""
    return _int_env("CONTACT_CACHE_MISS_TTL_DAYS", 30)


def deep_miss_ttl_days() -> int:
    """How long a deep-search miss is final (not re-bought by Launch PAIR or
    sourcing; a click re-buys it only on confirm). 0 = not honoured."""
    return _int_env("CONTACT_CACHE_DEEP_MISS_TTL_DAYS", 180)


def click_honours_deep_miss() -> bool:
    """Whether a recruiter's click (phone button, Rankings Get Contact) gets
    "searched <date>, nothing found" for a final miss instead of re-buying it
    unasked. False restores the 2026-09-28 rule (ca1c503b): a click always
    re-runs the whole chain."""
    return os.getenv("CONTACT_CLICK_HONOUR_DEEP_MISS", "true").strip().lower() not in {"0", "false", "no", "off"}


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
        cur.execute(MIGRATE_SQL)
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
            cur.execute(READ_SQL, {
                "slug": slug, "hit_days": hit_ttl_days(), "miss_days": miss_ttl_days(),
                "deep_miss_days": deep_miss_ttl_days(),
            })
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
            "email_missed": False, "phone_missed": False,
            "email_deep_missed": False, "phone_deep_missed": False,
            "email_missed_in_deep_ttl": False, "phone_missed_in_deep_ttl": False,
            "email_missed_at": "", "phone_missed_at": "",
            "email_deep_missed_at": "", "phone_deep_missed_at": ""}


def _iso(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def get(linkedin_url: Any) -> Dict[str, Any]:
    """What we already know about this person. Always returns the full shape
    (see ``empty``). With no fresh value for the field: ``*_missed`` is True
    for a normal-run miss within CONTACT_CACHE_MISS_TTL_DAYS or a deep-search
    miss within CONTACT_CACHE_DEEP_MISS_TTL_DAYS, ``*_deep_missed`` only for
    the latter, ``*_missed_in_deep_ttl`` for a normal-run miss within the
    deep-miss TTL (final when Exa runs but no deep search can follow; never
    with CONTACT_CACHE_MISS_TTL_DAYS=0). ``*_missed_at`` /
    ``*_deep_missed_at`` are the last miss times (ISO 8601, "" = none)."""
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
    for f in ("email", "phone"):
        deep = bool(row.get(f"{f}_deep_missed_recent")) and not out[f]
        out[f"{f}_deep_missed"] = deep
        out[f"{f}_missed"] = deep or (bool(row.get(f"{f}_missed_recent")) and not out[f])
        out[f"{f}_missed_in_deep_ttl"] = bool(row.get(f"{f}_missed_in_deep_ttl")) and not out[f]
        out[f"{f}_missed_at"] = _iso(row.get(f"{f}_missed_at"))
        out[f"{f}_deep_missed_at"] = _iso(row.get(f"{f}_deep_missed_at"))
    return out


async def record(
    linkedin_url: Any,
    *,
    email: str = "",
    phone: str = "",
    email_provider: str = "",
    phone_provider: str = "",
    missed: Iterable[str] = (),
    deep_missed: Iterable[str] = (),
) -> None:
    """Remember what a lookup found, which fields the normal Exa run answered
    without (``missed``) and which the deep search did (``deep_missed``).
    ``phone`` must be the candidate's PERSONAL number: it is stored as one and
    reused as one. No-op when there is nothing to say. Never raises."""
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
    deep_missed = set(deep_missed or ())
    email_missed = "email" in missed and not email
    phone_missed = "phone" in missed and not phone
    email_deep_missed = "email" in deep_missed and not email
    phone_deep_missed = "phone" in deep_missed and not phone
    if not (email or phone or email_missed or phone_missed or email_deep_missed or phone_deep_missed):
        return
    params = {
        "slug": slug,
        "email": email or None,
        "email_provider": (email_provider or None) if email else None,
        "email_missed": email_missed,
        "phone": phone or None,
        "phone_provider": (phone_provider or None) if phone else None,
        "phone_missed": phone_missed,
        "email_deep_missed": email_deep_missed,
        "phone_deep_missed": phone_deep_missed,
    }
    try:
        await asyncio.to_thread(_record_sync, params)
    except Exception as e:
        logger.warning("contact_cache write failed for %s: %s", slug, e)
