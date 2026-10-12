"""Launch-session tables and the vocabulary of statuses, stages and row states.

Four tables (startup DDL via main._init_schemas → init_launch_sessions_schema,
run once per boot under core.startup_once):

  launch_sessions             one row per recruiter click (or child pass): the
                              full input snapshot, the stage machine's position
                              and checkpoint, the lease, the timings.
  launch_session_candidates   the session's candidate rows (pool → selected →
                              … → launched/failed), keyed (session, candidate).
  launch_session_events       an append-only progress log for the browser
                              (seq from launch_sessions.event_seq).
  launch_session_stream       S1's raw search events per attempt, so a resumed
                              session rebuilds the exact sourcing state by
                              replaying them; deleted once selection is done.

Every statement is idempotent (IF NOT EXISTS) and TIMESTAMPTZ only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)

# ── Session status ──────────────────────────────────────────────────────────
STATUS_COLLECTING = "collecting"  # provided mode: the browser is uploading rows
STATUS_QUEUED = "queued"  # waiting for a worker (or for not_before)
STATUS_RUNNING = "running"  # leased by a worker (or lease expired → reclaimable)
STATUS_AWAITING_INPUT = "awaiting_input"  # QA review: waiting for POST /confirm
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES: Tuple[str, ...] = (
    STATUS_COLLECTING, STATUS_QUEUED, STATUS_RUNNING, STATUS_AWAITING_INPUT,
)
TERMINAL_STATUSES: Tuple[str, ...] = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

# ── Stages (S1 … S8) ───────────────────────────────────────────────────────
STAGE_CREATED = "created"
STAGE_SOURCING = "sourcing"  # S1 (full_search only)
STAGE_SELECTING = "selecting"  # S2
STAGE_ENRICHING = "enriching"  # S3
STAGE_REVIEW = "review"  # S4 (require_review only)
STAGE_SPLITTING = "splitting"  # S5
STAGE_SAVING = "saving"  # S6
STAGE_LAUNCHING = "launching"  # S7
STAGE_PROVISIONING = "provisioning"  # S7b
STAGE_DONE = "done"  # S8

STAGES: Tuple[str, ...] = (
    STAGE_CREATED, STAGE_SOURCING, STAGE_SELECTING, STAGE_ENRICHING, STAGE_REVIEW,
    STAGE_SPLITTING, STAGE_SAVING, STAGE_LAUNCHING, STAGE_PROVISIONING, STAGE_DONE,
)

# After S7 the launch itself is over (side effects fired, every row in its
# final state): only JobDiva provisioning (S7b) and the wrap-up (S8) remain.
# Such a session no longer holds its job — a new Source & Launch, a
# needs-contact or a retry child may start while it provisions.
LAUNCH_COMPLETE_STAGES: Tuple[str, ...] = (STAGE_PROVISIONING, STAGE_DONE)

# ── Modes / triggers ───────────────────────────────────────────────────────
MODE_FULL_SEARCH = "full_search"
MODE_PROVIDED = "provided"
MODES: Tuple[str, ...] = (MODE_FULL_SEARCH, MODE_PROVIDED)

TRIGGERS: Tuple[str, ...] = ("button", "qa_quick", "retry", "needs_contact", "launch_remaining")
CHILD_TRIGGERS: Tuple[str, ...] = ("retry", "needs_contact")

# Claim phases, each with its own global cap (store.claim):
#   search  a full search's search + selection — PAIR_LAUNCH_SEARCH_CONCURRENCY
#   launch  lookups, saves, Pairbot posts — PAIR_LAUNCH_GLOBAL_CONCURRENCY
#   wrapup  JobDiva provisioning + the wrap-up (S7b / S8) — no global cap (as
#           legacy's fire-and-forget provisioning; the per-worker cap bounds it)
PHASE_SEARCH = "search"
PHASE_LAUNCH = "launch"
PHASE_WRAPUP = "wrapup"
SEARCH_PHASE_STAGES: Tuple[str, ...] = (STAGE_CREATED, STAGE_SOURCING, STAGE_SELECTING)


def search_phase_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return f"({p}mode = 'full_search' AND {p}stage IN ('created', 'sourcing', 'selecting'))"


def phase_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (
        f"(CASE WHEN {search_phase_sql(alias)} THEN 'search' "
        f"WHEN {p}stage IN ('provisioning', 'done') THEN 'wrapup' ELSE 'launch' END)"
    )


SEARCH_PHASE_SQL = search_phase_sql()
PHASE_SQL = phase_sql()
# The rows the one-active-session-per-job index covers (keep in sync with it).
HOLDS_JOB_SQL = (
    "status IN ('collecting', 'queued', 'running', 'awaiting_input') "
    "AND stage NOT IN ('provisioning', 'done')"
)


def in_search_phase(mode: Any, stage: Any) -> bool:
    return mode == MODE_FULL_SEARCH and stage in SEARCH_PHASE_STAGES


def phase(mode: Any, stage: Any) -> str:
    if in_search_phase(mode, stage):
        return PHASE_SEARCH
    return PHASE_WRAPUP if stage in LAUNCH_COMPLETE_STAGES else PHASE_LAUNCH


def holds_job(status: Any, stage: Any) -> bool:
    """Whether a session occupies its job's one active slot."""
    return status in ACTIVE_STATUSES and stage not in LAUNCH_COMPLETE_STAGES


def launch_complete(status: Any, stage: Any) -> bool:
    """Terminal, or past S7 (only provisioning / the wrap-up left)."""
    return status in TERMINAL_STATUSES or stage in LAUNCH_COMPLETE_STAGES

# ── Candidate row states ───────────────────────────────────────────────────
ROW_POOLED = "pooled"  # in the sourcing table / uploaded, not yet selected
ROW_DROPPED = "dropped"  # removed from the table while sourcing (dropped / merged)
ROW_SELECTED = "selected"  # S2 picked it
ROW_SKIPPED = "skipped"  # not launched, for `reason` (never retried by this session)
ROW_NEEDS_CONTACT = "needs_contact"  # S5: no usable unique phone/email — parked
ROW_READY = "ready"  # S5: will be saved
ROW_SAVED = "saved"  # S6: /candidates/save stored it; waiting for S7
ROW_LAUNCHING = "launching"  # S7: claimed for this session, being posted
ROW_LAUNCHED = "launched"  # an interview exists for this job
ROW_FAILED = "failed"  # save or Pairbot failure (retry-failed child can retry)
ROW_UNCONFIRMED = "unconfirmed"  # posted; Pairbot's record could not settle it
ROW_HANDED_OFF = "handed_off"  # moved to a child session (needs-contact / retry)

ROW_STATES: Tuple[str, ...] = (
    ROW_POOLED, ROW_DROPPED, ROW_SELECTED, ROW_SKIPPED, ROW_NEEDS_CONTACT, ROW_READY,
    ROW_SAVED, ROW_LAUNCHING, ROW_LAUNCHED, ROW_FAILED, ROW_UNCONFIRMED, ROW_HANDED_OFF,
)

# Advisory lock keys (728193-728196 are taken: poll, BI sync, AutoSync, startup).
CLAIM_LOCK_KEY = 728197
HOUSEKEEPING_LOCK_KEY = 728198

_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS launch_sessions (
    id uuid PRIMARY KEY,
    job_id text NOT NULL,
    jobdiva_ref text,
    requested_by text,
    parent_session_id uuid,
    trigger text NOT NULL DEFAULT 'button',
    mode text NOT NULL,
    idempotency_key text,
    input_hash text,
    status text NOT NULL DEFAULT 'queued',
    stage text NOT NULL DEFAULT 'created',
    input jsonb NOT NULL,
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
    counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    attempt int NOT NULL DEFAULT 0,
    claims int NOT NULL DEFAULT 0,
    owner text,
    lease_until timestamptz,
    heartbeat_at timestamptz,
    not_before timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    cancel_reason text,
    launch_id uuid,
    side_effects_fired_at timestamptz,
    event_seq int NOT NULL DEFAULT 0,
    cand_rev bigint NOT NULL DEFAULT 0,
    recheck_at timestamptz,
    job_added_at timestamptz,
    clicked_at timestamptz NOT NULL DEFAULT now(),
    stage_started_at timestamptz,
    sourcing_started_at timestamptz,
    sourcing_done_at timestamptz,
    selection_done_at timestamptz,
    enrich_done_at timestamptz,
    review_started_at timestamptz,
    review_done_at timestamptz,
    save_started_at timestamptz,
    save_done_at timestamptz,
    launch_started_at timestamptz,
    first_interview_created_at timestamptz,
    last_batch_done_at timestamptz,
    first_dial_at timestamptz,
    completed_at timestamptz,
    human_wait_ms bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

_CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS launch_session_candidates (
    session_id uuid NOT NULL REFERENCES launch_sessions(id) ON DELETE CASCADE,
    candidate_id text NOT NULL,
    ord int NOT NULL DEFAULT 0,
    source text,
    row jsonb NOT NULL,
    match_score double precision,
    state text NOT NULL DEFAULT 'pooled',
    reason text,
    contact jsonb,
    failure_stage text,
    batch_index int,
    interview_id text,
    error text,
    rev bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, candidate_id)
)
"""

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS launch_session_events (
    session_id uuid NOT NULL REFERENCES launch_sessions(id) ON DELETE CASCADE,
    seq int NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
)
"""

_STREAM_DDL = """
CREATE TABLE IF NOT EXISTS launch_session_stream (
    session_id uuid NOT NULL REFERENCES launch_sessions(id) ON DELETE CASCADE,
    attempt int NOT NULL,
    chunk int NOT NULL,
    events jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, attempt, chunk)
)
"""

SCHEMA_STATEMENTS: Tuple[str, ...] = (
    _SESSIONS_DDL,
    # One ACTIVE session per job: the database, not the endpoint, guarantees
    # it. A session past S7 (provisioning / done) no longer holds the job
    # (HOLDS_JOB_SQL). v2 of the index: the first one covered those stages too.
    "DROP INDEX IF EXISTS ux_launch_sessions_active_job",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_launch_sessions_active_job_v2 ON launch_sessions (job_id) "
    "WHERE " + HOLDS_JOB_SQL,
    # Idempotency key per job (the browser mints one per click).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_launch_sessions_idem ON launch_sessions (job_id, idempotency_key) "
    "WHERE idempotency_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_launch_sessions_claimable ON launch_sessions (created_at) "
    "WHERE status IN ('queued', 'running')",
    "CREATE INDEX IF NOT EXISTS ix_launch_sessions_job ON launch_sessions (job_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_launch_sessions_parent ON launch_sessions (parent_session_id) "
    "WHERE parent_session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_launch_sessions_recheck ON launch_sessions (recheck_at) "
    "WHERE recheck_at IS NOT NULL",
    _CANDIDATES_DDL,
    "CREATE INDEX IF NOT EXISTS ix_launch_session_candidates_state "
    "ON launch_session_candidates (session_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_launch_session_candidates_rev "
    "ON launch_session_candidates (session_id, rev)",
    _EVENTS_DDL,
    _STREAM_DDL,
)


def _connect():
    from core.db import get_db_connection

    return get_db_connection()


def ensure_schema(connect=None) -> None:
    """Run SCHEMA_STATEMENTS in one transaction (sync)."""
    conn = (connect or _connect)()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5000ms'")
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()


async def init_launch_sessions_schema() -> None:
    """Startup hook (main._init_schemas). The caller logs failures."""
    await asyncio.to_thread(ensure_schema)
