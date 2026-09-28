"""Seed the person-level contact cache from lookups we already paid for.

contact_enrichment_cache (services/contact_cache.py) starts empty, so without
this every person enriched before it existed would be bought again on their
next job. Two provider-verified records live in sourced_candidates:

  - data.zoominfo_contact_enrichment: written by /candidates/enrich-contact
    (Launch PAIR, phone button) whenever a provider found something;
  - data.enhanced_info.contact_enrichment_provider: set by the sourcing-time
    chain on the row whose email/phone columns it filled.

Recruiter-typed phones are not marked as such anywhere, and profile-text (LLM)
contact is unreliable, so neither is used. Only hits are seeded (no record of
past misses exists), and only into empty cache fields. Runs once per database automatically (contact_cache._ensure_table) and
on demand via scripts/backfill_contact_cache.py.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services import contact_cache

logger = logging.getLogger(__name__)

# Marker row: a slug the URL parser can never produce ('#' is excluded).
DONE_SLUG = "#backfill"

SOURCE_SQL = """
    SELECT source, profile_url, email, phone, updated_at,
           data->'zoominfo_contact_enrichment' AS lookup,
           data->'enhanced_info'->>'contact_enrichment_provider' AS sourcing_provider
    FROM sourced_candidates
    WHERE data ? 'zoominfo_contact_enrichment'
       OR data->'enhanced_info' ? 'contact_enrichment_provider'
"""

# Fills only EMPTY cache fields. A live cache value always wins: a sourced
# row's updated_at moves on any later write (launch, status), so it is not a
# trustworthy "found at" to compare against.
UPSERT_SQL = """
    INSERT INTO contact_enrichment_cache AS c (
        linkedin_slug, email, email_provider, email_found_at,
        phone, phone_provider, phone_found_at, updated_at
    ) VALUES (
        %(slug)s, %(email)s, %(email_provider)s, %(email_found_at)s,
        %(phone)s, %(phone_provider)s, %(phone_found_at)s, NOW()
    )
    ON CONFLICT (linkedin_slug) DO UPDATE SET
        email = COALESCE(c.email, EXCLUDED.email),
        email_provider = CASE WHEN c.email IS NULL THEN EXCLUDED.email_provider ELSE c.email_provider END,
        email_found_at = CASE WHEN c.email IS NULL THEN EXCLUDED.email_found_at ELSE c.email_found_at END,
        phone = COALESCE(c.phone, EXCLUDED.phone),
        phone_provider = CASE WHEN c.phone IS NULL THEN EXCLUDED.phone_provider ELSE c.phone_provider END,
        phone_found_at = CASE WHEN c.phone IS NULL THEN EXCLUDED.phone_found_at ELSE c.phone_found_at END,
        updated_at = NOW()
"""

MARK_DONE_SQL = """
    INSERT INTO contact_enrichment_cache (linkedin_slug, updated_at) VALUES (%(slug)s, NOW())
    ON CONFLICT (linkedin_slug) DO UPDATE SET updated_at = NOW()
"""

IS_DONE_SQL = "SELECT 1 FROM contact_enrichment_cache WHERE linkedin_slug = %(slug)s"


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import json
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


def _utc(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
    return None


def _clean_email(value: Any) -> str:
    from utils.email_utils import is_placeholder_email

    email = str(value or "").strip().lower()
    if "@" not in email or is_placeholder_email(email):
        return ""
    return email


def _clean_phone(value: Any) -> str:
    from services.contact_enrichment import _normalise_phone

    phone = _normalise_phone(str(value or ""))
    return phone if sum(1 for ch in phone if ch.isdigit()) >= 7 else ""


def _provider(value: Any) -> str:
    p = str(value or "").strip().lower()
    return "" if p in {"", "none", "cache"} else p


def facts_from_row(row: Dict[str, Any]) -> List[Tuple[str, str, str, str, Optional[_dt.datetime]]]:
    """(slug, field, value, provider, found_at) facts one sourced row supports."""
    facts: List[Tuple[str, str, str, str, Optional[_dt.datetime]]] = []
    lookup = _as_dict(row.get("lookup"))
    if lookup:
        slug = contact_cache.linkedin_slug(lookup.get("linkedin_url") or row.get("profile_url"))
        provider = _provider(lookup.get("provider")) or "backfill"
        at = _utc(lookup.get("enriched_at")) or _utc(row.get("updated_at"))
        email = _clean_email(lookup.get("workEmail")) or _clean_email(lookup.get("personalEmail"))
        phones = [lookup.get("mobilePhone"), lookup.get("workPhone"), *(lookup.get("phoneCandidates") or [])]
        phone = next((p for p in (_clean_phone(x) for x in phones) if p), "")
        if slug and email:
            facts.append((slug, "email", email, provider, at))
        if slug and phone:
            facts.append((slug, "phone", phone, provider, at))

    provider = _provider(row.get("sourcing_provider"))
    if provider:
        slug = contact_cache.linkedin_slug(row.get("profile_url"))
        at = _utc(row.get("updated_at"))
        email = _clean_email(row.get("email"))
        phone = _clean_phone(row.get("phone"))
        if slug and email:
            facts.append((slug, "email", email, provider, at))
        if slug and phone:
            facts.append((slug, "phone", phone, provider, at))
    return facts


def plan(rows: Iterable[Dict[str, Any]], *, now: Optional[_dt.datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Newest fresh value per person and field -> upsert params keyed by slug."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    oldest = now - _dt.timedelta(days=contact_cache.hit_ttl_days())
    people: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for slug, field, value, provider, at in facts_from_row(row):
            at = at or now
            if at < oldest:
                continue  # already past the cache's reuse window
            p = people.setdefault(slug, {
                "slug": slug, "email": None, "email_provider": None, "email_found_at": None,
                "phone": None, "phone_provider": None, "phone_found_at": None,
            })
            if p[f"{field}_found_at"] is None or at > p[f"{field}_found_at"]:
                p[field] = value
                p[f"{field}_provider"] = provider
                p[f"{field}_found_at"] = at
    return people


def run(conn, *, apply: bool) -> Dict[str, Any]:
    """Scan sourced_candidates and (when apply) upsert the cache. The caller
    owns the transaction (commit / rollback)."""
    from psycopg2.extras import RealDictCursor

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(SOURCE_SQL)
        rows = [dict(r) for r in cur.fetchall() or []]
    people = plan(rows)
    summary = {
        "rows_scanned": len(rows),
        "people": len(people),
        "emails": sum(1 for p in people.values() if p["email"]),
        "phones": sum(1 for p in people.values() if p["phone"]),
        "applied": bool(apply),
    }
    if apply and people:
        with conn.cursor() as cur:
            for params in people.values():
                cur.execute(UPSERT_SQL, params)
    return summary


def run_once(conn) -> Optional[Dict[str, Any]]:
    """Backfill unless this database already has the done-marker. Serialised
    across workers by an advisory lock; commits on success, rolls back (and
    leaves the marker unset, so a later call retries) on failure."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('contact_enrichment_cache_backfill'))")
            cur.execute(IS_DONE_SQL, {"slug": DONE_SLUG})
            if cur.fetchone():
                conn.commit()
                return None
        summary = run(conn, apply=True)
        with conn.cursor() as cur:
            cur.execute(MARK_DONE_SQL, {"slug": DONE_SLUG})
        conn.commit()
        logger.warning("contact_cache backfill done: %s", summary)
        return summary
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("contact_cache backfill failed (will retry on next start): %s", e)
        return None
