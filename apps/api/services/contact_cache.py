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

The same row is the person's single-flight LEASE (``pending_*``, 2026-10-15):
one paid Exa contact lookup per person at a time across the 8 workers
(contact_enrichment.exa_step). ``claim`` takes it for LEASE_S, the holder's
``heartbeat`` keeps it (and stamps the run it started), ``release`` ends it and
hands back the late-fill contexts other lookups ``join``-ed while waiting. A
lease that lapsed is free again, except while the run started under it can
still answer (its run horizon): ``adopt_orphans`` gives such a run, whose
watcher died with its worker, to the next sweep (one worker each, FOR UPDATE
SKIP LOCKED).

Everything fails open: a DB problem means "no cache" (and no lease: the lookup
runs as it did before), never a failed lookup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote

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
        phone_deep_missed_at TIMESTAMPTZ,
        pending_token TEXT,
        pending_fields TEXT,
        pending_tier TEXT,
        pending_until TIMESTAMPTZ,
        pending_run_id TEXT,
        pending_run_at TIMESTAMPTZ,
        pending_meta JSONB
    )
"""

# Phones are reused only when marked personal (user 2026-09-28: "we only need
# personal phone number"). Rows written before the marker, including the
# backfill, were filled from mobile OR work numbers, so their phone is ignored
# and the person is looked up again.
# Deep-search misses (2026-09-29): a miss recorded before the deep search
# existed is a normal-run miss, so the next Launch still tries the deep search.
# Single-flight lease (2026-10-15): one ALTER (one lock) for its columns, and a
# partial index so the 60 s orphan sweep of every worker reads only rows with a
# run going.
MIGRATE_SQL = """
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS phone_personal BOOLEAN;
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS email_deep_missed_at TIMESTAMPTZ;
    ALTER TABLE contact_enrichment_cache ADD COLUMN IF NOT EXISTS phone_deep_missed_at TIMESTAMPTZ;
    ALTER TABLE contact_enrichment_cache
        ADD COLUMN IF NOT EXISTS pending_token TEXT,
        ADD COLUMN IF NOT EXISTS pending_fields TEXT,
        ADD COLUMN IF NOT EXISTS pending_tier TEXT,
        ADD COLUMN IF NOT EXISTS pending_until TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS pending_run_id TEXT,
        ADD COLUMN IF NOT EXISTS pending_run_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS pending_meta JSONB;
    CREATE INDEX IF NOT EXISTS contact_enrichment_cache_pending_run_idx
        ON contact_enrichment_cache (pending_run_at) WHERE pending_run_id IS NOT NULL
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

# ---------------------------------------------------------------------------
# Single-flight lease (see the module docstring)
# ---------------------------------------------------------------------------

# A lease lives this long without a heartbeat (the holder beats every 20 s).
LEASE_S = 60
# Late-fill contexts one lease collects at most (join).
MAX_JOINED = 50
# Upper bound of any run horizon: a deep run's 240 s window + the 600 s late
# watch + 30 s (contact_enrichment.run_horizon_s).
MAX_RUN_HORIZON_S = 870

# How long after its start the run of a lease ({t}) can still answer.
_HORIZON = (
    "make_interval(secs => CASE WHEN {t}.pending_tier = 'deep'"
    " THEN %(deep_horizon_s)s ELSE %(normal_horizon_s)s END)"
)
# A lease is free once it lapsed (no heartbeat for LEASE_S) and no run was
# started under it or that run is past its horizon. A lapsed lease whose run
# can still answer is an orphan: not claimable, adopted by the sweep.
_LEASE_FREE = (
    "(COALESCE({t}.pending_until, '-infinity'::timestamptz) < NOW()"
    " AND ({t}.pending_run_id IS NULL OR {t}.pending_run_at IS NULL"
    " OR {t}.pending_run_at < NOW() - " + _HORIZON + "))"
)

# The winner gets its token back; a claim while someone holds the person
# returns nothing. Contexts joined to a lease that lapsed before its run began
# are kept for the new holder.
CLAIM_SQL = """
    INSERT INTO contact_enrichment_cache AS c (
        linkedin_slug, pending_token, pending_fields, pending_tier, pending_until, pending_meta
    ) VALUES (
        %(slug)s, %(token)s, %(fields)s, %(tier)s,
        NOW() + make_interval(secs => %(lease_s)s), %(meta)s::jsonb
    )
    ON CONFLICT (linkedin_slug) DO UPDATE SET
        pending_token = EXCLUDED.pending_token,
        pending_fields = EXCLUDED.pending_fields,
        pending_tier = EXCLUDED.pending_tier,
        pending_until = EXCLUDED.pending_until,
        pending_run_id = NULL,
        pending_run_at = NULL,
        pending_meta = COALESCE(c.pending_meta, '[]'::jsonb) || EXCLUDED.pending_meta
    WHERE """ + _LEASE_FREE.format(t="c") + """
    RETURNING pending_token
"""

# Holder only. A new run id restarts the run's horizon.
HEARTBEAT_SQL = """
    UPDATE contact_enrichment_cache
    SET pending_until = NOW() + make_interval(secs => %(lease_s)s),
        pending_run_at = CASE WHEN %(run_id)s::text IS NOT NULL
                                   AND pending_run_id IS DISTINCT FROM %(run_id)s::text
                              THEN NOW() ELSE pending_run_at END,
        pending_run_id = COALESCE(%(run_id)s::text, pending_run_id)
    WHERE linkedin_slug = %(slug)s AND pending_token = %(token)s
"""

# Holder only; hands back the contexts joined while it held the person.
RELEASE_SQL = """
    WITH held AS (
        SELECT linkedin_slug, pending_meta FROM contact_enrichment_cache
        WHERE linkedin_slug = %(slug)s AND pending_token = %(token)s
        FOR UPDATE
    )
    UPDATE contact_enrichment_cache AS c
    SET pending_token = NULL, pending_fields = NULL, pending_tier = NULL, pending_until = NULL,
        pending_run_id = NULL, pending_run_at = NULL, pending_meta = NULL
    FROM held
    WHERE c.linkedin_slug = held.linkedin_slug
    RETURNING held.pending_meta
"""

JOIN_SQL = """
    UPDATE contact_enrichment_cache
    SET pending_meta = COALESCE(pending_meta, '[]'::jsonb) || jsonb_build_array(%(ctx)s::jsonb)
    WHERE linkedin_slug = %(slug)s
      AND pending_token IS NOT NULL
      AND NOT COALESCE(pending_meta, '[]'::jsonb) @> jsonb_build_array(%(ctx)s::jsonb)
      AND jsonb_array_length(COALESCE(pending_meta, '[]'::jsonb)) < %(max_joined)s
"""

HOLDER_SQL = """
    SELECT c.pending_fields, c.pending_tier, c.pending_run_id
    FROM contact_enrichment_cache AS c
    WHERE c.linkedin_slug = %(slug)s
      AND c.pending_token IS NOT NULL
      AND NOT """ + _LEASE_FREE.format(t="c") + """
"""

# Orphans: a lapsed lease whose run can still answer. Each row goes to one
# sweeping worker (SKIP LOCKED), under a new token and a fresh lease; the watch
# it gets lasts until the run's horizon.
ADOPT_SQL = """
    UPDATE contact_enrichment_cache AS c
    SET pending_token = md5(random()::text || clock_timestamp()::text || c.linkedin_slug),
        pending_until = NOW() + make_interval(secs => %(lease_s)s)
    FROM (
        SELECT o.linkedin_slug FROM contact_enrichment_cache AS o
        WHERE o.pending_run_id IS NOT NULL
          AND o.pending_token IS NOT NULL
          AND COALESCE(o.pending_until, '-infinity'::timestamptz) < NOW()
          AND o.pending_run_at > NOW() - """ + _HORIZON.format(t="o") + """
        ORDER BY o.pending_run_at
        LIMIT %(limit)s
        FOR UPDATE SKIP LOCKED
    ) AS orphan
    WHERE c.linkedin_slug = orphan.linkedin_slug
    RETURNING c.linkedin_slug, c.pending_token, c.pending_run_id, c.pending_fields, c.pending_tier,
              EXTRACT(EPOCH FROM (c.pending_run_at + """ + _HORIZON.format(t="c") + """ - NOW())) AS remaining_s
"""


def _enabled() -> bool:
    return os.getenv("CONTACT_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    """CONTACT_CACHE_ENABLED (default on): the cache and its lease."""
    return _enabled()


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


def profile_url(slug: str) -> str:
    """A LinkedIn profile URL whose ``linkedin_slug`` is ``slug`` (for work
    that only has the cache key, such as an adopted late watch)."""
    return f"https://www.linkedin.com/in/{quote(slug, safe='')}" if slug else ""


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


# ---------------------------------------------------------------------------
# Single-flight lease. Each call fails open: a DB error is logged and reported
# as "error" (claim / holder) or None, and the caller looks the person up
# without a lease, as before the lease existed.
# ---------------------------------------------------------------------------

def _lease_sync(sql: str, params: Dict[str, Any], fetch: str = "") -> Any:
    """Run one lease statement; ``fetch`` "one" / "all" returns rows, else
    the row count."""
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "one":
                out: Any = cur.fetchone()
            elif fetch == "all":
                out = cur.fetchall() or []
            else:
                out = cur.rowcount
            cols = [d[0] for d in cur.description] if fetch and cur.description else []
        conn.commit()
    finally:
        conn.close()
    if fetch == "one":
        return _as_dict(out, cols) if out else None
    if fetch == "all":
        return [_as_dict(r, cols) for r in out]
    return out


def _as_dict(row: Any, cols: List[str]) -> Dict[str, Any]:
    return dict(row) if isinstance(row, dict) else dict(zip(cols, row))


def _horizons(normal_horizon_s: int, deep_horizon_s: int) -> Dict[str, int]:
    return {
        "normal_horizon_s": max(0, min(int(normal_horizon_s), MAX_RUN_HORIZON_S)),
        "deep_horizon_s": max(0, min(int(deep_horizon_s), MAX_RUN_HORIZON_S)),
    }


def _fields_text(fields: Iterable[str]) -> str:
    wanted = set(fields or ())
    return ",".join(f for f in ("email", "phone") if f in wanted)


def _fields_of(text: Any) -> Tuple[str, ...]:
    return tuple(f for f in str(text or "").split(",") if f)


def _contexts(meta: Any) -> List[Dict[str, Any]]:
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return []
    return [m for m in (meta or []) if isinstance(m, dict)] if isinstance(meta, list) else []


async def claim(
    slug: str,
    fields: Iterable[str],
    tier: str,
    *,
    ctx: Optional[Dict[str, Any]] = None,
    normal_horizon_s: int = MAX_RUN_HORIZON_S,
    deep_horizon_s: int = MAX_RUN_HORIZON_S,
) -> Tuple[str, str]:
    """Take the person's lease for a paid lookup of ``fields`` at ``tier``
    ("normal" / "deep"): ("won", token), ("held", "") while another lookup
    holds it, or ("error", "") (fail open). ``ctx`` (candidate_id /
    jobdiva_id) is the holder's own late-fill context, kept in pending_meta so
    an adopted watch can fill its rows too."""
    if not slug or not _enabled():
        return "error", ""
    token = uuid.uuid4().hex
    params = {
        "slug": slug, "token": token, "fields": _fields_text(fields), "tier": tier,
        "lease_s": LEASE_S, "meta": json.dumps([ctx] if ctx else []),
        **_horizons(normal_horizon_s, deep_horizon_s),
    }
    try:
        row = await asyncio.to_thread(_lease_sync, CLAIM_SQL, params, "one")
    except Exception as e:
        logger.warning("contact_cache lease claim failed: %s", e)
        return "error", ""
    return ("won", token) if row and row.get("pending_token") == token else ("held", "")


async def heartbeat(slug: str, token: str, run_id: str = "") -> Optional[int]:
    """Keep the lease another LEASE_S (holder only), stamping ``run_id`` (the
    run just started) when given. Rows updated: 0 = someone else holds the
    person now; None = a DB error (the holder carries on)."""
    if not (slug and token):
        return None
    params = {"slug": slug, "token": token, "run_id": run_id or None, "lease_s": LEASE_S}
    try:
        return int(await asyncio.to_thread(_lease_sync, HEARTBEAT_SQL, params))
    except Exception as e:
        logger.warning("contact_cache lease heartbeat failed: %s", e)
        return None


async def release(slug: str, token: str) -> Optional[List[Dict[str, Any]]]:
    """End the lease (holder only). Returns the late-fill contexts joined to
    it ([] for none), or None when this token no longer holds it or the DB
    failed (the lease then lapses on its own)."""
    if not (slug and token):
        return None
    try:
        row = await asyncio.to_thread(_lease_sync, RELEASE_SQL, {"slug": slug, "token": token}, "one")
    except Exception as e:
        logger.warning("contact_cache lease release failed: %s", e)
        return None
    if row is None:
        return None
    return _contexts(row.get("pending_meta"))


async def join(slug: str, ctx: Dict[str, Any]) -> bool:
    """Add a late-fill context (candidate_id / jobdiva_id) to the lease held
    on the person, so whoever finishes its run fills that candidate's rows
    too. False when nobody holds it (or it is already there, or full)."""
    if not (slug and ctx and ctx.get("candidate_id")):
        return False
    params = {"slug": slug, "ctx": json.dumps(ctx), "max_joined": MAX_JOINED}
    try:
        return bool(await asyncio.to_thread(_lease_sync, JOIN_SQL, params))
    except Exception as e:
        logger.warning("contact_cache lease join failed: %s", e)
        return False


async def holder(
    slug: str,
    *,
    normal_horizon_s: int = MAX_RUN_HORIZON_S,
    deep_horizon_s: int = MAX_RUN_HORIZON_S,
) -> Dict[str, Any]:
    """Who holds the person: {"state": "held", "fields", "tier", "run_id"},
    {"state": "free"} (a claim would win) or {"state": "error"}."""
    if not slug or not _enabled():
        return {"state": "error"}
    params = {"slug": slug, **_horizons(normal_horizon_s, deep_horizon_s)}
    try:
        row = await asyncio.to_thread(_lease_sync, HOLDER_SQL, params, "one")
    except Exception as e:
        logger.warning("contact_cache lease read failed: %s", e)
        return {"state": "error"}
    if not row:
        return {"state": "free"}
    return {
        "state": "held",
        "fields": _fields_of(row.get("pending_fields")),
        "tier": "deep" if row.get("pending_tier") == "deep" else "normal",
        "run_id": str(row.get("pending_run_id") or ""),
    }


async def adopt_orphans(
    *,
    limit: int = 50,
    normal_horizon_s: int = MAX_RUN_HORIZON_S,
    deep_horizon_s: int = MAX_RUN_HORIZON_S,
) -> List[Dict[str, Any]]:
    """Take over up to ``limit`` runs whose lease lapsed while they can still
    answer (their watcher died with its worker). Each comes back once, to one
    caller, as {"slug", "token", "run_id", "fields", "tier", "remaining_s"}
    under a fresh lease the caller's watch must keep alive. [] on a DB error."""
    if not _enabled():
        return []
    params = {"limit": max(1, int(limit)), "lease_s": LEASE_S, **_horizons(normal_horizon_s, deep_horizon_s)}
    try:
        rows = await asyncio.to_thread(_lease_sync, ADOPT_SQL, params, "all")
    except Exception as e:
        logger.warning("contact_cache orphan sweep failed: %s", e)
        return []
    return [
        {
            "slug": str(r.get("linkedin_slug") or ""),
            "token": str(r.get("pending_token") or ""),
            "run_id": str(r.get("pending_run_id") or ""),
            "fields": _fields_of(r.get("pending_fields")),
            "tier": "deep" if r.get("pending_tier") == "deep" else "normal",
            "remaining_s": max(0.0, float(r.get("remaining_s") or 0.0)),
        }
        for r in rows
    ]
