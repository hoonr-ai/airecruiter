"""Engagement package: claims."""

from typing import Optional, List, Any, Dict, Tuple, Sequence

from .common import logger
from .pairbot import _job_key_list
from . import common


# --- Pairbot DuplicateInterview ----------------------------------------------
# Pairbot answers a duplicate with failed_interviews[] {resume_index,
# error_type: "DuplicateInterview", existing_interview_id}. Its dedupe
# (get_interview_by_candidate_and_role) is scoped to the ONE bulk being created
# (bulk_jd_id; Pairbot origin/main d03be9b): every POST stores a new bulk_jds
# row, and the candidate+role unique index is gone (migration 0007). So Pairbot
# does NOT stop a second bulk from calling the same person again — PAIR's own
# send claim (_SEND_CLAIM_SQL) is what prevents that. Within one bulk a
# duplicate means two PAIR ids for one person (a twin). Older Pairbot deduped
# per person + LOWER(role title) across all jobs, where two jobs titled "Java
# Developer" collided. Either way a duplicate is recorded as launched for this
# job only when by-job lists an interview under the candidate's OWN id;
# otherwise the row is failed + blocked (never re-posted: Pairbot would only
# answer the same duplicate again):
LAUNCH_BLOCK_DUPLICATE_OTHER_JOB = "duplicate_role_other_job"
# by-job lists the existing interview for THIS job under another PAIR id: the
# person is already being interviewed for this job under their other profile.
LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB = "duplicate_person_this_job"
# by-job could not be read (or could not yet rule out a bulk still creating),
# so the duplicate could not be placed. Blocked the same way (fail-safe); a
# later reconcile_with_pairbot heals it to 'sent' if by-job does list it under
# this candidate for this job.
LAUNCH_BLOCK_DUPLICATE_UNVERIFIED = "duplicate_unverified"
_LAUNCH_BLOCK_REASON_TEXT = {
    LAUNCH_BLOCK_DUPLICATE_OTHER_JOB: (
        "already has a PAIR interview for another job with the same title"
    ),
    LAUNCH_BLOCK_DUPLICATE_PERSON_THIS_JOB: (
        "already has a PAIR interview for this job under another profile"
    ),
    LAUNCH_BLOCK_DUPLICATE_UNVERIFIED: (
        "PAIR already has an interview for this person and role that could not be matched to this job"
    ),
}


def _launch_block_reason_text(reason: str) -> str:
    return _LAUNCH_BLOCK_REASON_TEXT.get(reason, f"launch blocked ({reason})")


def _pairbot_rejected_ids(
    failed_interviews: Any,
    payload_obj: Dict[str, Any],
    candidate_ids: Sequence[str],
) -> set:
    """Candidate ids Pairbot's ``failed_interviews`` explicitly names (any
    error type): placed by ``resume_index`` into the posted payload, or by an
    echoed ``source_candidate_id`` / ``candidate_id``. A candidate Pairbot
    answered for is a definite outcome; one it is silent about is not."""
    if not isinstance(failed_interviews, list):
        return set()
    resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
    resumes = resumes if isinstance(resumes, list) else []
    wanted = {str(c) for c in candidate_ids}
    out: set = set()
    for item in failed_interviews:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("source_candidate_id") or item.get("candidate_id") or "").strip()
        raw_idx = item.get("resume_index")
        if not cid and not isinstance(raw_idx, bool):
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                idx = -1
            if 0 <= idx < len(resumes) and isinstance(resumes[idx], dict):
                cid = str(resumes[idx].get("source_candidate_id") or "").strip()
        if cid and cid in wanted:
            out.add(cid)
    return out


def _map_duplicate_interviews(
    failed_interviews: Any,
    payload_obj: Dict[str, Any],
    candidate_ids: Sequence[str],
    created_ids: Optional[set] = None,
) -> Dict[str, Dict[str, Any]]:
    """{candidate_id: Pairbot's failed_interviews item} for DuplicateInterview
    answers (the item carries ``existing_interview_id``). ``resume_index``
    indexes the EXACT payload that was posted, so the candidate is
    ``payload_obj["resumes"][resume_index].source_candidate_id`` — never a
    position in ``candidate_ids`` (the resumes are only re-sliced when the
    lengths line up). Ids outside ``candidate_ids`` or already created in this
    bulk (``created_ids``) are ignored; the first item per candidate wins."""
    resumes = payload_obj.get("resumes") if isinstance(payload_obj, dict) else None
    if not isinstance(resumes, list) or not isinstance(failed_interviews, list):
        return {}
    wanted = {str(c) for c in candidate_ids}
    created = created_ids or set()
    out: Dict[str, Dict[str, Any]] = {}
    for item in failed_interviews:
        if not isinstance(item, dict) or item.get("error_type") != "DuplicateInterview":
            continue
        raw_idx = item.get("resume_index")
        if isinstance(raw_idx, bool):
            continue
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(resumes) or not isinstance(resumes[idx], dict):
            continue
        cid = str(resumes[idx].get("source_candidate_id") or "").strip()
        if not cid or cid not in wanted or cid in created:
            continue
        out.setdefault(cid, item)
    return out


# --- Per-candidate launch claims ------------------------------------------------
# Two conditional claims guard every Pairbot POST, so no two launch paths can
# post the same person (Pairbot itself does not stop a second bulk — see
# "Pairbot DuplicateInterview" above):
#
#   session claim  a server launch session claims its rows BEFORE it starts
#                  posting them: engage_status 'queued' + data.engage_session_id
#                  (claim_candidates_for_session). Legacy paths read 'queued' as
#                  already sent and their send claim cannot take it.
#   send claim     EVERY path (/engage/launch, /engage/send-bulk-interview,
#                  Rankings Engage, auto-launch, sessions) moves the rows it is
#                  about to post to 'sending' in the core, immediately before
#                  the POST (_claim_rows_for_send), and posts only what it won.
#                  The idempotency read before it is advisory: the core can
#                  spend minutes on employer resolution between that read and
#                  the POST.
#
# Both lock the candidate's rows in id order (SELECT … ORDER BY id FOR UPDATE:
# no lock-order deadlock; a waiter re-reads the committed row) and take a
# candidate only when EVERY row it has under the job's keys qualifies and no
# audit row holds an interview id for it. A row qualifies while it has no
# interview id and no engage_block_reason and engage_status is '' or 'failed'
# — or 'queued' for the claiming session (an idempotent re-claim on resume).
# The session claim records the status it replaced in
# data.engage_status_before_claim; releasing the claim restores it.
_LAUNCH_CLAIM_LOCK_CTE = """
    locked AS (
        SELECT sc.id, sc.candidate_id, sc.data
        FROM sourced_candidates sc
        WHERE sc.jobdiva_id = ANY(%(keys)s)
          AND sc.candidate_id = ANY(%(ids)s)
        ORDER BY sc.id
        FOR UPDATE
    ),
    eligible AS (
        SELECT l.candidate_id
        FROM locked l
        GROUP BY l.candidate_id
        HAVING bool_and(
            COALESCE(l.data->>'engage_interview_id', '') = ''
            AND COALESCE(l.data->>'engage_block_reason', '') = ''
            AND (
                COALESCE(l.data->>'engage_status', '') IN ('', 'failed')
                OR (%(sid)s <> ''
                    AND l.data->>'engage_status' = 'queued'
                    AND COALESCE(l.data->>'engage_session_id', '') = %(sid)s)
            )
        )
        AND NOT EXISTS (
            SELECT 1 FROM engage_interview_audit a
            WHERE a.candidate_id = l.candidate_id
              AND a.jobdiva_id = ANY(%(keys)s)
              AND COALESCE(a.interview_id, '') <> ''
        )
    )
"""

_CLAIM_SESSION_SQL = (
    "WITH" + _LAUNCH_CLAIM_LOCK_CTE + """
    UPDATE sourced_candidates t
    SET data = COALESCE(t.data, '{}'::jsonb)
               || jsonb_build_object('engage_status', 'queued', 'engage_session_id', %(sid)s::text)
               || CASE WHEN t.data->>'engage_status' = 'queued' THEN '{}'::jsonb
                       ELSE jsonb_build_object(
                           'engage_status_before_claim', COALESCE(t.data->>'engage_status', ''))
                  END,
        updated_at = CURRENT_TIMESTAMP
    FROM locked l
    JOIN eligible e ON e.candidate_id = l.candidate_id
    WHERE t.id = l.id
    RETURNING t.candidate_id
"""
)

# The send claim. Returns (candidate_id, claimed) for every candidate that has
# a row under the job's keys; a candidate with no row is absent.
_SEND_CLAIM_SQL = (
    "WITH" + _LAUNCH_CLAIM_LOCK_CTE + """,
    claimed AS (
        UPDATE sourced_candidates t
        SET data = COALESCE(t.data, '{}'::jsonb) || jsonb_build_object('engage_status', 'sending'),
            updated_at = CURRENT_TIMESTAMP
        FROM locked l
        JOIN eligible e ON e.candidate_id = l.candidate_id
        WHERE t.id = l.id
        RETURNING t.candidate_id
    )
    SELECT DISTINCT l.candidate_id,
           EXISTS (SELECT 1 FROM claimed c WHERE c.candidate_id = l.candidate_id) AS claimed
    FROM locked l
"""
)

# Undo a send claim when nothing (or nothing provable) reached Pairbot:
# 'sending' → 'queued' for the claiming session's own rows (the session keeps
# them and reconciles before any re-post), else → 'failed' (retryable, the
# state a failed POST has always left).
_RELEASE_SEND_CLAIM_SQL = """
    UPDATE sourced_candidates
    SET data = COALESCE(data, '{}'::jsonb) || jsonb_build_object(
            'engage_status',
            CASE WHEN %(sid)s <> '' AND COALESCE(data->>'engage_session_id', '') = %(sid)s
                 THEN 'queued' ELSE 'failed' END),
        updated_at = CURRENT_TIMESTAMP
    WHERE jobdiva_id = ANY(%(keys)s)
      AND candidate_id = ANY(%(ids)s)
      AND data->>'engage_status' = 'sending'
    RETURNING candidate_id
"""


# --- Person gate (services/launch_person_keys.py) ------------------------------
# The claims above are per candidate_id, but one human can be several ids
# (JobDiva numeric, exa_<url>, a Unipile id, a manual upload …). Before either
# claim runs, the same transaction takes a per-job advisory lock (so two
# claims for different ids of one person serialize) and drops every requested
# id whose email / phone / LinkedIn / JobDiva id is already HELD for the job by
# another candidate_id — an interview id, or queued/sending/processing/sent —
# or by an earlier id of the same request. Nothing is written for a dropped
# id: if the holder's launch later fails, the person is launchable again.
_LAUNCH_PERSON_LOCK_KEY = 728199  # 728193-728198 are taken (see launch_sessions/schema.py)

_PERSON_GATE_ROWS_SQL = """
    SELECT sc.candidate_id, sc.source, sc.email, sc.phone, sc.profile_url,
           sc.data->>'email', sc.data->>'phone', sc.data->>'profile_url',
           sc.data->>'linkedin_url', sc.data->>'public_profile_url', sc.data->'urls'->>'linkedin',
           sc.data->>'jobdiva_candidate_id',
           COALESCE(sc.data->>'engage_status', ''), COALESCE(sc.data->>'engage_interview_id', ''),
           COALESCE(sc.data->>'engage_session_id', '')
    FROM sourced_candidates sc
    WHERE sc.jobdiva_id = ANY(%(keys)s)
      AND (sc.candidate_id = ANY(%(ids)s)
           OR COALESCE(sc.data->>'engage_interview_id', '') <> ''
           OR LOWER(COALESCE(sc.data->>'engage_status', '')) IN ('queued', 'sending', 'processing', 'sent'))
    ORDER BY sc.id
"""

_PERSON_GATE_AUDIT_SQL = """
    SELECT DISTINCT candidate_id, candidate_email
    FROM engage_interview_audit
    WHERE jobdiva_id = ANY(%(keys)s)
      AND COALESCE(interview_id, '') <> ''
      AND COALESCE(candidate_email, '') <> ''
"""


def _person_gate_row(r: Sequence[Any]) -> Dict[str, Any]:
    return {
        "candidate_id": r[0], "source": r[1], "email": r[2], "phone": r[3], "profile_url": r[4],
        "data": {
            "email": r[5], "phone": r[6], "profile_url": r[7], "linkedin_url": r[8],
            "public_profile_url": r[9], "urls": {"linkedin": r[10]}, "jobdiva_candidate_id": r[11],
        },
        "engage_status": r[12], "engage_interview_id": r[13], "engage_session_id": r[14],
    }


def _person_gate(
    cur,
    keys: List[str],
    ids: List[str],
    session_id: str,
    fallback_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Tuple[str, str]]:
    """Run inside a claim transaction (before the claim statement). Returns
    ``{candidate_id: (holder candidate_id, key kind)}`` for the requested ids
    to drop. ``fallback_rows`` gives contacts for requested ids with no saved
    row (legacy posts those unclaimed — they must still not double a person)."""
    from services import launch_person_keys as lpk

    for k in sorted(set(keys)):
        cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_LAUNCH_PERSON_LOCK_KEY, k))
    cur.execute(_PERSON_GATE_ROWS_SQL, {"keys": keys, "ids": ids})
    wanted = set(ids)
    order = {cid: i for i, cid in enumerate(ids)}
    requested: List[Tuple[str, List[str]]] = []
    holders: List[Tuple[str, List[str]]] = []
    with_rows: set = set()
    for raw in cur.fetchall() or []:
        row = _person_gate_row(raw)
        cid = str(row["candidate_id"] or "")
        pkeys = lpk.person_keys(row)
        if cid in wanted:
            requested.append((cid, pkeys))
            with_rows.add(cid)
        if lpk.holds_person(row, session_id=session_id):
            holders.append((cid, pkeys))
    for cid, row in (fallback_rows or {}).items():
        if cid in wanted and cid not in with_rows:
            requested.append((cid, lpk.person_keys(row)))
    cur.execute(_PERSON_GATE_AUDIT_SQL, {"keys": keys})
    for cid, email in cur.fetchall() or []:
        ek = lpk.email_key(email)
        if ek:
            holders.append((str(cid or ""), [f"{lpk.KEY_EMAIL}:{ek}"]))
    requested.sort(key=lambda e: order.get(e[0], len(order)))
    dups = lpk.find_person_duplicates(requested, holders)
    for cid, (other, kind) in dups.items():
        logger.warning(
            "duplicate_person job=%s candidate=%s holder=%s key=%s session=%s",
            keys[0], cid, other, kind, bool(session_id),
        )
    return dups


def _person_duplicate_reason(other: str, kind: str) -> str:
    return f"Same person already launched for this job (matched by {kind}; candidate {other})"


def _claim_rows_for_send(
    job_keys: Sequence[str],
    candidate_ids: Sequence[str],
    session_id: Optional[str],
    fallback_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[List[str], set, Dict[str, Tuple[str, str]]]:
    """Run the person gate + the send claim (sync). Returns (claimed ids in
    input order, every id that has a row under the job's keys, the ids the
    person gate dropped as ``{id: (holder id, key kind)}``). Raises on a DB
    error — the caller then posts nothing."""
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    if not keys or not ids:
        return [], set(), {}
    sid = str(session_id or "").strip()
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        dups = _person_gate(cur, keys, ids, sid, fallback_rows)
        gated = [c for c in ids if c not in dups]
        rows = []
        if gated:
            cur.execute(_SEND_CLAIM_SQL, {"keys": keys, "ids": gated, "sid": sid})
            rows = cur.fetchall() or []
        conn.commit()
        cur.close()
    finally:
        conn.close()
    with_rows = {str(r[0]) for r in rows}
    claimed = {str(r[0]) for r in rows if r[1]}
    return [c for c in ids if c in claimed], with_rows, dups


def _release_send_claim(
    job_keys: Sequence[str], candidate_ids: Sequence[str], session_id: Optional[str]
) -> int:
    """Undo a send claim (sync, fail-open: logs and returns 0 on error)."""
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in candidate_ids or [] if str(c).strip()]
    if not keys or not ids:
        return 0
    try:
        conn = common._get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                _RELEASE_SEND_CLAIM_SQL,
                {"keys": keys, "ids": ids, "sid": str(session_id or "").strip()},
            )
            n = len(cur.fetchall() or [])
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — rows stay 'sending' (never re-posted)
        logger.error(
            "engage_send_claim_release_failed candidates=%d err=%s — rows left 'sending'",
            len(ids), exc,
        )
        return 0
    return n


def claim_candidates_for_session(
    job_keys: Tuple[str, ...],
    candidate_ids: Sequence[str],
    session_id: str,
    duplicates: Optional[Dict[str, Tuple[str, str]]] = None,
) -> List[str]:
    """Atomically claim rows for a launch session (sync; call via to_thread).

    Returns the claimed candidate ids in input order; only these may be sent
    (with ``claimed_session_id=session_id``). Ids not returned are launched,
    in flight, blocked, owned by another session, or (person gate) another id
    of a person already held for the job — never post them. ``duplicates``,
    when given, is filled with the person gate's ``{id: (holder id, kind)}``.
    """
    keys = _job_key_list(job_keys)
    ids = [str(c) for c in dict.fromkeys(candidate_ids or []) if str(c).strip()]
    sid = str(session_id or "").strip()
    if not keys or not ids or not sid:
        return []
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        dups = _person_gate(cur, keys, ids, sid)
        gated = [c for c in ids if c not in dups]
        claimed: set = set()
        if gated:
            cur.execute(_CLAIM_SESSION_SQL, {"keys": keys, "ids": gated, "sid": sid})
            claimed = {str(r[0]) for r in (cur.fetchall() or [])}
        conn.commit()
        cur.close()
    finally:
        conn.close()
    if duplicates is not None:
        duplicates.update(dups)
    return [c for c in ids if c in claimed]


def release_session_claims(
    session_id: str,
    job_keys: Tuple[str, ...],
    candidate_ids: Optional[Sequence[str]] = None,
) -> int:
    """Hand a session's leftover 'queued' rows back: engage_status returns to
    what it was before the claim (data.engage_status_before_claim — '' or
    'failed'; '' for a claim older than that key), and engage_session_id +
    engage_status_before_claim are dropped. A previously failed row stays
    'failed', so auto-launch (which only takes '' rows) does not pick it up.
    Optionally only ``candidate_ids``. Rows another path moved on
    (sending/processing/sent/failed) are untouched. Sync; returns rows released."""
    keys = _job_key_list(job_keys)
    sid = str(session_id or "").strip()
    if not keys or not sid:
        return 0
    params: Dict[str, Any] = {"keys": keys, "sid": sid}
    id_filter = ""
    if candidate_ids is not None:
        params["ids"] = [str(c) for c in candidate_ids if str(c).strip()]
        if not params["ids"]:
            return 0
        id_filter = "AND candidate_id = ANY(%(ids)s)"
    conn = common._get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE sourced_candidates
            SET data = (COALESCE(data, '{{}}'::jsonb) - 'engage_session_id' - 'engage_status_before_claim')
                       || jsonb_build_object(
                            'engage_status', COALESCE(data->>'engage_status_before_claim', '')),
                updated_at = CURRENT_TIMESTAMP
            WHERE jobdiva_id = ANY(%(keys)s)
              AND data->>'engage_status' = 'queued'
              AND data->>'engage_session_id' = %(sid)s
              {id_filter}
            RETURNING candidate_id
            """,
            params,
        )
        n = len(cur.fetchall() or [])
        conn.commit()
        cur.close()
    finally:
        conn.close()
    return n
