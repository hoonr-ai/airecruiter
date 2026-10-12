"""All SQL of the launch sessions (sync psycopg2; async callers use to_thread).

Fencing. A worker's claim returns ``(id, claims)``; ``claims`` is bumped by
every claim, so a session reclaimed by another worker can never be written by
the old one. Every runner write takes a ``Fence(session_id, owner, claims)``
and starts its transaction with

    UPDATE launch_sessions SET … WHERE id = … AND owner = … AND claims = …
                                 AND status = 'running'

— zero rows raises OwnershipLost and nothing else in that transaction is
written. The UPDATE also locks the session row for the transaction, so a
claim (FOR UPDATE SKIP LOCKED) can never interleave with a fenced write, and
writes to launch_session_candidates / _events / _stream in the same
transaction are owner-scoped too. Candidate writes take their ``rev`` from
``launch_sessions.cand_rev`` bumped by that same UPDATE: revs are committed
in order, which is what the browser's incremental read (after_rev) relies on.

Endpoint-side writes (create, upload, start, confirm, cancel, children) are
single conditional statements on the expected status — zero rows means the
session is not in that status (the caller answers 409).

``PgStore`` is the production store; tests run the same contract suite
against it (scratch Postgres) and against an in-memory twin.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from services.launch_sessions import schema as S
from services.launch_sessions.errors import CancelRequested, OwnershipLost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fence:
    session_id: str
    owner: str
    claims: int


@dataclass(frozen=True)
class Claim:
    session_id: str
    claims: int
    stage: str


# ── JSON helpers ────────────────────────────────────────────────────────────

def jsonable(value: Any) -> Any:
    """A deep copy Postgres jsonb accepts: NaN/Infinity → None, NUL chars
    stripped from strings, non-JSON scalars (datetime, Decimal, UUID …) → str."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k).replace("\x00", ""): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    try:
        return jsonable(float(value)) if hasattr(value, "is_finite") else str(value)
    except Exception:  # noqa: BLE001
        return str(value)


def dumps(value: Any) -> str:
    return json.dumps(jsonable(value), separators=(",", ":"))


def input_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(jsonable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def finite_score(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


_HEAVY_ROW_KEYS = ("resume_text", "resumeText", "linkedin_profile")


def light_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """The table projection: the row without résumé text / LinkedIn profile."""
    return {k: v for k, v in (row or {}).items() if k not in _HEAVY_ROW_KEYS}


# Columns a fenced update may set directly.
_SETTABLE = frozenset({
    "stage", "error", "launch_id", "counts", "status", "stage_started_at", "recheck_at",
    "sourcing_started_at", "sourcing_done_at", "selection_done_at", "enrich_done_at",
    "review_started_at", "review_done_at", "save_started_at", "save_done_at",
    "launch_started_at", "first_interview_created_at", "last_batch_done_at",
    "first_dial_at", "completed_at",
})
_TIMESTAMP_COLS = frozenset(c for c in _SETTABLE if c.endswith("_at"))

_FENCE_SQL = (
    "UPDATE launch_sessions SET updated_at = now(){extra} "
    "WHERE id = %(sid)s::uuid AND owner = %(owner)s AND claims = %(claims)s "
    "AND status = 'running' RETURNING cancel_requested, cand_rev"
)

_CREATE_COLS = (
    "id", "job_id", "jobdiva_ref", "requested_by", "parent_session_id", "trigger", "mode",
    "idempotency_key", "input_hash", "status", "stage", "input", "checkpoint", "counts",
    "job_added_at",
)

_RECORDSET_COLS = (
    "candidate_id text, ord int, source text, row jsonb, match_score double precision, "
    "state text, reason text, contact jsonb"
)
_STATE_COLS = (
    "candidate_id text, state text, reason text, contact jsonb, failure_stage text, "
    "batch_index int, interview_id text, error text, row jsonb"
)

_JOB_ROW_SQL = """
    SELECT to_jsonb(mj) AS j FROM monitored_jobs mj
    WHERE mj.job_id::text = %(key)s OR mj.jobdiva_id = %(key)s
    ORDER BY (mj.job_id::text ~ '^[0-9]+$') DESC, mj.created_at DESC
    LIMIT 1
"""


def _default_connect():
    from core.db import get_db_connection

    return get_db_connection()


class PgStore:
    """Launch-session persistence on Postgres. Every method is synchronous and
    opens (and returns) one pooled connection."""

    def __init__(self, connect: Optional[Callable[[], Any]] = None) -> None:
        self._connect_fn = connect

    # ── plumbing ───────────────────────────────────────────────────────────
    def _conn(self):
        return (self._connect_fn or _default_connect)()

    def _tx(self, fn: Callable[[Any], Any]) -> Any:
        import psycopg2.extras

        conn = self._conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                result = fn(cur)
            finally:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            conn.commit()
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _fence(cur, fence: Fence, *, bump_rev: bool = False, extra: str = "") -> Dict[str, Any]:
        extra_sql = extra + (", cand_rev = cand_rev + 1" if bump_rev else "")
        cur.execute(
            _FENCE_SQL.format(extra=extra_sql),
            {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims},
        )
        row = cur.fetchone()
        if row is None:
            raise OwnershipLost(f"session {fence.session_id} is no longer held by this worker")
        return row

    @staticmethod
    def _row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        out = dict(row)
        for key in ("id", "parent_session_id", "launch_id"):
            if out.get(key) is not None:
                out[key] = str(out[key])
        return out

    # ── reads ──────────────────────────────────────────────────────────────
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        def run(cur):
            cur.execute("SELECT * FROM launch_sessions WHERE id = %s::uuid", (session_id,))
            return self._row(cur.fetchone())
        return self._tx(run)

    def find_by_key(self, job_id: str, idempotency_key: str) -> Optional[Dict[str, Any]]:
        def run(cur):
            cur.execute(
                "SELECT * FROM launch_sessions WHERE job_id = %s AND idempotency_key = %s",
                (job_id, idempotency_key),
            )
            return self._row(cur.fetchone())
        return self._tx(run)

    def active_session(self, job_id: str) -> Optional[Dict[str, Any]]:
        """The session holding the job's one active slot (S.HOLDS_JOB_SQL —
        a session past S7 that only provisions does not)."""
        def run(cur):
            cur.execute(
                f"SELECT * FROM launch_sessions WHERE job_id = %s AND {S.HOLDS_JOB_SQL} "
                "ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            )
            return self._row(cur.fetchone())
        return self._tx(run)

    def queue_position(self, session_id: str) -> Optional[int]:
        """For a session waiting for a worker (queued, or running with no live
        lease): how many claimable sessions of the same phase come before it
        in the claim order (0 = next). None when it is not waiting."""
        def run(cur):
            cur.execute(
                f"""
                WITH me AS (
                    SELECT id, created_at, cancel_requested, parent_session_id IS NOT NULL AS child,
                           {S.PHASE_SQL} AS phase
                    FROM launch_sessions
                    WHERE id = %(sid)s::uuid
                      AND (status = 'queued' OR (status = 'running'
                           AND (lease_until IS NULL OR lease_until < now())))
                )
                SELECT (SELECT count(*) FROM launch_sessions s, me
                        WHERE s.id <> me.id
                          AND (s.status = 'queued' OR (s.status = 'running'
                               AND (s.lease_until IS NULL OR s.lease_until < now())))
                          AND (s.not_before IS NULL OR s.not_before <= now() OR s.cancel_requested)
                          AND {S.phase_sql('s')} = me.phase
                          AND (s.cancel_requested, s.parent_session_id IS NOT NULL, me.created_at)
                              > (me.cancel_requested, me.child, s.created_at)
                       ) AS ahead
                FROM me
                """,
                {"sid": session_id},
            )
            row = cur.fetchone()
            return int(row["ahead"]) if row else None
        return self._tx(run)

    def list_sessions(
        self, job_id: str, *, active_only: bool = False, needs_contact_only: bool = False, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        def run(cur):
            where = ["job_id = %(job)s"]
            if active_only:
                where.append("status = ANY(%(active)s)")
            if needs_contact_only:
                where.append(
                    "status = ANY(%(terminal)s) AND EXISTS (SELECT 1 FROM launch_session_candidates c "
                    "WHERE c.session_id = launch_sessions.id AND c.state = 'needs_contact')"
                )
            cur.execute(
                "SELECT * FROM launch_sessions WHERE " + " AND ".join(where)
                + " ORDER BY created_at DESC LIMIT %(limit)s",
                {"job": job_id, "active": list(S.ACTIVE_STATUSES),
                 "terminal": list(S.TERMINAL_STATUSES), "limit": max(1, min(int(limit), 50))},
            )
            return [self._row(r) for r in cur.fetchall()]
        return self._tx(run)

    def children(self, session_id: str) -> List[Dict[str, Any]]:
        def run(cur):
            cur.execute(
                "SELECT id::text AS id, trigger, status, stage, created_at FROM launch_sessions "
                "WHERE parent_session_id = %s::uuid ORDER BY created_at",
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        return self._tx(run)

    def events_after(self, session_id: str, after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        def run(cur):
            cur.execute(
                "SELECT seq, payload, created_at FROM launch_session_events "
                "WHERE session_id = %s::uuid AND seq > %s ORDER BY seq LIMIT %s",
                (session_id, int(after_seq), max(1, min(int(limit), 500))),
            )
            return [dict(r) for r in cur.fetchall()]
        return self._tx(run)

    def events_tail(self, session_id: str, n: int = 20) -> List[Dict[str, Any]]:
        def run(cur):
            cur.execute(
                "SELECT seq, payload, created_at FROM launch_session_events "
                "WHERE session_id = %s::uuid ORDER BY seq DESC LIMIT %s",
                (session_id, max(1, min(int(n), 100))),
            )
            return list(reversed([dict(r) for r in cur.fetchall()]))
        return self._tx(run)

    def candidate_rows(
        self, session_id: str, *, states: Optional[Sequence[str]] = None, ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Full rows (``row`` included), pool order."""
        def run(cur):
            where = ["session_id = %(sid)s::uuid"]
            params: Dict[str, Any] = {"sid": session_id}
            if states is not None:
                where.append("state = ANY(%(states)s)")
                params["states"] = list(states)
            if ids is not None:
                where.append("candidate_id = ANY(%(ids)s)")
                params["ids"] = [str(i) for i in ids]
            cur.execute(
                "SELECT candidate_id, ord, source, row, match_score, state, reason, contact, "
                "failure_stage, batch_index, interview_id, error, rev, updated_at "
                "FROM launch_session_candidates WHERE " + " AND ".join(where)
                + " ORDER BY ord, candidate_id",
                params,
            )
            return [dict(r) for r in cur.fetchall()]
        return self._tx(run)

    def candidate_states(self, session_id: str) -> List[Dict[str, Any]]:
        """[{candidate_id, state, reason}] of every row (no row payload)."""
        def run(cur):
            cur.execute(
                "SELECT candidate_id, state, reason FROM launch_session_candidates "
                "WHERE session_id = %s::uuid ORDER BY ord, candidate_id",
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        return self._tx(run)

    def candidate_ids(self, session_id: str, states: Optional[Sequence[str]] = None) -> List[str]:
        def run(cur):
            if states is None:
                cur.execute(
                    "SELECT candidate_id FROM launch_session_candidates WHERE session_id = %s::uuid "
                    "ORDER BY ord, candidate_id",
                    (session_id,),
                )
            else:
                cur.execute(
                    "SELECT candidate_id FROM launch_session_candidates WHERE session_id = %s::uuid "
                    "AND state = ANY(%s) ORDER BY ord, candidate_id",
                    (session_id, list(states)),
                )
            return [str(r["candidate_id"]) for r in cur.fetchall()]
        return self._tx(run)

    def candidates_page(
        self,
        session_id: str,
        *,
        states: Optional[Sequence[str]] = None,
        limit: int = 200,
        offset: int = 0,
        after_rev: Optional[int] = None,
        after_id: Optional[str] = None,
        updated_after: Optional[_dt.datetime] = None,
        light: bool = True,
        ids: Optional[Sequence[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """(rows, total matching). ``light`` drops résumé text / LinkedIn profile;
        ``ids`` keeps only those candidate ids (the primary key's second column).

        Incremental reads (``after_rev`` given) page by the keyset
        (rev, candidate_id) — revs are committed in order, so a cursor never
        skips a later write; otherwise rows come in pool order with offset."""
        def run(cur):
            where = ["session_id = %(sid)s::uuid"]
            params: Dict[str, Any] = {"sid": session_id, "limit": max(1, min(int(limit), 1000)),
                                      "offset": max(0, int(offset))}
            if states:
                where.append("state = ANY(%(states)s)")
                params["states"] = list(states)
            if ids is not None:
                where.append("candidate_id = ANY(%(ids)s)")
                params["ids"] = [str(i) for i in ids]
            keyset = after_rev is not None
            if keyset:
                where.append("(rev > %(after_rev)s OR (rev = %(after_rev)s AND candidate_id > %(after_id)s))")
                params["after_rev"] = int(after_rev)
                params["after_id"] = str(after_id or "")
            if updated_after is not None:
                where.append("updated_at > %(updated_after)s")
                params["updated_after"] = updated_after
            row_expr = "row - 'resume_text' - 'resumeText' - 'linkedin_profile'" if light else "row"
            clause = " AND ".join(where)
            cur.execute(f"SELECT count(*) AS n FROM launch_session_candidates WHERE {clause}", params)
            total = int(cur.fetchone()["n"])
            order = "rev, candidate_id LIMIT %(limit)s" if keyset else "ord, candidate_id LIMIT %(limit)s OFFSET %(offset)s"
            cur.execute(
                f"SELECT candidate_id, ord, source, {row_expr} AS row, match_score, state, reason, contact, "
                "failure_stage, batch_index, interview_id, error, rev, updated_at "
                f"FROM launch_session_candidates WHERE {clause} ORDER BY {order}",
                params,
            )
            return [dict(r) for r in cur.fetchall()], total
        return self._tx(run)

    def state_counts(self, session_id: str) -> Dict[str, Any]:
        """{"states": {state: n}, "reasons": {state: {reason kind: n}}}; a
        reason kind is the reason up to its first ':'."""
        def run(cur):
            cur.execute(
                "SELECT state, COALESCE(split_part(reason, ':', 1), '') AS kind, count(*) AS n "
                "FROM launch_session_candidates WHERE session_id = %s::uuid GROUP BY 1, 2",
                (session_id,),
            )
            return cur.fetchall()
        return _fold_state_counts(self._tx(run))

    def stream_load(self, session_id: str) -> Dict[int, List[Dict[str, Any]]]:
        def run(cur):
            cur.execute(
                "SELECT attempt, chunk, events FROM launch_session_stream "
                "WHERE session_id = %s::uuid ORDER BY attempt, chunk",
                (session_id,),
            )
            out: Dict[int, List[Dict[str, Any]]] = {}
            for r in cur.fetchall():
                out.setdefault(int(r["attempt"]), []).extend(r["events"] or [])
            return out
        return self._tx(run)

    # ── job data (sourced_candidates / monitored_jobs) ────────────────────
    def resolve_job(self, job_key: str) -> Optional[Dict[str, Any]]:
        """The canonical monitored_jobs row for a job id or JobDiva ref (same
        tie-break as the launch code: numeric job_id first, newest created_at)."""
        key = str(job_key or "").strip()
        if not key:
            return None

        def run(cur):
            cur.execute(_JOB_ROW_SQL, {"key": key})
            row = cur.fetchone()
            return dict(row["j"]) if row and row.get("j") else None
        job = self._tx(run)
        if not job:
            return None
        return {
            "job_id": str(job.get("job_id")),
            "jobdiva_id": (str(job.get("jobdiva_id")).strip() or None) if job.get("jobdiva_id") else None,
            "recruiter_emails": job.get("recruiter_emails"),
            "outreach_stopped_at": job.get("outreach_stopped_at"),
            "pair_added_at": job.get("pair_added_at"),
            "first_launch_at": job.get("first_launch_at"),
            "customer_name": job.get("customer_name"),
            "customer": job.get("customer"),
        }

    def job_first_launch_at(self, job_key: str) -> Any:
        job = self.resolve_job(job_key)
        return job.get("first_launch_at") if job else None

    def job_launch_state(self, job_keys: Sequence[str]) -> List[Dict[str, Any]]:
        """Every sourced row of the job (both keys) with its launch facts, plus
        audit-only interviews: the input of the runner's launched predicate."""
        keys = _keys(job_keys)
        if not keys:
            return []

        def run(cur):
            cur.execute(
                """
                SELECT sc.candidate_id, sc.source,
                       COALESCE(sc.data->>'engage_status', '') AS engage_status,
                       COALESCE(sc.data->>'engage_interview_id', '') AS interview_id,
                       COALESCE(sc.data->>'jobdiva_candidate_id', '') AS jobdiva_candidate_id,
                       COALESCE(sc.data->>'engage_session_id', '') AS engage_session_id,
                       COALESCE(sc.data->>'engage_block_reason', '') AS block_reason,
                       EXISTS (
                           SELECT 1 FROM engage_interview_audit a
                           WHERE a.candidate_id = sc.candidate_id AND a.jobdiva_id = ANY(%(keys)s)
                             AND COALESCE(a.interview_id, '') <> ''
                       ) AS audit_launched,
                       -- Contacts, for the person gate's launched_person set
                       -- (services/launch_person_keys.py).
                       COALESCE(NULLIF(sc.email, ''), sc.data->>'email') AS email,
                       COALESCE(NULLIF(sc.phone, ''), sc.data->>'phone') AS phone,
                       COALESCE(NULLIF(sc.profile_url, ''), sc.data->>'profile_url',
                                sc.data->>'linkedin_url', sc.data->'urls'->>'linkedin') AS profile_url
                FROM sourced_candidates sc
                WHERE sc.jobdiva_id = ANY(%(keys)s)
                UNION ALL
                SELECT DISTINCT ON (a.candidate_id) a.candidate_id, NULL, '', '', '', '', '', TRUE,
                       a.candidate_email, NULL, NULL
                FROM engage_interview_audit a
                WHERE a.jobdiva_id = ANY(%(keys)s) AND COALESCE(a.interview_id, '') <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM sourced_candidates sc
                      WHERE sc.candidate_id = a.candidate_id AND sc.jobdiva_id = ANY(%(keys)s)
                  )
                """,
                {"keys": keys},
            )
            return [dict(r) for r in cur.fetchall()]
        return self._tx(run)

    def engage_rows(self, job_keys: Sequence[str], candidate_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Per candidate (folded over both job keys): engage_status values,
        interview id (data or audit), engage_session_id, engage_bulk_id and
        block reason — what S7 re-reads after every batch."""
        keys = _keys(job_keys)
        ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
        if not keys or not ids:
            return {}

        def run(cur):
            cur.execute(
                """
                SELECT sc.candidate_id,
                       LOWER(COALESCE(sc.data->>'engage_status', '')) AS engage_status,
                       COALESCE(sc.data->>'engage_interview_id', '') AS interview_id,
                       COALESCE(sc.data->>'engage_session_id', '') AS engage_session_id,
                       COALESCE(sc.data->>'engage_bulk_id', '') AS engage_bulk_id,
                       COALESCE(sc.data->>'engage_block_reason', '') AS block_reason,
                       (SELECT a.interview_id FROM engage_interview_audit a
                         WHERE a.candidate_id = sc.candidate_id AND a.jobdiva_id = ANY(%(keys)s)
                           AND COALESCE(a.interview_id, '') <> '' ORDER BY a.id DESC LIMIT 1) AS audit_interview_id
                FROM sourced_candidates sc
                WHERE sc.jobdiva_id = ANY(%(keys)s) AND sc.candidate_id = ANY(%(ids)s)
                """,
                {"keys": keys, "ids": ids},
            )
            return [dict(r) for r in cur.fetchall()]
        return fold_engage_rows(self._tx(run))

    def requeue_engage_rows(self, job_keys: Sequence[str], candidate_ids: Sequence[str], session_id: str) -> List[str]:
        """Rows this session posted that Pairbot provably never created
        ('missing'): back to 'queued' for this session so the next pass can
        claim and post them again. Never touches a row with an interview id,
        another session's row, or a row another path moved on."""
        keys, ids = _keys(job_keys), [str(c) for c in candidate_ids if str(c).strip()]
        if not keys or not ids:
            return []

        def run(cur):
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object('engage_status', 'queued'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE jobdiva_id = ANY(%(keys)s) AND candidate_id = ANY(%(ids)s)
                  AND COALESCE(data->>'engage_session_id', '') = %(sid)s
                  AND data->>'engage_status' IN ('processing', 'sending', 'queued')
                  AND COALESCE(data->>'engage_interview_id', '') = ''
                RETURNING candidate_id
                """,
                {"keys": keys, "ids": ids, "sid": str(session_id)},
            )
            return sorted({str(r["candidate_id"]) for r in cur.fetchall()})
        return self._tx(run)

    def fail_engage_rows(self, job_keys: Sequence[str], candidate_ids: Sequence[str], session_id: str) -> List[str]:
        """Rows this session posted that Pairbot provably never created and
        that will not be re-posted (the session is finished): 'failed', the
        session's claim dropped — retryable by a retry-failed child."""
        keys, ids = _keys(job_keys), [str(c) for c in candidate_ids if str(c).strip()]
        if not keys or not ids:
            return []

        def run(cur):
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = (COALESCE(data, '{}'::jsonb) - 'engage_session_id' - 'engage_status_before_claim')
                           || jsonb_build_object('engage_status', 'failed'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE jobdiva_id = ANY(%(keys)s) AND candidate_id = ANY(%(ids)s)
                  AND COALESCE(data->>'engage_session_id', '') = %(sid)s
                  AND data->>'engage_status' IN ('processing', 'sending', 'queued')
                  AND COALESCE(data->>'engage_interview_id', '') = ''
                RETURNING candidate_id
                """,
                {"keys": keys, "ids": ids, "sid": str(session_id)},
            )
            return sorted({str(r["candidate_id"]) for r in cur.fetchall()})
        return self._tx(run)

    def park_posted_rows(
        self, job_keys: Sequence[str], bulk_by_id: Dict[str, str], session_id: str,
    ) -> List[str]:
        """A finishing session's rows that MAY be at Pairbot with no recorded
        outcome (posted but unconfirmed / proven but unwritten) and that D1's
        no-answer path left 'queued': moved to 'processing' with their bulk id
        (data.engage_bulk_id), keeping engage_session_id. Every launch path
        then reads them as in flight (never re-claimable, never re-posted) and
        the housekeeping re-check settles them by-job ('sent' / 'failed').
        Rows with an interview id, another session's rows or rows another path
        moved on are untouched."""
        keys = _keys(job_keys)
        ids = {str(c): str(b or "") for c, b in (bulk_by_id or {}).items() if str(c).strip()}
        if not keys or not ids:
            return []

        def run(cur):
            cur.execute(
                """
                UPDATE sourced_candidates
                SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object('engage_status', 'processing')
                           || CASE WHEN COALESCE(%(bulks)s::jsonb ->> candidate_id, '') = '' THEN '{}'::jsonb
                                   ELSE jsonb_build_object('engage_bulk_id', %(bulks)s::jsonb ->> candidate_id) END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE jobdiva_id = ANY(%(keys)s) AND candidate_id = ANY(%(ids)s)
                  AND COALESCE(data->>'engage_session_id', '') = %(sid)s
                  AND data->>'engage_status' = 'queued'
                  AND COALESCE(data->>'engage_interview_id', '') = ''
                RETURNING candidate_id
                """,
                {"keys": keys, "ids": list(ids), "bulks": dumps(ids), "sid": str(session_id)},
            )
            return sorted({str(r["candidate_id"]) for r in cur.fetchall()})
        return self._tx(run)

    # ── creation and browser-side transitions ─────────────────────────────
    def create_session(
        self, fields: Dict[str, Any], candidates: Sequence[Dict[str, Any]] = (),
        event: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """INSERT … ON CONFLICT DO NOTHING (both unique indexes: the
        idempotency key and the one-active-per-job index). None = conflict;
        else the new row, with its first candidates and 'created' event
        written in the same transaction."""
        return self._tx(lambda cur: self.create_session_in(cur, fields, candidates, event))

    def add_candidates(self, session_id: str, rows: Sequence[Dict[str, Any]], *, max_total: int) -> Dict[str, Any]:
        """Append uploaded rows to a COLLECTING session. Returns {"ok": bool,
        "status": current status, "total": rows now held, "too_many": bool}."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET cand_rev = cand_rev + 1, updated_at = now() "
                "WHERE id = %s::uuid AND status = 'collecting' RETURNING cand_rev",
                (session_id,),
            )
            hit = cur.fetchone()
            if hit is None:
                cur.execute("SELECT status FROM launch_sessions WHERE id = %s::uuid", (session_id,))
                cur_row = cur.fetchone()
                return {"ok": False, "status": cur_row["status"] if cur_row else None, "total": 0, "too_many": False}
            cur.execute(
                "SELECT count(*) AS n, COALESCE(max(ord), -1) AS max_ord FROM launch_session_candidates "
                "WHERE session_id = %s::uuid",
                (session_id,),
            )
            have = cur.fetchone()
            new_ids = {str(r["candidate_id"]) for r in rows}
            cur.execute(
                "SELECT count(*) AS n FROM launch_session_candidates WHERE session_id = %s::uuid "
                "AND candidate_id = ANY(%s)",
                (session_id, list(new_ids)),
            )
            existing = int(cur.fetchone()["n"])
            total = int(have["n"]) + len(new_ids) - existing
            if total > max_total:
                raise _TooMany(total)
            start = int(have["max_ord"]) + 1
            _insert_candidates(
                cur, session_id, [dict(r, ord=start + i) for i, r in enumerate(rows)], rev=int(hit["cand_rev"]),
            )
            return {"ok": True, "status": S.STATUS_COLLECTING, "total": total, "too_many": False}
        try:
            return self._tx(run)
        except _TooMany as exc:
            return {"ok": False, "status": S.STATUS_COLLECTING, "total": exc.total, "too_many": True}

    def start_session(self, session_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """collecting → queued (needs ≥1 row). (row, None) or (None, reason)."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET status = 'queued', updated_at = now() "
                "WHERE id = %s::uuid AND status = 'collecting' "
                "AND EXISTS (SELECT 1 FROM launch_session_candidates c WHERE c.session_id = launch_sessions.id) "
                "RETURNING *",
                (session_id,),
            )
            row = self._row(cur.fetchone())
            if row is not None:
                return row, None
            cur.execute("SELECT status FROM launch_sessions WHERE id = %s::uuid", (session_id,))
            cur_row = cur.fetchone()
            if cur_row is None:
                return None, "not_found"
            return None, "empty" if cur_row["status"] == S.STATUS_COLLECTING else str(cur_row["status"])
        return self._tx(run)

    def cancel_session(self, session_id: str, reason: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """'cancelled' (was collecting/queued/awaiting_input: done now),
        'requested' (running: the runner finalizes), or 'not_active'."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET status = 'cancelled', completed_at = now(), updated_at = now(), "
                "error = %(reason)s, cancel_reason = %(reason)s, owner = NULL, lease_until = NULL "
                "WHERE id = %(sid)s::uuid AND status IN ('collecting', 'queued', 'awaiting_input') RETURNING *",
                {"sid": session_id, "reason": reason},
            )
            row = self._row(cur.fetchone())
            if row is not None:
                return "cancelled", row
            # A released session waiting out a back-off (not_before) is
            # claimable at once: the cancel is finalized now, not after it.
            cur.execute(
                "UPDATE launch_sessions SET cancel_requested = true, updated_at = now(), not_before = NULL, "
                "cancel_reason = COALESCE(cancel_reason, %(reason)s) "
                "WHERE id = %(sid)s::uuid AND status = 'running' RETURNING *",
                {"sid": session_id, "reason": reason},
            )
            row = self._row(cur.fetchone())
            if row is not None:
                return "requested", row
            cur.execute("SELECT * FROM launch_sessions WHERE id = %s::uuid", (session_id,))
            return "not_active", self._row(cur.fetchone())
        return self._tx(run)

    def confirm_review(
        self, session_id: str, final_contacts: Dict[str, Dict[str, Any]], review: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """awaiting_input → queued with the confirmed contacts (FINAL: they
        replace both channels); selected rows not confirmed are skipped."""
        def run(cur):
            cur.execute(
                """
                UPDATE launch_sessions
                SET status = 'queued', review_done_at = now(), not_before = NULL, updated_at = now(),
                    human_wait_ms = human_wait_ms + GREATEST(0, (EXTRACT(EPOCH FROM
                        (now() - COALESCE(review_started_at, now()))) * 1000)::bigint),
                    checkpoint = checkpoint || jsonb_build_object('review', %(review)s::jsonb),
                    cand_rev = cand_rev + 1
                WHERE id = %(sid)s::uuid AND status = 'awaiting_input'
                RETURNING *
                """,
                {"sid": session_id, "review": dumps(review)},
            )
            row = self._row(cur.fetchone())
            if row is None:
                return None
            rev = int(row["cand_rev"])
            items = [
                {"candidate_id": cid, "contact": {"final": vals, "confirmed": True}}
                for cid, vals in final_contacts.items()
            ]
            if items:
                _apply_states(cur, session_id, items, rev, from_states=[S.ROW_SELECTED])
            cur.execute(
                "UPDATE launch_session_candidates SET state = 'skipped', reason = 'review_removed', "
                "rev = %s, updated_at = now() WHERE session_id = %s::uuid AND state = 'selected' "
                "AND NOT (candidate_id = ANY(%s))",
                (rev, session_id, list(final_contacts.keys())),
            )
            return row
        return self._tx(run)

    def create_child(
        self,
        fields: Dict[str, Any],
        rows: Sequence[Dict[str, Any]],
        *,
        parent_id: str,
        handed_off: Sequence[str],
        skipped: Sequence[str],
        event: Optional[Dict[str, Any]] = None,
        parent_event: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """A child session (needs-contact / retry-failed) with its rows, and
        the parent's rows moved to handed_off / skipped — one transaction.
        None = conflict (another active session for the job, or the key)."""
        def run(cur):
            child = self.create_session_in(cur, fields, rows, event)
            if child is None:
                return None
            cur.execute(
                "UPDATE launch_sessions SET cand_rev = cand_rev + 1 WHERE id = %s::uuid RETURNING cand_rev",
                (parent_id,),
            )
            prow = cur.fetchone()
            rev = int(prow["cand_rev"]) if prow else 0
            items = [{"candidate_id": c, "state": S.ROW_HANDED_OFF, "reason": f"child:{child['id']}"} for c in handed_off]
            items += [{"candidate_id": c, "state": S.ROW_SKIPPED, "reason": "skipped_by_recruiter"} for c in skipped]
            if items:
                _apply_states(cur, parent_id, items, rev)
            if parent_event:
                _append_event_cur(cur, parent_id, parent_event)
            return child
        return self._tx(run)

    def create_session_in(self, cur, fields, rows, event) -> Optional[Dict[str, Any]]:
        """create_session's statements on an open cursor (create_child reuses
        them). Optional ``clicked_at`` / ``human_wait_ms`` (a child carrying
        its parent's click) default to now() / 0."""
        cols = list(_CREATE_COLS)
        values = {c: fields.get(c) for c in cols}
        for c in ("input", "checkpoint", "counts"):
            values[c] = dumps(values[c] if values[c] is not None else {})
        placeholders = ", ".join(
            f"%({c})s::jsonb" if c in ("input", "checkpoint", "counts")
            else f"%({c})s::uuid" if c in ("id", "parent_session_id")
            else f"%({c})s" for c in cols
        )
        cur.execute(
            f"INSERT INTO launch_sessions ({', '.join(cols)}, cand_rev, event_seq, clicked_at, human_wait_ms) "
            f"VALUES ({placeholders}, %(rev)s, %(seq)s, COALESCE(%(clicked_at)s::timestamptz, now()), "
            f"COALESCE(%(human_wait_ms)s::bigint, 0)) ON CONFLICT DO NOTHING RETURNING *",
            {**values, "rev": 1 if rows else 0, "seq": 1 if event else 0,
             "clicked_at": fields.get("clicked_at"), "human_wait_ms": fields.get("human_wait_ms")},
        )
        row = self._row(cur.fetchone())
        if row is None:
            return None
        if rows:
            _insert_candidates(cur, row["id"], rows, rev=1)
        if event:
            cur.execute(
                "INSERT INTO launch_session_events (session_id, seq, payload) VALUES (%s::uuid, 1, %s::jsonb)",
                (row["id"], dumps(event)),
            )
        return row

    def append_event(self, session_id: str, payload: Dict[str, Any], fence: Optional[Fence] = None) -> Optional[int]:
        """Best-effort: the seq comes from launch_sessions.event_seq in the
        same transaction (commit order == seq order). A fenced append whose
        fence fails writes nothing. Never raises."""
        def run(cur):
            if fence is not None:
                cur.execute(
                    "UPDATE launch_sessions SET event_seq = event_seq + 1 WHERE id = %(sid)s::uuid "
                    "AND owner = %(owner)s AND claims = %(claims)s AND status = 'running' RETURNING event_seq",
                    {"sid": session_id, "owner": fence.owner, "claims": fence.claims},
                )
                hit = cur.fetchone()
                if hit is None:
                    return None
                seq = int(hit["event_seq"])
                cur.execute(
                    "INSERT INTO launch_session_events (session_id, seq, payload) VALUES (%s::uuid, %s, %s::jsonb)",
                    (session_id, seq, dumps(payload)),
                )
                return seq
            return _append_event_cur(cur, session_id, payload)
        try:
            return self._tx(run)
        except Exception as exc:  # noqa: BLE001 — progress events never break a launch
            logger.warning("launch_session_event_append_failed session=%s err=%s", session_id, exc)
            return None

    # ── runner: claim / lease ─────────────────────────────────────────────
    def claim(
        self, owner: str, limit: int, *, global_cap: int, lease_s: int, session_id: Optional[str] = None,
        search_cap: Optional[int] = None,
    ) -> List[Claim]:
        """Claim up to ``limit`` claimable sessions (queued, or running with
        an expired lease; not_before passed), within the global caps of live
        leases — counted and claimed under one transaction-scoped advisory
        lock, so concurrent ticks on 8 workers can never exceed them.

        Caps (S.PHASE_SQL): with ``search_cap``, full searches in their search
        phase count against ``search_cap``, sessions in the launch phase
        against ``global_cap``, and sessions past S7 (provisioning / done)
        against neither — the runner hands a session over when it crosses from
        search to launch, so a live lease's phase is never more constrained
        than the one it was claimed under. Without ``search_cap``, every lease
        counts against ``global_cap``. Phases are taken launch, search, then
        wrap-up; within one: cancel-requested first, then child sessions (a
        recruiter is waiting on a modal), then the oldest. A cancel-requested
        session is claimed regardless of the caps (the runner only finalizes
        it); a cancel clears not_before, and a release of a cancel-requested
        session sets none (``release``), so it is finalized at once."""
        if limit <= 0:
            return []

        claimable = (
            "(status = 'queued' OR (status = 'running' AND (lease_until IS NULL OR lease_until < now()))) "
            "AND (%(only)s::uuid IS NULL OR id = %(only)s::uuid)"
        )

        def take_some(cur, where: str, n: int) -> List[Claim]:
            if n <= 0:
                return []
            cur.execute(
                f"""
                WITH picked AS (
                    SELECT id FROM launch_sessions
                    WHERE {claimable} AND {where}
                    ORDER BY cancel_requested DESC, (parent_session_id IS NOT NULL) DESC, created_at
                    LIMIT %(take)s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE launch_sessions s
                SET status = 'running', owner = %(owner)s, claims = s.claims + 1,
                    lease_until = now() + make_interval(secs => %(lease)s),
                    heartbeat_at = now(), not_before = NULL, updated_at = now()
                FROM picked WHERE s.id = picked.id
                RETURNING s.id::text AS id, s.claims, s.stage
                """,
                {"only": session_id, "take": int(n), "owner": owner, "lease": int(lease_s)},
            )
            return [Claim(str(r["id"]), int(r["claims"]), str(r["stage"])) for r in cur.fetchall()]

        def run(cur):
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (S.CLAIM_LOCK_KEY,))
            out = take_some(cur, "cancel_requested AND (not_before IS NULL OR not_before <= now())", int(limit))
            left = int(limit) - len(out)
            if left <= 0:
                return out
            cur.execute(
                f"SELECT count(*) FILTER (WHERE {S.PHASE_SQL} = 'search') AS search, "
                f"count(*) FILTER (WHERE {S.PHASE_SQL} = 'launch') AS launch, count(*) AS total "
                "FROM launch_sessions WHERE status = 'running' AND lease_until > now()"
            )
            live = cur.fetchone()
            waited = "NOT cancel_requested AND (not_before IS NULL OR not_before <= now())"
            if search_cap is None:
                return out + take_some(cur, waited, min(left, int(global_cap) - int(live["total"])))
            for name, free in (
                (S.PHASE_LAUNCH, int(global_cap) - int(live["launch"])),
                (S.PHASE_SEARCH, int(search_cap) - int(live["search"])),
                (S.PHASE_WRAPUP, left),
            ):
                got = take_some(cur, f"{waited} AND {S.PHASE_SQL} = '{name}'", min(left, free))
                out += got
                left -= len(got)
                if left <= 0:
                    break
            return out
        return self._tx(run)

    def heartbeat(self, fence: Fence, *, lease_s: int) -> bool:
        """Renew the lease. Returns cancel_requested; raises OwnershipLost."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET lease_until = now() + make_interval(secs => %(lease)s), "
                "heartbeat_at = now() WHERE id = %(sid)s::uuid AND owner = %(owner)s "
                "AND claims = %(claims)s AND status = 'running' RETURNING cancel_requested",
                {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims, "lease": int(lease_s)},
            )
            row = cur.fetchone()
            if row is None:
                raise OwnershipLost(f"heartbeat: session {fence.session_id} is no longer ours")
            return bool(row["cancel_requested"])
        return self._tx(run)

    def assert_owner(self, fence: Fence, *, lease_s: int, margin_s: int) -> bool:
        """Before an externally visible unit: still ours AND more than
        ``margin_s`` left on the lease (then renewed). Returns cancel_requested;
        raises OwnershipLost otherwise — the caller must not send."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET lease_until = now() + make_interval(secs => %(lease)s), "
                "heartbeat_at = now() WHERE id = %(sid)s::uuid AND owner = %(owner)s "
                "AND claims = %(claims)s AND status = 'running' "
                "AND lease_until > now() + make_interval(secs => %(margin)s) RETURNING cancel_requested",
                {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims,
                 "lease": int(lease_s), "margin": int(margin_s)},
            )
            row = cur.fetchone()
            if row is None:
                raise OwnershipLost(f"assert_owner: session {fence.session_id} is not safely ours")
            return bool(row["cancel_requested"])
        return self._tx(run)

    def release(
        self, fence: Fence, *, not_before_s: Optional[float] = None, error: Optional[str] = None,
        count_failure: bool = False, cancel_skips_wait: bool = True,
    ) -> Optional[int]:
        """Owner-scoped: lease_until = now() and owner = NULL (status stays
        running) so the next tick anywhere adopts it — after ``not_before_s``
        when given. Clearing the owner fences this claim out for good: a
        heartbeat still in flight can no longer re-lease it. A session with a
        pending cancel gets no not_before (finalized at once) unless
        ``cancel_skips_wait`` is False (its finalize itself failed: back off).
        ``count_failure`` bumps ``attempt``. Returns the new attempt, or None
        when the session is no longer ours (nothing written)."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET owner = NULL, lease_until = now(), heartbeat_at = now(), "
                "updated_at = now(), "
                "not_before = CASE WHEN %(nb)s::float8 IS NULL OR (%(skip)s AND cancel_requested) THEN NULL "
                "ELSE now() + make_interval(secs => %(nb)s::float8) END, "
                "attempt = attempt + %(inc)s, error = COALESCE(%(error)s, error) "
                "WHERE id = %(sid)s::uuid AND owner = %(owner)s AND claims = %(claims)s "
                "AND status = 'running' RETURNING attempt",
                {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims,
                 "nb": not_before_s, "inc": 1 if count_failure else 0, "error": error,
                 "skip": bool(cancel_skips_wait)},
            )
            row = cur.fetchone()
            return int(row["attempt"]) if row else None
        return self._tx(run)

    # ── runner: fenced writes ─────────────────────────────────────────────
    def update_session(
        self,
        fence: Fence,
        *,
        values: Optional[Dict[str, Any]] = None,
        coalesce_now: Iterable[str] = (),
        set_now: Iterable[str] = (),
        checkpoint: Optional[Dict[str, Any]] = None,
        counts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fenced UPDATE of allowlisted columns; ``checkpoint`` / ``counts``
        merge top-level keys (``||``). Returns the updated row."""
        sets: List[str] = ["updated_at = now()"]
        params: Dict[str, Any] = {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims}
        for col, val in (values or {}).items():
            if col not in _SETTABLE:
                raise ValueError(f"column {col} is not settable")
            if col == "counts":
                sets.append("counts = %(v_counts)s::jsonb")
                params["v_counts"] = dumps(val or {})
            elif col == "launch_id":
                sets.append("launch_id = %(v_launch_id)s::uuid")
                params["v_launch_id"] = val
            else:
                sets.append(f"{col} = %(v_{col})s")
                params[f"v_{col}"] = val
        for col in coalesce_now:
            if col not in _TIMESTAMP_COLS:
                raise ValueError(f"column {col} is not a timestamp")
            sets.append(f"{col} = COALESCE({col}, now())")
        for col in set_now:
            if col not in _TIMESTAMP_COLS:
                raise ValueError(f"column {col} is not a timestamp")
            sets.append(f"{col} = now()")
        if checkpoint:
            sets.append("checkpoint = checkpoint || %(ck)s::jsonb")
            params["ck"] = dumps(checkpoint)
        if counts:
            sets.append("counts = counts || %(cn)s::jsonb")
            params["cn"] = dumps(counts)

        def run(cur):
            cur.execute(
                f"UPDATE launch_sessions SET {', '.join(sets)} WHERE id = %(sid)s::uuid "
                "AND owner = %(owner)s AND claims = %(claims)s AND status = 'running' RETURNING *",
                params,
            )
            row = self._row(cur.fetchone())
            if row is None:
                raise OwnershipLost(f"update: session {fence.session_id} is no longer ours")
            return row
        return self._tx(run)

    def set_checkpoint_path(self, fence: Fence, path: Sequence[str], value: Any) -> None:
        """Fenced jsonb_set of one nested checkpoint key (parents must exist):
        lets concurrent writers record different keys without overwriting."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET checkpoint = jsonb_set(checkpoint, %(path)s::text[], %(val)s::jsonb, true), "
                "updated_at = now() WHERE id = %(sid)s::uuid AND owner = %(owner)s AND claims = %(claims)s "
                "AND status = 'running' RETURNING id",
                {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims,
                 "path": [str(p) for p in path], "val": dumps(value)},
            )
            if cur.fetchone() is None:
                raise OwnershipLost(f"checkpoint: session {fence.session_id} is no longer ours")
        self._tx(run)

    def to_awaiting_input(self, fence: Fence, checkpoint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Hand the session to the recruiter (QA review): status
        awaiting_input, no owner, no lease — nothing runs while it waits.
        Never over a pending cancel: raises CancelRequested (the runner
        finalizes the cancel instead)."""
        params = {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims,
                  "ck": dumps(checkpoint or {})}

        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET status = 'awaiting_input', owner = NULL, lease_until = NULL, "
                "review_started_at = COALESCE(review_started_at, now()), updated_at = now(), "
                "checkpoint = checkpoint || %(ck)s::jsonb "
                "WHERE id = %(sid)s::uuid AND owner = %(owner)s AND claims = %(claims)s "
                "AND status = 'running' AND NOT cancel_requested RETURNING *",
                params,
            )
            row = self._row(cur.fetchone())
            if row is not None:
                return row
            cur.execute(
                "SELECT cancel_requested FROM launch_sessions WHERE id = %(sid)s::uuid AND owner = %(owner)s "
                "AND claims = %(claims)s AND status = 'running'",
                params,
            )
            still = cur.fetchone()
            if still is not None and still["cancel_requested"]:
                raise CancelRequested(fence.session_id)
            raise OwnershipLost(f"review: session {fence.session_id} is no longer ours")
        return self._tx(run)

    def finish(
        self, fence: Fence, *, status: str, error: Optional[str], counts: Dict[str, Any],
        recheck_in_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Terminal status (fenced). ``recheck_in_s`` schedules the housekeeping
        re-check of unconfirmed rows."""
        if status not in S.TERMINAL_STATUSES:
            raise ValueError(status)

        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET status = %(status)s, error = %(error)s, "
                "counts = counts || %(counts)s::jsonb, completed_at = now(), lease_until = NULL, "
                "updated_at = now(), stage = CASE WHEN %(status)s = 'completed' THEN 'done' ELSE stage END, "
                "recheck_at = CASE WHEN %(recheck)s::int IS NULL THEN NULL "
                "ELSE now() + make_interval(secs => %(recheck)s::int) END "
                "WHERE id = %(sid)s::uuid AND owner = %(owner)s AND claims = %(claims)s "
                "AND status = 'running' RETURNING *",
                {"sid": fence.session_id, "owner": fence.owner, "claims": fence.claims,
                 "status": status, "error": error, "counts": dumps(counts or {}),
                 "recheck": recheck_in_s},
            )
            row = self._row(cur.fetchone())
            if row is None:
                raise OwnershipLost(f"finish: session {fence.session_id} is no longer ours")
            return row
        return self._tx(run)

    def claim_side_effects(self, fence: Fence) -> bool:
        """The once-per-session guard: True for exactly one caller."""
        def run(cur):
            self._fence(cur, fence)
            cur.execute(
                "UPDATE launch_sessions SET side_effects_fired_at = now() WHERE id = %s::uuid "
                "AND side_effects_fired_at IS NULL RETURNING 1",
                (fence.session_id,),
            )
            return cur.fetchone() is not None
        return self._tx(run)

    def reset_side_effects(self, session_id: str) -> None:
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET side_effects_fired_at = NULL WHERE id = %s::uuid", (session_id,),
            )
        self._tx(run)

    def upsert_rows(self, fence: Fence, rows: Sequence[Dict[str, Any]], *, chunk: int = 100) -> int:
        """Fenced upsert of candidate rows (one transaction per chunk)."""
        written = 0
        for i in range(0, len(rows), max(1, chunk)):
            part = rows[i:i + chunk]

            def run(cur, part=part):
                hit = self._fence(cur, fence, bump_rev=True)
                _insert_candidates(cur, fence.session_id, part, rev=int(hit["cand_rev"]), upsert=True)
            self._tx(run)
            written += len(part)
        return written

    def set_states(
        self, fence: Optional[Fence], items: Sequence[Dict[str, Any]], *,
        session_id: Optional[str] = None, from_states: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """Update rows' state / reason / contact (merged) / failure fields.
        Fenced, or (fence=None, session_id=…) for a finished session's
        housekeeping re-check. ``from_states`` limits which rows may change."""
        if not items:
            return []
        sid = fence.session_id if fence is not None else str(session_id)

        def run(cur):
            if fence is not None:
                rev = int(self._fence(cur, fence, bump_rev=True)["cand_rev"])
            else:
                cur.execute(
                    "UPDATE launch_sessions SET cand_rev = cand_rev + 1 WHERE id = %s::uuid RETURNING cand_rev",
                    (sid,),
                )
                hit = cur.fetchone()
                if hit is None:
                    return []
                rev = int(hit["cand_rev"])
            return _apply_states(cur, sid, items, rev, from_states=from_states)
        return self._tx(run)

    def stream_append(self, fence: Fence, attempt: int, chunk: int, events: Sequence[Dict[str, Any]]) -> None:
        def run(cur):
            self._fence(cur, fence)
            cur.execute(
                "INSERT INTO launch_session_stream (session_id, attempt, chunk, events) "
                "VALUES (%s::uuid, %s, %s, %s::jsonb) ON CONFLICT (session_id, attempt, chunk) "
                "DO UPDATE SET events = EXCLUDED.events",
                (fence.session_id, int(attempt), int(chunk), dumps(list(events))),
            )
        self._tx(run)

    def s1_flush(
        self,
        fence: Fence,
        *,
        attempt: int,
        chunk: int,
        events: Sequence[Dict[str, Any]],
        rows: Sequence[Dict[str, Any]],
        checkpoint_s1: Dict[str, Any],
        counts: Dict[str, Any],
    ) -> bool:
        """One S1 flush in ONE fenced transaction: the raw events (for
        replay), the changed table rows (for the browser) and the s1 progress.
        Returns cancel_requested (the Stop button acts at the next flush)."""
        def run(cur):
            hit = self._fence(cur, fence, bump_rev=bool(rows))
            if events:
                cur.execute(
                    "INSERT INTO launch_session_stream (session_id, attempt, chunk, events) "
                    "VALUES (%s::uuid, %s, %s, %s::jsonb) ON CONFLICT (session_id, attempt, chunk) "
                    "DO UPDATE SET events = EXCLUDED.events",
                    (fence.session_id, int(attempt), int(chunk), dumps(list(events))),
                )
            if rows:
                _insert_candidates(cur, fence.session_id, rows, rev=int(hit["cand_rev"]), upsert=True)
            cur.execute(
                "UPDATE launch_sessions SET checkpoint = checkpoint || jsonb_build_object('s1', %s::jsonb), "
                "counts = counts || %s::jsonb WHERE id = %s::uuid",
                (dumps(checkpoint_s1), dumps(counts), fence.session_id),
            )
            return bool(hit["cancel_requested"])
        return self._tx(run)

    def stream_delete(self, fence: Fence, *, from_attempt: Optional[int] = None) -> None:
        def run(cur):
            self._fence(cur, fence)
            if from_attempt is None:
                cur.execute("DELETE FROM launch_session_stream WHERE session_id = %s::uuid", (fence.session_id,))
            else:
                cur.execute(
                    "DELETE FROM launch_session_stream WHERE session_id = %s::uuid AND attempt >= %s",
                    (fence.session_id, int(from_attempt)),
                )
        self._tx(run)

    # ── housekeeping ──────────────────────────────────────────────────────
    def housekeeping(
        self, *, collecting_ttl_min: int, review_ttl_h: int, retention_days: int, recheck_h: int,
        queued_max_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Idempotent expiry + retention, run by ONE worker per tick
        (pg_try_advisory_xact_lock). Returns what it did.

        ``queued_max_s`` (> 0): a session still QUEUED and never claimed
        (claims = 0, no owner) whose updated_at — the time it was queued — is
        older than that is cancelled ('queue_expired'). Claim-safe: the rows
        are locked FOR UPDATE SKIP LOCKED (a row a claim holds is skipped)
        and the UPDATE re-checks status = 'queued', so a session a worker
        claimed in the meantime is never touched. A session re-queued after
        it ran (a confirmed QA review: claims > 0) is never expired here."""
        def run(cur):
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS got", (S.HOUSEKEEPING_LOCK_KEY,))
            if not cur.fetchone()["got"]:
                return {"skipped": True}
            out: Dict[str, Any] = {"skipped": False}
            cur.execute(
                "UPDATE launch_sessions SET status = 'cancelled', completed_at = now(), updated_at = now(), "
                "error = 'upload abandoned', cancel_reason = 'upload abandoned' "
                "WHERE status = 'collecting' AND updated_at < now() - make_interval(mins => %s) "
                "RETURNING id::text AS id",
                (int(collecting_ttl_min),),
            )
            out["collecting_expired"] = [r["id"] for r in cur.fetchall()]
            cur.execute(
                "UPDATE launch_sessions SET status = 'cancelled', completed_at = now(), updated_at = now(), "
                "error = 'review expired', cancel_reason = 'review expired', "
                "human_wait_ms = human_wait_ms + GREATEST(0, (EXTRACT(EPOCH FROM "
                "(now() - COALESCE(review_started_at, updated_at))) * 1000)::bigint) "
                "WHERE status = 'awaiting_input' "
                "AND COALESCE(review_started_at, updated_at) < now() - make_interval(hours => %s) "
                "RETURNING id::text AS id",
                (int(review_ttl_h),),
            )
            out["review_expired"] = [r["id"] for r in cur.fetchall()]
            for sid in out["collecting_expired"] + out["review_expired"]:
                _append_event_cur(cur, sid, {"type": "cancelled", "reason": "expired"})
            out["queue_expired"] = []
            if queued_max_s is not None and int(queued_max_s) > 0:
                cur.execute(
                    "UPDATE launch_sessions SET status = 'cancelled', completed_at = now(), updated_at = now(), "
                    "error = 'queue expired: no worker started it in time', cancel_reason = 'queue_expired' "
                    "WHERE id IN (SELECT id FROM launch_sessions WHERE status = 'queued' AND claims = 0 "
                    " AND owner IS NULL AND updated_at < now() - make_interval(secs => %(max_s)s) "
                    " FOR UPDATE SKIP LOCKED) "
                    "AND status = 'queued' AND claims = 0 AND owner IS NULL "
                    "RETURNING id::text AS id",
                    {"max_s": int(queued_max_s)},
                )
                out["queue_expired"] = [r["id"] for r in cur.fetchall()]
                for sid in out["queue_expired"]:
                    _append_event_cur(cur, sid, {"type": "cancelled", "reason": "queue_expired"})
            cur.execute(
                "UPDATE launch_sessions SET recheck_at = NULL WHERE recheck_at IS NOT NULL "
                "AND completed_at < now() - make_interval(hours => %s)",
                (int(recheck_h),),
            )
            cur.execute(
                "DELETE FROM launch_session_candidates WHERE session_id IN ("
                " SELECT id FROM launch_sessions WHERE status = ANY(%s)"
                " AND completed_at < now() - make_interval(days => %s) AND recheck_at IS NULL)",
                (list(S.TERMINAL_STATUSES), int(retention_days)),
            )
            out["rows_deleted"] = cur.rowcount
            cur.execute(
                "DELETE FROM launch_session_stream WHERE session_id IN ("
                " SELECT id FROM launch_sessions WHERE status = ANY(%s) OR selection_done_at IS NOT NULL)",
                (list(S.TERMINAL_STATUSES),),
            )
            out["stream_deleted"] = cur.rowcount
            return out
        return self._tx(run)

    def claim_rechecks(self, *, limit: int, every_s: int) -> List[Dict[str, Any]]:
        """Finished sessions whose unconfirmed rows are due a by-job re-check;
        each is pushed ``every_s`` ahead in the same statement (one worker)."""
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions s SET recheck_at = now() + make_interval(secs => %(every)s) "
                "WHERE s.id IN (SELECT id FROM launch_sessions WHERE recheck_at IS NOT NULL "
                " AND recheck_at <= now() AND status = ANY(%(terminal)s) ORDER BY recheck_at "
                " LIMIT %(limit)s FOR UPDATE SKIP LOCKED) RETURNING s.*",
                {"every": int(every_s), "terminal": list(S.TERMINAL_STATUSES), "limit": max(1, int(limit))},
            )
            return [self._row(r) for r in cur.fetchall()]
        return self._tx(run)

    def finish_recheck(self, session_id: str, *, counts: Dict[str, Any], done: bool) -> None:
        def run(cur):
            cur.execute(
                "UPDATE launch_sessions SET counts = counts || %s::jsonb, "
                "recheck_at = CASE WHEN %s THEN NULL ELSE recheck_at END WHERE id = %s::uuid",
                (dumps(counts), bool(done), session_id),
            )
        self._tx(run)


# ── module helpers (shared by the store methods) ─────────────────────────────

class _TooMany(Exception):
    def __init__(self, total: int) -> None:
        super().__init__(total)
        self.total = total


def _keys(job_keys: Sequence[str]) -> List[str]:
    out: List[str] = []
    for k in job_keys or ():
        s = str(k or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _candidate_record(r: Dict[str, Any], i: int) -> Dict[str, Any]:
    return {
        "candidate_id": str(r["candidate_id"]),
        "ord": int(r.get("ord", i) if r.get("ord") is not None else i),
        "source": None if r.get("source") is None else str(r.get("source")),
        "row": r.get("row") or {},
        "match_score": finite_score(r.get("match_score")),
        "state": r.get("state") or S.ROW_POOLED,
        "reason": r.get("reason"),
        "contact": r.get("contact"),
    }


def _insert_candidates(cur, session_id: str, rows: Sequence[Dict[str, Any]], *, rev: int, upsert: bool = True) -> None:
    records = [_candidate_record(r, i) for i, r in enumerate(rows)]
    if not records:
        return
    conflict = (
        "DO UPDATE SET ord = EXCLUDED.ord, source = EXCLUDED.source, row = EXCLUDED.row, "
        "match_score = EXCLUDED.match_score, state = EXCLUDED.state, reason = EXCLUDED.reason, "
        "contact = COALESCE(EXCLUDED.contact, launch_session_candidates.contact), "
        "rev = EXCLUDED.rev, updated_at = now()"
        if upsert else "DO NOTHING"
    )
    cur.execute(
        "INSERT INTO launch_session_candidates "
        "(session_id, candidate_id, ord, source, row, match_score, state, reason, contact, rev, updated_at) "
        f"SELECT %(sid)s::uuid, r.candidate_id, r.ord, r.source, r.row, r.match_score, "
        f"COALESCE(r.state, 'pooled'), r.reason, r.contact, %(rev)s, now() "
        f"FROM jsonb_to_recordset(%(rows)s::jsonb) AS r({_RECORDSET_COLS}) "
        f"ON CONFLICT (session_id, candidate_id) {conflict}",
        {"sid": session_id, "rev": int(rev), "rows": dumps(records)},
    )


def _apply_states(
    cur, session_id: str, items: Sequence[Dict[str, Any]], rev: int, *, from_states: Optional[Sequence[str]] = None,
) -> List[str]:
    records = [
        {
            "candidate_id": str(it["candidate_id"]),
            "state": it.get("state"),
            "reason": it.get("reason"),
            "contact": it.get("contact"),
            "failure_stage": it.get("failure_stage"),
            "batch_index": it.get("batch_index"),
            "interview_id": it.get("interview_id"),
            "error": None if it.get("error") is None else str(it.get("error"))[:500],
            "row": it.get("row"),
        }
        for it in items
    ]
    cur.execute(
        f"""
        WITH r AS (SELECT * FROM jsonb_to_recordset(%(items)s::jsonb) AS r({_STATE_COLS}))
        UPDATE launch_session_candidates c SET
            state = COALESCE(r.state, c.state),
            reason = CASE WHEN r.state IS NULL THEN c.reason ELSE r.reason END,
            contact = CASE WHEN r.contact IS NULL THEN c.contact
                           ELSE COALESCE(c.contact, '{{}}'::jsonb) || r.contact END,
            failure_stage = COALESCE(r.failure_stage, c.failure_stage),
            batch_index = COALESCE(r.batch_index, c.batch_index),
            interview_id = COALESCE(r.interview_id, c.interview_id),
            error = COALESCE(r.error, c.error),
            row = COALESCE(r.row, c.row),
            rev = %(rev)s, updated_at = now()
        FROM r
        WHERE c.session_id = %(sid)s::uuid AND c.candidate_id = r.candidate_id
          AND (%(from)s::text[] IS NULL OR c.state = ANY(%(from)s::text[]))
        RETURNING c.candidate_id
        """,
        {"items": dumps(records), "rev": int(rev), "sid": session_id,
         "from": list(from_states) if from_states is not None else None},
    )
    return [str(r["candidate_id"]) for r in cur.fetchall()]


def _append_event_cur(cur, session_id: str, payload: Dict[str, Any]) -> Optional[int]:
    cur.execute(
        "WITH s AS (UPDATE launch_sessions SET event_seq = event_seq + 1 WHERE id = %(sid)s::uuid "
        "RETURNING event_seq) INSERT INTO launch_session_events (session_id, seq, payload) "
        "SELECT %(sid)s::uuid, s.event_seq, %(payload)s::jsonb FROM s RETURNING seq",
        {"sid": session_id, "payload": dumps(payload)},
    )
    row = cur.fetchone()
    return int(row["seq"]) if row else None


def _fold_state_counts(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    states: Dict[str, int] = {}
    reasons: Dict[str, Dict[str, int]] = {}
    for r in rows:
        state, kind, n = str(r["state"]), str(r.get("kind") or ""), int(r["n"])
        states[state] = states.get(state, 0) + n
        if kind:
            bucket = reasons.setdefault(state, {})
            bucket[kind] = bucket.get(kind, 0) + n
    return {"states": states, "reasons": reasons}


def fold_engage_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fold a candidate's sourced rows (one per job key) into one fact:
    interview id (row or audit), every engage_status seen, the session /
    bulk ids, the block reason."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cid = str(r["candidate_id"])
        f = out.setdefault(cid, {
            "candidate_id": cid, "statuses": [], "interview_id": "", "session_ids": [],
            "bulk_ids": [], "block_reason": "",
        })
        status = str(r.get("engage_status") or "").lower()
        if status not in f["statuses"]:
            f["statuses"].append(status)
        iid = str(r.get("interview_id") or "").strip() or str(r.get("audit_interview_id") or "").strip()
        if iid and not f["interview_id"]:
            f["interview_id"] = iid
        for key, field in (("engage_session_id", "session_ids"), ("engage_bulk_id", "bulk_ids")):
            v = str(r.get(key) or "").strip()
            if v and v not in f[field]:
                f[field].append(v)
        if r.get("block_reason") and not f["block_reason"]:
            f["block_reason"] = str(r["block_reason"])
    return out
