"""Cross submissions — previously screened candidates for a new job.

When a recruiter sources a new job, look back over every candidate PAIR
already reached out to (through Pairbot) in the last N days who *responded*
— i.e. the phone screen is at least partially complete (`in_progress`) or
finished (`passed` / `failed` / `completed`) — score them against the new
job's Step-5 criteria, and email the recruiter a separate "cross
submissions" list of the relevant ones.

Data model this reads (nothing new is captured — it all already exists):

* ``sourced_candidates`` — one row per (job, candidate, source). The launch
  path stamps ``data.engage_status`` + ``data.engage_interview_id``; the
  Pairbot webhook (``routers/voice_agent.py``) then writes the screen result
  into the same JSONB blob: ``engage_status`` (in_progress|passed|failed),
  ``engage_updated_at``, ``engage_completed_at``, ``engage_score``,
  ``engage_total_score``, ``engage_hard_filter_status`` and the full
  transcript under ``engage_last_response``.
* ``engage_interview_audit`` — one row per interview sent (payload + raw
  webhook response). Not needed here; sourced_candidates carries the
  resolved state.
* ``monitored_jobs`` — the job the outreach belonged to (title, client,
  recruiters).

Writes:

* ``cross_submissions`` — one row per (new job, person) surfaced, so the
  list is auditable, the UI can show it, and a person is emailed at most
  once per job (the INSERT ... ON CONFLICT DO NOTHING is the claim).
* ``monitored_jobs.cross_submissions_checked_at`` — throttle stamp; the
  Step-5 hook may fire on every search, the scan runs at most once per
  ``CROSS_SUBMISSIONS_THROTTLE_MINUTES`` unless forced.

Identity: ``candidate_id`` is source-scoped, so a *person* is keyed by
normalised email, else phone digits, else (source, candidate_id) — see the
``candidate-identity-is-not-candidate-id`` note. The same-job exclusion is
by base JobDiva ref (``26-06182-v2`` → ``26-06182``) so re-versioned jobs
never re-list their own candidates.

Everything here is fail-open: any failure logs and returns an empty
summary; it must never break the Step-5 search that triggered it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import psycopg2.extras

from core.config import (
    CROSS_SUBMISSIONS_ENABLED,
    CROSS_SUBMISSIONS_LOOKBACK_DAYS,
    CROSS_SUBMISSIONS_MAX_LISTED,
    CROSS_SUBMISSIONS_MAX_SCORED,
    CROSS_SUBMISSIONS_MIN_SCORE,
    CROSS_SUBMISSIONS_THROTTLE_MINUTES,
)
from core.db import get_db_connection

logger = logging.getLogger(__name__)

# Raw engage_status values that mean "the candidate picked up and answered
# at least part of the screen". Mirrors the rank-list vocabulary in
# routers/candidates._format_engage_status: there is no raw
# "partially_completed" — partial == in_progress; completed == Pass + Fail.
RESPONDED_STATUSES: Tuple[str, ...] = (
    "in_progress",
    "in progress",
    "completed",
    "passed",
    "pass",
    "hired",
    "failed",
    "fail",
    "rejected",
)

_VERSION_SUFFIX_RE = re.compile(r"-v\d+$", re.IGNORECASE)

Scorer = Callable[[Dict[str, Any], Any], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def json_load_safe(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse the timestamp shapes the webhook/launch paths write.

    Accepts datetime objects, ISO-8601 strings (with ``Z`` or an offset,
    or naive → assumed UTC) and a couple of common Pairbot formats. Returns
    an aware UTC datetime or None.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def base_job_ref(ref: Any) -> str:
    """``26-06182-v2`` → ``26-06182`` (lower-cased); versions share a base."""
    text = str(ref or "").strip().lower()
    return _VERSION_SUFFIX_RE.sub("", text)


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    # Drop a leading US country code so +1 (415) 555-0100 == 4155550100.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def is_synthetic_email(email: str) -> bool:
    e = normalize_email(email)
    return (not e) or e.endswith("@jobdiva.com") or "available upon request" in e or "@" not in e


def person_key(email: Any, phone: Any, source: Any = "", candidate_id: Any = "") -> str:
    """Stable person identity: email, else phone, else (source, candidate_id)."""
    e = normalize_email(email)
    if e and not is_synthetic_email(e):
        return f"email:{e}"
    p = normalize_phone(phone)
    if len(p) >= 7:
        return f"phone:{p}"
    return f"id:{str(source or '').strip().lower()}:{str(candidate_id or '').strip()}"


def person_keys_for(email: Any, phone: Any, source: Any = "", candidate_id: Any = "") -> Set[str]:
    """Every key a row can be matched on (used for "already in this job")."""
    keys: Set[str] = set()
    e = normalize_email(email)
    if e and not is_synthetic_email(e):
        keys.add(f"email:{e}")
    p = normalize_phone(phone)
    if len(p) >= 7:
        keys.add(f"phone:{p}")
    cid = str(candidate_id or "").strip()
    if cid:
        keys.add(f"id:{str(source or '').strip().lower()}:{cid}")
    return keys


def screen_result_label(engage_status: Any, hard_filter_status: Any = None) -> str:
    """Pass / Fail / In Progress — same mapping as the rank list."""
    s = str(engage_status or "").strip().lower()
    if s in ("passed", "pass", "hired"):
        return "Pass"
    if s in ("failed", "fail", "rejected"):
        return "Fail"
    if s in ("in_progress", "in progress"):
        return "In Progress"
    if s == "completed":
        hf = str(hard_filter_status or "").strip().lower()
        return "Pass" if hf in ("", "pass", "passed", "not_hard_filter") else "Fail"
    return "Pending"


def has_responded(engage_status: Any) -> bool:
    return str(engage_status or "").strip().lower() in RESPONDED_STATUSES


def screened_at_from_blob(blob: Dict[str, Any], fallback: Any = None) -> Optional[datetime]:
    """When the candidate last answered: completed_at, else updated_at, else row ts."""
    for key in ("engage_completed_at", "engage_updated_at"):
        ts = parse_ts(blob.get(key))
        if ts:
            return ts
    return parse_ts(fallback)


def format_screen_score(score: Any, total: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return ""
    try:
        t = float(total)
    except (TypeError, ValueError):
        t = 0.0
    if t > 0:
        return f"{round(s / t * 100)}%"
    return f"{s:g}"


def build_scoring_payload(row: Dict[str, Any], blob: Dict[str, Any]) -> Dict[str, Any]:
    """Same shape ``routers.candidates._compute_resume_matching`` consumes."""
    return {
        "candidate_id": row.get("candidate_id"),
        "name": row.get("name"),
        "headline": row.get("headline"),
        "location": row.get("location"),
        "resume_text": row.get("resume_text") or blob.get("resume_text") or "",
        "skills": blob.get("skills") or [],
        "experience_years": blob.get("experience_years") or 0,
        "enhanced_info": blob.get("enhanced_info") or {},
        "data": blob,
        "match_score": row.get("resume_match_percentage") or blob.get("match_score") or 0,
    }


def select_candidates(
    prior_rows: Iterable[Dict[str, Any]],
    *,
    new_job_base_refs: Set[str],
    existing_person_keys: Set[str],
    cutoff: datetime,
    scorer: Optional[Scorer],
    criteria: Any,
    min_score: float = CROSS_SUBMISSIONS_MIN_SCORE,
    max_scored: int = CROSS_SUBMISSIONS_MAX_SCORED,
    max_listed: int = CROSS_SUBMISSIONS_MAX_LISTED,
) -> List[Dict[str, Any]]:
    """Filter → dedupe by person (most recent screen wins) → score → rank.

    ``prior_rows`` are sourced_candidates rows (dicts) joined with their
    job's title/client. Rows for the same base job ref as the new job, rows
    whose person is already in the new job, rows that never responded and
    rows outside the look-back window are dropped before scoring.
    """
    by_person: Dict[str, Dict[str, Any]] = {}
    for row in prior_rows:
        blob = json_load_safe(row.get("data"), {}) or {}
        if not isinstance(blob, dict):
            continue
        if not has_responded(blob.get("engage_status")):
            continue
        prior_ref = row.get("prior_jobdiva_id") or row.get("prior_key") or row.get("jobdiva_id")
        if base_job_ref(prior_ref) in new_job_base_refs or base_job_ref(row.get("prior_key")) in new_job_base_refs:
            continue
        screened_at = screened_at_from_blob(blob, row.get("updated_at"))
        if not screened_at or screened_at < cutoff:
            continue
        email = row.get("email") or blob.get("email")
        phone = row.get("phone") or blob.get("phone")
        keys = person_keys_for(email, phone, row.get("source"), row.get("candidate_id"))
        if keys & existing_person_keys:
            continue
        key = person_key(email, phone, row.get("source"), row.get("candidate_id"))
        candidate = {
            "person_key": key,
            "candidate_id": str(row.get("candidate_id") or ""),
            "source": str(row.get("source") or ""),
            "name": row.get("name") or blob.get("name") or "",
            "email": normalize_email(email) if not is_synthetic_email(normalize_email(email)) else "",
            "phone": str(phone or "").strip(),
            "headline": row.get("headline") or "",
            "location": row.get("location") or "",
            "prior_job_id": str(row.get("prior_job_id") or ""),
            "prior_jobdiva_id": str(prior_ref or ""),
            "prior_job_title": row.get("prior_title") or row.get("prior_enhanced_title") or "",
            "prior_customer_name": row.get("prior_customer_name") or "",
            "engage_status": str(blob.get("engage_status") or ""),
            "screen_result": screen_result_label(blob.get("engage_status"), blob.get("engage_hard_filter_status")),
            "engage_score": blob.get("engage_score"),
            "engage_total_score": blob.get("engage_total_score"),
            "screen_score_display": format_screen_score(blob.get("engage_score"), blob.get("engage_total_score")),
            "screened_at": screened_at,
            "_row": row,
            "_blob": blob,
        }
        prev = by_person.get(key)
        if prev is None or candidate["screened_at"] > prev["screened_at"]:
            by_person[key] = candidate

    # Score the most recently screened first when the pool exceeds the budget.
    pool = sorted(by_person.values(), key=lambda c: c["screened_at"], reverse=True)[: max(0, int(max_scored))]

    selected: List[Dict[str, Any]] = []
    for cand in pool:
        score = 0.0
        matched: List[str] = []
        missing: List[str] = []
        if scorer is not None and criteria is not None:
            try:
                result = scorer(build_scoring_payload(cand["_row"], cand["_blob"]), criteria) or {}
                score = float(result.get("score") or 0)
                matched = list(result.get("matched_skills") or [])
                missing = list(result.get("missing_skills") or [])
            except Exception as e:  # noqa: BLE001 — one bad row must not sink the list
                logger.warning("cross_submissions: scoring failed for %s: %s", cand.get("candidate_id"), e)
                score = 0.0
        cand["match_score"] = round(score, 1)
        cand["matched_skills"] = matched
        cand["missing_skills"] = missing
        if score >= float(min_score):
            selected.append(cand)

    selected.sort(key=lambda c: (c["match_score"], c["screened_at"]), reverse=True)
    for cand in selected:
        cand.pop("_row", None)
        cand.pop("_blob", None)
    return selected[: max(0, int(max_listed))]


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------
def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def load_job(cur, job_ref: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT job_id, jobdiva_id, title, enhanced_title, customer_name, recruiter_emails,
               parent_job_id, cross_submissions_checked_at
        FROM monitored_jobs
        WHERE job_id = %s OR jobdiva_id = %s
        ORDER BY (jobdiva_id = %s) DESC, created_at DESC
        LIMIT 1
        """,
        (job_ref, job_ref, job_ref),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def job_keys(job: Dict[str, Any], job_ref: str) -> List[str]:
    keys = [job_ref, job.get("job_id"), job.get("jobdiva_id"), job.get("parent_job_id")]
    return [str(k) for k in dict.fromkeys(k for k in keys if k)]


def job_base_refs(job: Dict[str, Any], job_ref: str) -> Set[str]:
    return {base_job_ref(k) for k in job_keys(job, job_ref) if base_job_ref(k)}


def claim_throttle(cur, job_id: str, *, force: bool, throttle_minutes: int) -> bool:
    """Atomic conditional UPDATE — only one worker per throttle window runs."""
    if force:
        cur.execute(
            "UPDATE monitored_jobs SET cross_submissions_checked_at = NOW() WHERE job_id = %s RETURNING job_id",
            (job_id,),
        )
    else:
        cur.execute(
            """
            UPDATE monitored_jobs
               SET cross_submissions_checked_at = NOW()
             WHERE job_id = %s
               AND (cross_submissions_checked_at IS NULL
                    OR cross_submissions_checked_at < NOW() - (%s * INTERVAL '1 minute'))
            RETURNING job_id
            """,
            (job_id, int(throttle_minutes)),
        )
    return cur.fetchone() is not None


def fetch_existing_person_keys(cur, keys: List[str]) -> Set[str]:
    """Persons already on the new job's candidate list (any status)."""
    if not keys:
        return set()
    cur.execute(
        """
        SELECT candidate_id, source, email, phone,
               data->>'email' AS data_email, data->>'phone' AS data_phone
        FROM sourced_candidates
        WHERE jobdiva_id = ANY(%s)
        """,
        (keys,),
    )
    out: Set[str] = set()
    for row in cur.fetchall():
        out |= person_keys_for(row.get("email") or row.get("data_email"), row.get("phone") or row.get("data_phone"), row.get("source"), row.get("candidate_id"))
    return out


def fetch_prior_rows(cur, *, lookback_days: int, exclude_keys: List[str]) -> List[Dict[str, Any]]:
    """Responded rows from other jobs inside the look-back window.

    The SQL window test is deliberately loose (ISO-string prefix compare on
    the JSONB timestamp OR the row's updated_at) — ``select_candidates``
    applies the precise check after parsing. ``dnc_stopped_at`` excludes
    anyone suppressed via Do-Not-Call.
    """
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    cur.execute(
        """
        SELECT sc.id, sc.jobdiva_id AS prior_key, sc.candidate_id, sc.source, sc.name, sc.email, sc.phone,
               sc.headline, sc.location, sc.resume_text, sc.data, sc.resume_match_percentage, sc.updated_at,
               mj.job_id AS prior_job_id, mj.jobdiva_id AS prior_jobdiva_id,
               mj.title AS prior_title, mj.enhanced_title AS prior_enhanced_title,
               mj.customer_name AS prior_customer_name
        FROM sourced_candidates sc
        LEFT JOIN LATERAL (
            SELECT m.job_id, m.jobdiva_id, m.title, m.enhanced_title, m.customer_name
            FROM monitored_jobs m
            WHERE m.jobdiva_id = sc.jobdiva_id OR m.job_id = sc.jobdiva_id
            ORDER BY (m.jobdiva_id = sc.jobdiva_id) DESC, m.created_at DESC
            LIMIT 1
        ) mj ON TRUE
        WHERE LOWER(COALESCE(sc.data->>'engage_status', '')) = ANY(%s)
          AND sc.dnc_stopped_at IS NULL
          AND NOT (sc.jobdiva_id = ANY(%s))
          AND (
                COALESCE(sc.data->>'engage_updated_at', '') >= %s
             OR COALESCE(sc.data->>'engage_completed_at', '') >= %s
             OR sc.updated_at >= (%s)::timestamp
          )
        """,
        (list(RESPONDED_STATUSES), exclude_keys or [""], cutoff_date, cutoff_date, cutoff_date),
    )
    return [dict(r) for r in cur.fetchall()]


def persist_new(cur, job: Dict[str, Any], job_ref: str, selected: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Insert one row per person; return only the rows this call inserted.

    UNIQUE(job_id, person_key) + ON CONFLICT DO NOTHING is the claim, so a
    person is emailed at most once per job even if two searches race.
    """
    job_id = str(job.get("job_id") or job_ref)
    jobdiva_id = str(job.get("jobdiva_id") or job_ref)
    inserted: List[Dict[str, Any]] = []
    for cand in selected:
        cur.execute(
            """
            INSERT INTO cross_submissions (
                job_id, jobdiva_id, person_key, candidate_id, source, name, email, phone, headline, location,
                prior_job_id, prior_jobdiva_id, prior_job_title, prior_customer_name,
                engage_status, screen_result, engage_score, engage_total_score, screened_at,
                match_score, matched_skills, missing_skills
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (job_id, person_key) DO NOTHING
            RETURNING id
            """,
            (
                job_id, jobdiva_id, cand["person_key"], cand["candidate_id"], cand["source"], cand["name"],
                cand["email"], cand["phone"], cand["headline"], cand["location"],
                cand["prior_job_id"], cand["prior_jobdiva_id"], cand["prior_job_title"], cand["prior_customer_name"],
                cand["engage_status"], cand["screen_result"], cand.get("engage_score"), cand.get("engage_total_score"),
                cand["screened_at"],
                cand["match_score"], json.dumps(cand.get("matched_skills") or []), json.dumps(cand.get("missing_skills") or []),
            ),
        )
        row = cur.fetchone()
        if row:
            cand["id"] = row["id"] if isinstance(row, dict) else row[0]
            inserted.append(cand)
    return inserted


def mark_notified(cur, ids: List[int]) -> None:
    if ids:
        cur.execute("UPDATE cross_submissions SET notified_at = NOW() WHERE id = ANY(%s)", (ids,))


def list_for_job(job_ref: str) -> List[Dict[str, Any]]:
    """Stored cross submissions for a job (newest/highest match first)."""
    conn = get_db_connection()
    try:
        with _dict_cursor(conn) as cur:
            job = load_job(cur, job_ref)
            keys = job_keys(job, job_ref) if job else [job_ref]
            cur.execute(
                """
                SELECT id, job_id, jobdiva_id, person_key, candidate_id, source, name, email, phone, headline, location,
                       prior_job_id, prior_jobdiva_id, prior_job_title, prior_customer_name,
                       engage_status, screen_result, engage_score, engage_total_score, screened_at,
                       match_score, matched_skills, missing_skills, created_at, notified_at
                FROM cross_submissions
                WHERE job_id = ANY(%s) OR jobdiva_id = ANY(%s)
                ORDER BY match_score DESC, screened_at DESC
                """,
                (keys, keys),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        for k in ("screened_at", "created_at", "notified_at"):
            if isinstance(r.get(k), datetime):
                r[k] = r[k].isoformat()
        for k in ("engage_score", "engage_total_score", "match_score"):
            if r.get(k) is not None:
                try:
                    r[k] = float(r[k])
                except (TypeError, ValueError):
                    pass
    return rows


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _parse_recruiter_emails(raw: Any) -> List[str]:
    if isinstance(raw, str):
        try:
            emails = json.loads(raw) if raw.strip().startswith("[") else [raw]
        except Exception:
            emails = [raw] if raw else []
    elif isinstance(raw, list):
        emails = raw
    else:
        emails = []
    return [str(e).strip().lower() for e in emails if e and str(e).strip()]


def run_for_job(
    job_ref: str,
    criteria: Any,
    scorer: Optional[Scorer],
    *,
    force: bool = False,
    send_email: bool = True,
    app_base_url: str = "",
) -> Dict[str, Any]:
    """Scan → score → persist → email. Synchronous; call via ``asyncio.to_thread``.

    Returns a summary dict; never raises (fail-open, see module docstring).
    """
    summary: Dict[str, Any] = {
        "job_ref": job_ref, "ran": False, "skipped_reason": None,
        "scanned": 0, "selected": 0, "new": 0, "emailed": False, "candidates": [],
    }
    if not CROSS_SUBMISSIONS_ENABLED and not force:
        summary["skipped_reason"] = "disabled"
        return summary
    conn = None
    try:
        conn = get_db_connection()
        with _dict_cursor(conn) as cur:
            cur.execute("SET LOCAL statement_timeout = '60000ms'")
            job = load_job(cur, job_ref)
            if not job:
                summary["skipped_reason"] = "job_not_found"
                conn.rollback()
                return summary
            if not claim_throttle(cur, str(job["job_id"]), force=force, throttle_minutes=CROSS_SUBMISSIONS_THROTTLE_MINUTES):
                summary["skipped_reason"] = "throttled"
                conn.rollback()
                return summary
            conn.commit()  # persist the claim before the (slow) scan
            cur.execute("SET LOCAL statement_timeout = '60000ms'")  # commit reset the LOCAL above

            keys = job_keys(job, job_ref)
            existing = fetch_existing_person_keys(cur, keys)
            prior = fetch_prior_rows(cur, lookback_days=CROSS_SUBMISSIONS_LOOKBACK_DAYS, exclude_keys=keys)
            summary["scanned"] = len(prior)
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(CROSS_SUBMISSIONS_LOOKBACK_DAYS))
            selected = select_candidates(
                prior,
                new_job_base_refs=job_base_refs(job, job_ref),
                existing_person_keys=existing,
                cutoff=cutoff,
                scorer=scorer,
                criteria=criteria,
            )
            summary["selected"] = len(selected)
            new_rows = persist_new(cur, job, job_ref, selected)
            conn.commit()
            summary["new"] = len(new_rows)
            summary["ran"] = True
            summary["candidates"] = [
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in c.items()} for c in selected
            ]

            if send_email and new_rows:
                from core.email import notify_cross_submissions  # local import: keeps email optional in tests

                recruiter_emails = _parse_recruiter_emails(job.get("recruiter_emails"))
                ok = notify_cross_submissions(
                    jobdiva_id=str(job.get("jobdiva_id") or job_ref),
                    job_id=str(job.get("job_id") or ""),
                    job_title=str(job.get("enhanced_title") or job.get("title") or ""),
                    customer_name=str(job.get("customer_name") or ""),
                    recruiter_emails=recruiter_emails,
                    candidates=new_rows,
                    lookback_days=int(CROSS_SUBMISSIONS_LOOKBACK_DAYS),
                    app_base_url=app_base_url or None,
                )
                summary["emailed"] = bool(ok)
                if ok:
                    mark_notified(cur, [c["id"] for c in new_rows if c.get("id")])
                    conn.commit()
        logger.info(
            "cross_submissions: job=%s scanned=%s selected=%s new=%s emailed=%s",
            job_ref, summary["scanned"], summary["selected"], summary["new"], summary["emailed"],
        )
        return summary
    except Exception as e:  # noqa: BLE001 — fail-open by design
        logger.error("cross_submissions: run failed for job %s: %s", job_ref, e, exc_info=True)
        summary["skipped_reason"] = f"error: {e}"
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return summary
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


async def run_for_job_async(job_ref: str, criteria: Any, scorer: Optional[Scorer], **kwargs: Any) -> Dict[str, Any]:
    return await asyncio.to_thread(run_for_job, job_ref, criteria, scorer, **kwargs)
