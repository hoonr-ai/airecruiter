"""Idempotent DDL bootstrap for the person-level candidate pool.

Mirrors `scripts/migrations/001_global_candidate_pool.sql` so a deploy that
hasn't had the migration applied yet still boots with working tables. The SQL
file remains the reviewable source of truth; this module exists for the same
reason `_ensure_sourced_candidates_schema` does.

Deliberately CREATE-only: no `ALTER TABLE` here. ALTER takes an ACCESS
EXCLUSIVE lock, and running one on the startup path is what used to stall
concurrent readers and produce `LockNotAvailable` 500s. Column additions to
these tables go in a numbered migration file and are applied by hand.

Every index below is on a table this module just created, so the indexes are
built against zero or few rows — plain `CREATE INDEX` is safe. Any future index
on `sourced_candidates` / `engage_interview_audit` must use `CONCURRENTLY` from
a migration file instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

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
            raise RuntimeError("DATABASE_URL not configured for candidate_pool_schema")
        _ENGINE = sqlalchemy.create_engine(
            url,
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _ENGINE


_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS candidate_person (
        person_id        BIGSERIAL PRIMARY KEY,
        display_name     TEXT,
        primary_email    TEXT,
        primary_phone    TEXT,
        headline         TEXT,
        location         TEXT,
        profile_url      TEXT,
        primary_key_type TEXT,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_identity_key (
        key_type   TEXT   NOT NULL,
        key_value  TEXT   NOT NULL,
        person_id  BIGINT NOT NULL REFERENCES candidate_person(person_id) ON DELETE CASCADE,
        confidence REAL   NOT NULL DEFAULT 1.0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (key_type, key_value),
        CONSTRAINT candidate_identity_key_type_chk CHECK (
            key_type IN ('linkedin_url', 'jobdiva_id', 'email', 'phone', 'resume_hash', 'source_cid')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_outreach (
        id                BIGSERIAL PRIMARY KEY,
        person_id         BIGINT NOT NULL REFERENCES candidate_person(person_id) ON DELETE CASCADE,
        candidate_id      TEXT   NOT NULL,
        jobdiva_id        TEXT   NOT NULL,
        interview_id      TEXT,
        tier              TEXT   NOT NULL DEFAULT 'full',
        screening_level   TEXT,
        status            TEXT   NOT NULL DEFAULT 'sent',
        sent_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        first_activity_at TIMESTAMPTZ,
        completed_at      TIMESTAMPTZ,
        outcome           TEXT,
        score             NUMERIC,
        total_score       NUMERIC,
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT candidate_outreach_tier_chk CHECK (tier IN ('full', 'recheck', 'none')),
        CONSTRAINT candidate_outreach_outcome_chk CHECK (
            outcome IS NULL OR outcome IN ('pass', 'fail')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_screen_fact (
        person_id    BIGINT NOT NULL REFERENCES candidate_person(person_id) ON DELETE CASCADE,
        fact_key     TEXT   NOT NULL,
        fact_value   TEXT,
        normalized   JSONB,
        source       TEXT   NOT NULL DEFAULT 'pair_screen',
        interview_id TEXT,
        jobdiva_id   TEXT,
        captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at   TIMESTAMPTZ,
        PRIMARY KEY (person_id, fact_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_pool_state (
        person_id         BIGINT PRIMARY KEY REFERENCES candidate_person(person_id) ON DELETE CASCADE,
        bucket            TEXT   NOT NULL DEFAULT 'never_contacted',
        last_outreach_at  TIMESTAMPTZ,
        last_job_id       TEXT,
        last_job_title    TEXT,
        last_tier         TEXT,
        last_completed_at TIMESTAMPTZ,
        last_outcome      TEXT,
        last_score        NUMERIC,
        attempts_90d      INT    NOT NULL DEFAULT 0,
        attempts_total    INT    NOT NULL DEFAULT 0,
        next_eligible_at  TIMESTAMPTZ,
        suppressed_reason TEXT,
        facts_fresh_until TIMESTAMPTZ,
        display_name      TEXT,
        headline          TEXT,
        location          TEXT,
        has_contact       BOOLEAN NOT NULL DEFAULT FALSE,
        -- COALESCE(last_outreach_at, last_completed_at, epoch). NOT NULL and
        -- sorted DESC alongside person_id DESC so list pagination is a keyset
        -- range scan; see the migration file for why the nullable column can't
        -- serve that role directly.
        sort_at           TIMESTAMPTZ NOT NULL DEFAULT to_timestamp(0),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT candidate_pool_state_bucket_chk CHECK (
            bucket IN ('in_progress', 'completed', 'no_response', 'never_contacted', 'suppressed')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_pool_reconcile_lock (
        lock_name   TEXT PRIMARY KEY,
        claimed_at  TIMESTAMPTZ,
        claimed_by  TEXT,
        finished_at TIMESTAMPTZ
    )
    """,
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_cik_person ON candidate_identity_key (person_id)",
    "CREATE INDEX IF NOT EXISTS idx_co_person_sent ON candidate_outreach (person_id, sent_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_co_job_person ON candidate_outreach (jobdiva_id, person_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_co_interview "
    "ON candidate_outreach (interview_id) WHERE interview_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_co_open_attempts "
    "ON candidate_outreach (sent_at) WHERE completed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_csf_person_expiry "
    "ON candidate_screen_fact (person_id, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_cps_bucket_recency "
    "ON candidate_pool_state (bucket, sort_at DESC, person_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cps_next_eligible "
    "ON candidate_pool_state (next_eligible_at) WHERE next_eligible_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_cps_completed "
    "ON candidate_pool_state (last_completed_at DESC) WHERE last_completed_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_cps_name_lower "
    "ON candidate_pool_state (LOWER(display_name))",
)


def _ensure_candidate_pool_schema() -> None:
    url = DATABASE_URL or SUPABASE_DB_URL
    if not url:
        logger.warning("candidate_pool_schema_init_skipped: no DATABASE_URL")
        return
    try:
        engine = _get_engine()
        # AUTOCOMMIT so one failing statement doesn't abort the transaction and
        # skip every statement after it (the same reason
        # _ensure_sourced_candidates_schema uses it).
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            # New tables use TIMESTAMPTZ while legacy tables use naive TIMESTAMP
            # written via CURRENT_TIMESTAMP. Those legacy values are read as UTC
            # (`col AT TIME ZONE 'UTC'`) wherever the two are compared, which is
            # only correct if the server actually runs UTC. Log it so a non-UTC
            # server is visible instead of silently shifting every comparison.
            try:
                server_tz = conn.execute(text("SHOW timezone")).scalar()
                if str(server_tz or "").upper() not in ("UTC", "ETC/UTC", "GMT"):
                    logger.warning(
                        "candidate_pool: DB server TimeZone is %r, not UTC. Legacy "
                        "naive TIMESTAMP columns are interpreted as UTC when compared "
                        "against the pool's TIMESTAMPTZ columns, so recency windows "
                        "will be offset by the server's UTC offset.",
                        server_tz,
                    )
            except Exception:  # noqa: BLE001
                pass

            for stmt in _TABLES:
                try:
                    conn.execute(text(stmt))
                except Exception as e:  # noqa: BLE001
                    logger.error(f"candidate_pool table create failed: {e}")

            for stmt in _INDEXES:
                try:
                    conn.execute(text(stmt))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"candidate_pool index create skipped: {stmt!r}: {e}")

            try:
                conn.execute(text(
                    "INSERT INTO candidate_pool_reconcile_lock (lock_name) "
                    "VALUES ('pool_state_reconcile') ON CONFLICT (lock_name) DO NOTHING"
                ))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"candidate_pool reconcile lock seed skipped: {e}")

        logger.info("candidate_pool schema ready")
    except Exception as e:  # noqa: BLE001
        logger.error(f"candidate_pool schema init failed: {e}")


async def init_candidate_pool_schema() -> None:
    """Async wrapper called from main.py lifespan."""
    await asyncio.to_thread(_ensure_candidate_pool_schema)
