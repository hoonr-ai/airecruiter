"""What recruiters do in PAIR and where their time goes.

Two records, both keyed by the recruiter's (lower-cased) email:

  recruiter_actions      one row per meaningful action: a search run, a
                         Source & Launch click, a QA-review confirm, a
                         needs-contact answer, a Submit / Reject / Unreachable
                         decision, a recording played, a report exported …
                         Written SERVER-side wherever the endpoint knows the
                         user (record_action), and by the browser for what
                         only the page sees (POST /api/v1/activity/actions,
                         CLIENT_ACTIONS only).
  recruiter_active_time  ACTIVE time per (recruiter, surface, job, Eastern
                         day): the tab visible and the recruiter interacting
                         within the last 5 minutes (apps/web/lib/step-time.ts,
                         the same rule as the wizard's job_step_time, which is
                         dual-written here as surface "wizard_step_N").

Both are mirrored to New Relic (RecruiterAction / RecruiterActiveTime) with
the recruiter as ``recruiter`` = sha256(email)[:12] — the same pseudonymous id
the browser agent sets (apps/web/lib/newrelic.ts nrUserIdFor) — and never a
name, email, phone or candidate PII. The in-app read (fetch_activity, behind
GET /api/v1/admin/recruiter-activity) is the hash → recruiter lookup.

Everything here is telemetry: every write is fail-open and never raises.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
# A single report can carry at most this much (the client flushes ~every 2 min).
MAX_ACTIVE_MS_PER_REPORT = 10 * 60 * 1000
MAX_ATTR_JSON = 2000

# Where active time is measured. The wizard's steps come through
# routers/job_step_time (dual-written as wizard_step_1..5).
SURFACES = frozenset({
    "jobs_dashboard", "rankings", "assess_modal", "candidate_detail", "candidates",
    "launch_review", "needs_contact", "launch_report", "analytics", "recruiter_analytics",
    "admin_dashboard", "campaigns",
    *(f"wizard_step_{n}" for n in range(1, 6)),
})

# Actions the server records itself (record_action at the endpoint).
SERVER_ACTIONS = frozenset({
    "search_run", "launch_clicked", "launch_session_created", "review_confirmed",
    "needs_contact_answered", "retry_failed", "launch_cancelled",
    "feedback_submit", "feedback_reject", "feedback_unreachable", "feedback_other",
})
# Actions only the page sees (POST /api/v1/activity/actions).
CLIENT_ACTIONS = frozenset({
    "candidate_opened", "resume_viewed", "recording_played", "report_exported",
    "job_archived", "job_unarchived", "outreach_stopped", "job_new_version",
    "jobs_dashboard_filtered",
})
ACTIONS = SERVER_ACTIONS | CLIENT_ACTIONS

# Run by routers/jobs._ensure_monitored_jobs_schema at startup.
SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS recruiter_actions (
        id BIGSERIAL PRIMARY KEY,
        at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_email TEXT NOT NULL,
        action TEXT NOT NULL,
        job_key TEXT,
        candidate_id TEXT,
        session_id TEXT,
        source TEXT NOT NULL DEFAULT 'server',
        attrs JSONB
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recruiter_actions_at ON recruiter_actions (at)",
    "CREATE INDEX IF NOT EXISTS idx_recruiter_actions_user_at ON recruiter_actions (user_email, at)",
    """CREATE TABLE IF NOT EXISTS recruiter_active_time (
        user_email TEXT NOT NULL,
        surface TEXT NOT NULL,
        job_key TEXT NOT NULL DEFAULT '',
        day DATE NOT NULL,
        active_ms BIGINT NOT NULL DEFAULT 0,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (user_email, surface, job_key, day)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_recruiter_active_time_day ON recruiter_active_time (day)",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def recruiter_hash(email: Any) -> Optional[str]:
    """sha256(lower(email))[:12] — apps/web/lib/newrelic.ts nrUserIdFor."""
    e = str(email or "").strip().lower()
    return hashlib.sha256(e.encode("utf-8")).hexdigest()[:12] if e else None


def _email(value: Any) -> str:
    e = str(value or "").strip().lower()
    return e if "@" in e else ""


def clamp_active_ms(value: Any) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(ms, MAX_ACTIVE_MS_PER_REPORT))


def eastern_day(at: Optional[datetime.datetime] = None) -> datetime.date:
    at = at or datetime.datetime.now(datetime.timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=datetime.timezone.utc)
    return at.astimezone(EASTERN).date()


def _clean_attrs(attrs: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Numbers, booleans and short enum-like strings only: no free text, no PII."""
    if not attrs:
        return None
    out: Dict[str, Any] = {}
    for k, v in list(attrs.items())[:20]:
        key = str(k)[:40]
        if any(w in key.lower() for w in ("name", "email", "phone", "resume", "address", "linkedin")):
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, str) and len(v) <= 64 and "@" not in v:
            out[key] = v
    return out or None


def _connect():
    from core.db import get_db_connection

    return get_db_connection()


def _nr(event: str, params: Dict[str, Any]) -> None:
    try:
        from core.newrelic import record_custom_event

        record_custom_event(event, {k: v for k, v in params.items() if v is not None})
    except Exception:  # noqa: BLE001 — telemetry never fails the caller
        pass


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def record_action(
    user_email: Any,
    action: str,
    *,
    job_key: Any = None,
    candidate_id: Any = None,
    session_id: Any = None,
    attrs: Optional[Mapping[str, Any]] = None,
    source: str = "server",
    at: Optional[datetime.datetime] = None,
) -> bool:
    """One action row (+ RecruiterAction NR event). Sync; never raises."""
    email = _email(user_email)
    if not email or action not in ACTIONS:
        return False
    clean = _clean_attrs(attrs)
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '3000ms'")
                cur.execute(
                    "INSERT INTO recruiter_actions (at, user_email, action, job_key, candidate_id, session_id, source, attrs)"
                    " VALUES (COALESCE(%s, NOW()), %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (at, email, action, str(job_key)[:64] if job_key else None,
                     str(candidate_id)[:200] if candidate_id else None,
                     str(session_id)[:64] if session_id else None, source,
                     json.dumps(clean)[:MAX_ATTR_JSON] if clean else None),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[RecruiterActivity] could not record {action}: {exc}")
        return False
    _nr("RecruiterAction", {
        "recruiter": recruiter_hash(email), "action": action, "source": source,
        "job_id": str(job_key) if job_key else None, **(clean or {}),
    })
    return True


def record_action_soon(user_email: Any, action: str, **kwargs: Any) -> None:
    """record_action off the event loop, fire-and-forget (request handlers)."""
    import asyncio

    try:
        from core.tasks import spawn

        spawn(asyncio.to_thread(record_action, user_email, action, **kwargs))
    except Exception as exc:  # noqa: BLE001 — no running loop: record inline
        logger.debug(f"[RecruiterActivity] spawn failed ({exc}); recording inline")
        record_action(user_email, action, **kwargs)


_ACTIVE_UPSERT_SQL = """
INSERT INTO recruiter_active_time (user_email, surface, job_key, day, active_ms)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (user_email, surface, job_key, day) DO UPDATE
SET active_ms = recruiter_active_time.active_ms + EXCLUDED.active_ms,
    last_seen_at = NOW()
"""


def record_active_time(conn, user_email: Any, surface: str, job_key: Any, active_ms: Any) -> bool:
    """Add active ms for (recruiter, surface, job, today ET) on ``conn``
    (committed). Raises on DB errors — the endpoint decides how to degrade.
    False for an unknown surface or a non-person identity."""
    email = _email(user_email)
    if not email or surface not in SURFACES:
        return False
    ms = clamp_active_ms(active_ms)
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '2000ms'")
        cur.execute("SET LOCAL statement_timeout = '5000ms'")
        cur.execute(_ACTIVE_UPSERT_SQL, (email, surface, str(job_key or "")[:64], eastern_day(), ms))
    conn.commit()
    if ms > 0:
        _nr("RecruiterActiveTime", {
            "recruiter": recruiter_hash(email), "surface": surface, "active_ms": ms,
            "job_id": str(job_key) if job_key else None,
        })
    return True


# ---------------------------------------------------------------------------
# Read (GET /api/v1/admin/recruiter-activity)
# ---------------------------------------------------------------------------
def _ms_to_minutes(ms: Any) -> float:
    return round(float(ms or 0) / 60000.0, 1)


def _median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 1)


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    row = cur.fetchone()
    return bool(row and row[0])


def fetch_activity(
    conn,
    *,
    start: datetime.date,
    end: datetime.date,
    emails: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Per recruiter, between two Eastern days inclusive:

      active_minutes_total / by_surface / by_day   recruiter_active_time
      actions                                      counts by action
      decisions / decision_latency_median_minutes  Submit / Reject / Unreachable
                                                   on a PASSED launched person:
                                                   pass (first_completed_at) →
                                                   feedback_at, by submitted_by
      launches / searches / searches_per_launch    from the action log
      waiting_on_pair_minutes                      launch-session machine time
                                                   (click → first interview,
                                                   minus the recruiter's own
                                                   waits) on sessions they
                                                   started, when sessions exist
      recruiter_hash                               the New Relic id
    """
    scope = sorted({_email(e) for e in emails if _email(e)}) if emails is not None else None
    lo = datetime.datetime.combine(start, datetime.time.min, tzinfo=EASTERN)
    hi = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, tzinfo=EASTERN)
    people: Dict[str, Dict[str, Any]] = {}

    def person(email: str) -> Dict[str, Any]:
        return people.setdefault(email, {
            "email": email, "recruiter_hash": recruiter_hash(email),
            "active_ms": 0, "by_surface": {}, "by_day": {}, "actions": {},
            "decision_latencies": [], "waiting_ms": 0, "sessions": 0,
        })

    scope_sql = "AND user_email = ANY(%s)" if scope is not None else ""
    scope_params: List[Any] = [scope] if scope is not None else []
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '15000ms'")
        if _table_exists(cur, "recruiter_active_time"):
            cur.execute(
                f"SELECT user_email, surface, day, SUM(active_ms) FROM recruiter_active_time"
                f" WHERE day BETWEEN %s AND %s {scope_sql} GROUP BY 1, 2, 3",
                [start, end, *scope_params],
            )
            for email, surface, day, ms in cur.fetchall():
                p = person(email)
                p["active_ms"] += int(ms or 0)
                p["by_surface"][surface] = p["by_surface"].get(surface, 0) + int(ms or 0)
                key = day.isoformat()
                p["by_day"][key] = p["by_day"].get(key, 0) + int(ms or 0)
        if _table_exists(cur, "recruiter_actions"):
            cur.execute(
                f"SELECT user_email, action, COUNT(*) FROM recruiter_actions"
                f" WHERE at >= %s AND at < %s {scope_sql} GROUP BY 1, 2",
                [lo, hi, *scope_params],
            )
            for email, action, n in cur.fetchall():
                person(email)["actions"][action] = int(n)
        # Decision latency: the recruiter's decision on a person PAIR passed.
        # feedback_at / first_completed_at are ISO text; only ISO-looking
        # values are cast, so one malformed row cannot fail the read.
        iso = "'^[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}'"
        by = "LOWER(BTRIM(sc.data->>'submitted_by'))"
        cur.execute(
            f"""
            SELECT {by},
                   EXTRACT(EPOCH FROM (
                       (sc.data->>'feedback_at')::timestamptz - (sc.data->>'first_completed_at')::timestamptz
                   )) / 60.0
            FROM sourced_candidates sc
            WHERE COALESCE(sc.data->>'submitted_by', '') <> ''
              AND COALESCE(sc.data->>'feedback_at', '') ~ {iso}
              AND COALESCE(sc.data->>'first_completed_at', '') ~ {iso}
              AND LOWER(COALESCE(sc.data->>'engage_hard_filter_status', '')) IN ('pass', 'passed')
              AND (sc.data->>'feedback_at')::timestamptz >= %s
              AND (sc.data->>'feedback_at')::timestamptz < %s
              {"AND " + by + " = ANY(%s)" if scope is not None else ""}
            """,
            [lo, hi, *scope_params],
        )
        for email, minutes in cur.fetchall():
            if email and minutes is not None and float(minutes) >= 0:
                person(email)["decision_latencies"].append(float(minutes))
        if _table_exists(cur, "launch_sessions"):
            cur.execute(
                f"""
                SELECT LOWER(requested_by),
                       EXTRACT(EPOCH FROM (first_interview_created_at - clicked_at)) * 1000
                         - COALESCE(human_wait_ms, 0)
                FROM launch_sessions
                WHERE clicked_at >= %s AND clicked_at < %s
                  AND first_interview_created_at IS NOT NULL
                  {"AND LOWER(requested_by) = ANY(%s)" if scope is not None else ""}
                """,
                [lo, hi, *scope_params],
            )
            for email, ms in cur.fetchall():
                if email and ms is not None and float(ms) >= 0:
                    p = person(email)
                    p["waiting_ms"] += int(ms)
                    p["sessions"] += 1

    recruiters = []
    for p in people.values():
        acts = p["actions"]
        launches = int(acts.get("launch_clicked", 0))
        searches = int(acts.get("search_run", 0))
        decisions = sum(int(acts.get(a, 0)) for a in (
            "feedback_submit", "feedback_reject", "feedback_unreachable", "feedback_other"))
        recruiters.append({
            "email": p["email"],
            "recruiter_hash": p["recruiter_hash"],
            "active_minutes_total": _ms_to_minutes(p["active_ms"]),
            "active_minutes_by_surface": {k: _ms_to_minutes(v) for k, v in sorted(
                p["by_surface"].items(), key=lambda kv: -kv[1])},
            "active_minutes_by_day": {k: _ms_to_minutes(v) for k, v in sorted(p["by_day"].items())},
            "days_active": sum(1 for v in p["by_day"].values() if v > 0),
            "actions": dict(sorted(acts.items())),
            "searches": searches,
            "launches": launches,
            "searches_per_launch": round(searches / launches, 1) if launches else None,
            "decisions": decisions,
            "decision_latency_median_minutes": _median(p["decision_latencies"]),
            "decisions_timed": len(p["decision_latencies"]),
            "waiting_on_pair_minutes": _ms_to_minutes(p["waiting_ms"]) if p["sessions"] else None,
        })
    recruiters.sort(key=lambda r: (-r["active_minutes_total"], r["email"]))
    totals = {
        "recruiters": len(recruiters),
        "active_minutes_total": round(sum(r["active_minutes_total"] for r in recruiters), 1),
        "searches": sum(r["searches"] for r in recruiters),
        "launches": sum(r["launches"] for r in recruiters),
        "decisions": sum(r["decisions"] for r in recruiters),
        "decision_latency_median_minutes": _median(
            [m for p in people.values() for m in p["decision_latencies"]]),
    }
    return {"start_date": start.isoformat(), "end_date": end.isoformat(), "recruiters": recruiters, "totals": totals}
