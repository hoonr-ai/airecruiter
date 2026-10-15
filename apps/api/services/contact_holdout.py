"""The Launch deep-email holdout's decision log (EXA_DEEP_SKIP_REACHABLE_PCT).

Launch PAIR buys a deep-search email run for a candidate who already holds a
personal phone, though the launch gate is phone OR email (~$0.30 per email
found). 2026-10-14, user decision: skip it for a share of those people (the
"skip" arm, by a hash of the LinkedIn slug: contact_enrichment.deep_skip_arm)
and keep it for the rest ("control") for 2 weeks, then decide on whether the
control arm's deep emails start screens.

The analysis needs the people of BOTH arms, recorded when the decision is made
and before any deep run: the contact cache only learns about the control arm
after its run (and only on a miss), which would bias who counts as eligible.
So routers/candidates.py records each eligible Launch lookup here:

  - ``record_decision``: the person's FIRST exposure (one row per LinkedIn
    slug; a later lookup of the same person changes nothing, so two lookups
    racing on different workers still make one row), with the arm, the pct,
    the job / candidate it came from, where the held phone came from
    ("seed" = the row's or the request's, "found" = a provider's mobile this
    lookup) and the normal run's outcome;
  - ``record_deep_outcome``: what the control arm's deep run did for the email
    ("email" found, "miss", "in_progress" = still going when the lookup
    stopped waiting, its answer then lands in the contact cache only,
    "failed"). The first definitive answer wins; "in_progress" / "failed" can
    be replaced by a later one.

Ids and enums only (no contact). About 10k rows a month; drop the table when
the experiment ends. Everything fails open: a DB problem is logged and the
lookup goes on as decided.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from services import contact_cache

logger = logging.getLogger(__name__)

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS contact_deep_skip_holdout (
        linkedin_slug TEXT PRIMARY KEY,
        arm TEXT NOT NULL,
        pct INTEGER NOT NULL,
        decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        jobdiva_id TEXT,
        candidate_id TEXT,
        phone_from TEXT,
        normal_outcome TEXT,
        deep_outcome TEXT,
        deep_outcome_at TIMESTAMPTZ
    );
    CREATE INDEX IF NOT EXISTS contact_deep_skip_holdout_decided_at
        ON contact_deep_skip_holdout (decided_at)
"""

DECISION_SQL = """
    INSERT INTO contact_deep_skip_holdout
        (linkedin_slug, arm, pct, jobdiva_id, candidate_id, phone_from, normal_outcome)
    VALUES (%(slug)s, %(arm)s, %(pct)s, %(jobdiva_id)s, %(candidate_id)s, %(phone_from)s, %(normal_outcome)s)
    ON CONFLICT (linkedin_slug) DO NOTHING
    RETURNING linkedin_slug
"""

DEEP_OUTCOME_SQL = """
    UPDATE contact_deep_skip_holdout
    SET deep_outcome = %(outcome)s, deep_outcome_at = NOW()
    WHERE linkedin_slug = %(slug)s
      AND arm = 'control'
      AND (deep_outcome IS NULL OR deep_outcome IN ('in_progress', 'failed'))
"""

ARMS = ("skip", "control")
DEEP_OUTCOMES = ("email", "miss", "in_progress", "failed")

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
        # 8 workers can race on first use (same pattern as contact_cache).
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('contact_deep_skip_holdout_ddl'))")
        cur.execute(CREATE_SQL)
    conn.commit()
    _table_ready = True


def _run_sync(sql: str, params: Dict[str, Any]) -> int:
    """Run one statement; the rows it returned (RETURNING) or touched."""
    conn = _connect()
    try:
        _ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            count = len(cur.fetchall()) if cur.description else cur.rowcount
        conn.commit()
        return int(count or 0)
    finally:
        conn.close()


def _text(value: Any, limit: int = 64) -> Optional[str]:
    text = str(value or "").strip()[:limit]
    return text or None


async def record_decision(
    linkedin_url: Any,
    *,
    arm: str,
    pct: int,
    jobdiva_id: Any = None,
    candidate_id: Any = None,
    phone_from: str = "",
    normal_outcome: str = "",
) -> Optional[bool]:
    """Log the person's first exposure to the holdout. True = this call
    logged it, False = the person was already logged (a later lookup), None
    = not logged (no slug, an arm that is not one, a DB problem). Never
    raises."""
    slug = contact_cache.linkedin_slug(linkedin_url)
    if not slug or arm not in ARMS:
        return None
    params = {
        "slug": slug, "arm": arm, "pct": int(pct), "jobdiva_id": _text(jobdiva_id),
        "candidate_id": _text(candidate_id, 200), "phone_from": _text(phone_from),
        "normal_outcome": _text(normal_outcome),
    }
    try:
        return await asyncio.to_thread(_run_sync, DECISION_SQL, params) > 0
    except Exception as e:
        logger.warning("contact_holdout: decision not recorded for %s: %s", slug, e)
        return None


async def record_deep_outcome(linkedin_url: Any, outcome: str) -> None:
    """Log what the control arm's deep run did for the email (see the module
    docstring). Never raises."""
    slug = contact_cache.linkedin_slug(linkedin_url)
    if not slug or outcome not in DEEP_OUTCOMES:
        return
    try:
        await asyncio.to_thread(_run_sync, DEEP_OUTCOME_SQL, {"slug": slug, "outcome": outcome})
    except Exception as e:
        logger.warning("contact_holdout: deep outcome not recorded for %s: %s", slug, e)
