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
- ``outreach_opt_out_audit`` — who stopped a candidate's outreach, why, and
  what pair-bot replied. dnc_list.notes cannot carry this for an email-only
  opt-out (dnc_list is keyed on phone), and "why did we stop contacting them"
  is the first question asked when a candidate escalates.

Writes land here as well as in pair-bot (see services/opt_out.py). pair-bot
owns the actual send cancellation; these rows are what stop *us* re-launching
the same person on the next import, via the existing dnc_stopped_at gates in
routers/candidates.py and routers/engagement.py.
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
from utils.pii import mask_email, mask_phone

logger = logging.getLogger(__name__)


_ENGINE: Optional[sqlalchemy.engine.Engine] = None
_engine_lock = threading.Lock()


def _get_engine() -> sqlalchemy.engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        with _engine_lock:
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
                    # Without this, SQLAlchemy appends "[parameters: (...)]" to every
                    # DBAPI error string — which for this module means a candidate's
                    # email and phone. That string is logged, stored in
                    # outreach_opt_out_audit.local_result, AND returned to the browser
                    # as local.error, so masking the log arguments alone would not
                    # cover it. The statement and the driver's own message survive.
                    hide_parameters=True,
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
            # Audit trail for recruiter-initiated "Stop outreach". Provisioned
            # here rather than in its own lifespan step because it shares the
            # dnc_list lifecycle (and the same sourced_candidates dependency).
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS outreach_opt_out_audit (
                    id SERIAL PRIMARY KEY,
                    action TEXT NOT NULL,
                    email TEXT,
                    phone TEXT,
                    interview_id TEXT,
                    candidate_id TEXT,
                    scope TEXT,
                    channels TEXT,
                    reason TEXT,
                    created_by TEXT,
                    pairbot_ok BOOLEAN,
                    pairbot_response JSONB,
                    local_result JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_opt_out_audit_created_at "
                "ON outreach_opt_out_audit(created_at DESC)"
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


# ---------------------------------------------------------------------------
# Local suppression writes (recruiter "Stop outreach")
# ---------------------------------------------------------------------------
# pair-bot is the system that actually cancels queued sends; these writes are
# what keep *pair* from re-launching the same person. Without them a candidate
# re-sourced next month gets a fresh sourced_candidates row with a NULL
# dnc_stopped_at and sails straight back through Launch PAIR.
#
# Phone matching mirrors utils.phone.normalize_phone in SQL. Rather than
# re-implement its 10-vs-11-digit CASE in the query (which would put a literal
# `%` inside a parameterised text() — a paramstyle escaping hazard), we compare
# the stripped column against BOTH digit forms of the already-normalized
# number. Equivalent, and safe to bind.
_SC_PHONE_DIGITS_SQL = r"regexp_replace(phone, '\D', '', 'g')"


def _phone_digit_forms(phone_normalized: str) -> list:
    """['14408405137', '4408405137'] — the two ways the raw column may read."""
    forms = [phone_normalized]
    if len(phone_normalized) == 11 and phone_normalized.startswith("1"):
        forms.append(phone_normalized[1:])
    return forms


def suppress_contact_locally(
    phone: Optional[str] = None,
    email: Optional[str] = None,
    reason: Optional[str] = None,
    created_by: str = "unknown",
) -> dict:
    """Record a local suppression for a phone and/or email.

    - Upserts the phone into ``dnc_list`` so the Launch PAIR save gate
      (routers/candidates.py) and the payload gate (routers/engagement.py)
      both reject it, now and after any re-import.
    - Stamps ``dnc_stopped_at`` on every matching ``sourced_candidates`` row
      so already-launched rows stop feeding outreach.

    Known gap: ``dnc_list`` is keyed on phone, so an **email-only** opt-out
    leaves no entry there — it only stamps the rows that exist today, and a
    re-import of that candidate would not be gated locally. pair-bot still
    suppresses them (its suppression is contact-keyed and survives re-import)
    and it owns every send, so the candidate is not contacted; what is lost is
    pair's own second line of defence. Widening dnc_list to hold addresses
    would change the shape of the read model the sourcing table consumes
    (``GET /dnc/keys``), so it is deliberately left for a follow-up.

    Never raises: the caller has already stopped outreach at pair-bot and must
    report that truthfully even if this bookkeeping fails.
    """
    result = {
        # True when a row was newly INSERTed / UPDATEd by THIS call.
        "dnc_phone_added": False,
        "candidates_stopped": 0,
        # True when the contact is suppressed here once this call is done,
        # whether or not this call is what did it. Callers deciding what to
        # tell the recruiter want this one: an idempotent re-click, or a
        # candidate already on the imported DNC list, changes no rows and
        # would otherwise look indistinguishable from a failed write.
        "locally_suppressed": False,
        "phone": None,
        "email": None,
        "error": None,
    }
    phone_norm = None
    if phone:
        from utils.phone import normalize_phone

        phone_norm = normalize_phone(phone)
        result["phone"] = phone_norm
        if phone and not phone_norm:
            logger.warning(
                "opt_out_local_phone_unnormalizable phone=%s", mask_phone(phone)
            )
    email_norm = (email or "").strip().lower() or None
    result["email"] = email_norm

    if not phone_norm and not email_norm:
        result["error"] = "no normalizable phone or email"
        return result

    notes = f"opt-out by {created_by}" + (f": {reason}" if reason else "")
    try:
        engine = _get_engine()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if phone_norm:
                res = conn.execute(
                    text(
                        "INSERT INTO dnc_list (phone, source, notes) "
                        "VALUES (:phone, 'opt_out', :notes) "
                        "ON CONFLICT (phone) DO NOTHING"
                    ),
                    {"phone": phone_norm, "notes": notes[:1000]},
                )
                result["dnc_phone_added"] = bool(res.rowcount and res.rowcount > 0)

            clauses = []
            params: dict = {}
            if phone_norm:
                forms = _phone_digit_forms(phone_norm)
                params["p0"] = forms[0]
                placeholders = [":p0"]
                if len(forms) > 1:
                    params["p1"] = forms[1]
                    placeholders.append(":p1")
                clauses.append(
                    f"(phone IS NOT NULL AND phone <> '' "
                    f"AND {_SC_PHONE_DIGITS_SQL} IN ({', '.join(placeholders)}))"
                )
            if email_norm:
                params["email"] = email_norm
                clauses.append("(email IS NOT NULL AND LOWER(email) = :email)")

            stopped = conn.execute(
                text(
                    "UPDATE sourced_candidates SET dnc_stopped_at = NOW() "
                    "WHERE dnc_stopped_at IS NULL AND (" + " OR ".join(clauses) + ")"
                ),
                params,
            )
            result["candidates_stopped"] = stopped.rowcount or 0

            # Read back the resulting state rather than inferring it from the
            # rowcounts above. Both writes are no-ops when the contact was
            # already suppressed, and "no rows changed" must not be reported as
            # "not suppressed".
            listed = False
            if phone_norm:
                listed = bool(
                    conn.execute(
                        text("SELECT 1 FROM dnc_list WHERE phone = :phone LIMIT 1"),
                        {"phone": phone_norm},
                    ).fetchone()
                )
            stamped = conn.execute(
                text(
                    "SELECT 1 FROM sourced_candidates "
                    "WHERE dnc_stopped_at IS NOT NULL AND ("
                    + " OR ".join(clauses)
                    + ") LIMIT 1"
                ),
                params,
            ).fetchone()
            result["locally_suppressed"] = bool(listed or stamped)
    except Exception as e:  # noqa: BLE001 — bookkeeping must not mask the stop
        logger.error(f"suppress_contact_locally failed: {e}", exc_info=True)
        result["error"] = str(e)
        return result

    invalidate_dnc_cache()
    # Masked: the full values are already in dnc_list / outreach_opt_out_audit,
    # which are row-access-controlled. See utils/pii.py.
    logger.info(
        "opt_out_local_suppressed phone=%s email=%s dnc_added=%s stopped=%s "
        "suppressed=%s by=%s",
        mask_phone(phone_norm), mask_email(email_norm), result["dnc_phone_added"],
        result["candidates_stopped"], result["locally_suppressed"], created_by,
    )
    return result


def release_contact_locally(
    phone: Optional[str] = None,
    email: Optional[str] = None,
    created_by: str = "unknown",
) -> dict:
    """Undo suppress_contact_locally: drop the DNC row, clear dnc_stopped_at.

    Only removes ``dnc_list`` rows this feature created (``source='opt_out'``).
    An imported Zoom DNC entry is a separate legal instruction and is not
    something a recruiter clicking "Resume outreach" gets to overrule.

    Like pair-bot's opt-in, this does NOT re-queue cancelled outreach — it only
    clears the flags that block a future launch.
    """
    result = {
        "dnc_phone_removed": False,
        "dnc_phone_retained_other_source": False,
        "candidates_released": 0,
        "phone": None,
        "email": None,
        "error": None,
    }
    phone_norm = None
    if phone:
        from utils.phone import normalize_phone

        phone_norm = normalize_phone(phone)
        result["phone"] = phone_norm
    email_norm = (email or "").strip().lower() or None
    result["email"] = email_norm

    if not phone_norm and not email_norm:
        result["error"] = "no normalizable phone or email"
        return result

    try:
        engine = _get_engine()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if not phone_norm and email_norm:
                # Resolve the candidate's phone from sourced_candidates
                phone_row = conn.execute(
                    text("SELECT phone FROM sourced_candidates WHERE email IS NOT NULL AND LOWER(email) = :email AND phone IS NOT NULL AND phone <> '' LIMIT 1"),
                    {"email": email_norm}
                ).fetchone()
                if phone_row and phone_row[0]:
                    from utils.phone import normalize_phone
                    resolved_phone = normalize_phone(phone_row[0])
                    if resolved_phone:
                        phone_norm = resolved_phone
                        result["phone"] = phone_norm

            if phone_norm:
                res = conn.execute(
                    text("DELETE FROM dnc_list WHERE phone = :phone AND source = 'opt_out'"),
                    {"phone": phone_norm},
                )
                result["dnc_phone_removed"] = bool(res.rowcount and res.rowcount > 0)
                if not result["dnc_phone_removed"]:
                    still = conn.execute(
                        text("SELECT 1 FROM dnc_list WHERE phone = :phone LIMIT 1"),
                        {"phone": phone_norm},
                    ).fetchone()
                    result["dnc_phone_retained_other_source"] = bool(still)

            clauses = []
            params: dict = {}
            if phone_norm:
                forms = _phone_digit_forms(phone_norm)
                params["p0"] = forms[0]
                placeholders = [":p0"]
                if len(forms) > 1:
                    params["p1"] = forms[1]
                    placeholders.append(":p1")
                clauses.append(
                    f"(phone IS NOT NULL AND phone <> '' "
                    f"AND {_SC_PHONE_DIGITS_SQL} IN ({', '.join(placeholders)}))"
                )
            if email_norm:
                params["email"] = email_norm
                clauses.append("(email IS NOT NULL AND LOWER(email) = :email)")

            # A DNC entry we did not create (e.g. the imported Zoom list) is a
            # separate instruction to stop calling this number. Release nothing
            # while it stands — not even rows matched via the email — because
            # the person behind both identities is the same one that list names.
            if phone_norm and result["dnc_phone_retained_other_source"]:
                logger.info(
                    "opt_out_local_release_blocked phone=%s reason=dnc_other_source",
                    mask_phone(phone_norm),
                )
            else:
                released = conn.execute(
                    text(
                        "UPDATE sourced_candidates SET dnc_stopped_at = NULL "
                        "WHERE dnc_stopped_at IS NOT NULL AND ("
                        + " OR ".join(clauses)
                        + ")"
                    ),
                    params,
                )
                result["candidates_released"] = released.rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"release_contact_locally failed: {e}", exc_info=True)
        result["error"] = str(e)
        return result

    invalidate_dnc_cache()
    logger.info(
        "opt_out_local_released phone=%s email=%s dnc_removed=%s released=%s by=%s",
        mask_phone(phone_norm), mask_email(email_norm), result["dnc_phone_removed"],
        result["candidates_released"], created_by,
    )
    return result


def local_suppression_status(
    phone: Optional[str] = None, email: Optional[str] = None
) -> dict:
    """Pair's own view: is this contact blocked from a future launch here?"""
    out = {"dnc_listed": False, "dnc_source": None, "stopped_rows": 0, "error": None}
    phone_norm = None
    if phone:
        from utils.phone import normalize_phone

        phone_norm = normalize_phone(phone)
    email_norm = (email or "").strip().lower() or None
    if not phone_norm and not email_norm:
        return out
    try:
        engine = _get_engine()
        with engine.connect() as conn:
            if not phone_norm and email_norm:
                # Resolve phone
                phone_row = conn.execute(
                    text("SELECT phone FROM sourced_candidates WHERE email IS NOT NULL AND LOWER(email) = :email AND phone IS NOT NULL AND phone <> '' LIMIT 1"),
                    {"email": email_norm}
                ).fetchone()
                if phone_row and phone_row[0]:
                    from utils.phone import normalize_phone
                    phone_norm = normalize_phone(phone_row[0])

            if phone_norm:
                row = conn.execute(
                    text("SELECT source FROM dnc_list WHERE phone = :phone LIMIT 1"),
                    {"phone": phone_norm},
                ).fetchone()
                if row:
                    out["dnc_listed"] = True
                    out["dnc_source"] = row[0]

            clauses = []
            params: dict = {}
            if phone_norm:
                forms = _phone_digit_forms(phone_norm)
                params["p0"] = forms[0]
                placeholders = [":p0"]
                if len(forms) > 1:
                    params["p1"] = forms[1]
                    placeholders.append(":p1")
                clauses.append(
                    f"(phone IS NOT NULL AND phone <> '' "
                    f"AND {_SC_PHONE_DIGITS_SQL} IN ({', '.join(placeholders)}))"
                )
            if email_norm:
                params["email"] = email_norm
                clauses.append("(email IS NOT NULL AND LOWER(email) = :email)")
            cnt = conn.execute(
                text(
                    "SELECT COUNT(*) FROM sourced_candidates "
                    "WHERE dnc_stopped_at IS NOT NULL AND (" + " OR ".join(clauses) + ")"
                ),
                params,
            ).fetchone()
            out["stopped_rows"] = int(cnt[0]) if cnt else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"local_suppression_status failed: {e}")
        out["error"] = str(e)
    return out


def record_opt_out_audit(
    action: str,
    email: Optional[str],
    phone: Optional[str],
    interview_id: Optional[str],
    candidate_id: Optional[str],
    scope: Optional[str],
    channels: Optional[str],
    reason: Optional[str],
    created_by: str,
    pairbot_ok: bool,
    pairbot_response: Optional[dict],
    local_result: Optional[dict],
) -> None:
    """Best-effort audit insert. Never raises — an audit failure must not turn
    a successful stop into an error the recruiter reads as "still calling"."""
    import json as _json

    try:
        engine = _get_engine()
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "INSERT INTO outreach_opt_out_audit "
                    "(action, email, phone, interview_id, candidate_id, scope, channels, "
                    " reason, created_by, pairbot_ok, pairbot_response, local_result) "
                    "VALUES (:action, :email, :phone, :interview_id, :candidate_id, :scope, "
                    " :channels, :reason, :created_by, :pairbot_ok, "
                    " CAST(:pairbot_response AS JSONB), CAST(:local_result AS JSONB))"
                ),
                {
                    "action": action,
                    "email": email,
                    "phone": phone,
                    "interview_id": str(interview_id) if interview_id is not None else None,
                    "candidate_id": candidate_id,
                    "scope": scope,
                    "channels": channels,
                    "reason": (reason or None),
                    "created_by": created_by,
                    "pairbot_ok": pairbot_ok,
                    "pairbot_response": _json.dumps(pairbot_response or {}),
                    "local_result": _json.dumps(local_result or {}),
                },
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"record_opt_out_audit failed: {e}")
