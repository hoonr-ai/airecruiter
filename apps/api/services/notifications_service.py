"""In-app notification creation, alongside (never instead of) this app's
existing email notifications. Three types today:

- candidate_passed (create_candidate_passed_notifications): a candidate's
  interview result. Independent of the pass-email trigger in
  routers/engagement.py (`_check_and_fire_candidate_passed_notification`):
  that function gates on the stricter, email-specific hard-filter check,
  while this one gates on `engage_status.format_engage_status(...) ==
  "Pass"` — the same rule Rankings, the launch report and admin analytics
  use — so "you got notified in-app" always agrees with "this candidate
  shows as Pass everywhere else in the product." Candidate-level:
  candidate_id is set, and the unique index on (recipient_email,
  candidate_id, jobdiva_id, type) dedupes a repeat trigger for the same
  candidate/job/recipient.

- pair_launched / pair_inactive (create_pair_launched_notification /
  create_pair_inactive_notification): job-level events with no single
  candidate (a launch has a candidate COUNT; an inactivity transition has
  none at all) — candidate_id is NULL, so the index above never applies to
  them. Mirror core/email.py's notify_pair_launched / notify_pair_inactive,
  fired from the same call sites, but run INDEPENDENTLY of whether that
  email send succeeds — a transient email-provider failure shouldn't also
  silently cost the in-app notification. Each has its own dedicated partial
  unique index instead (see routers/notifications.py's schema init and the
  _CONFLICT_* constants below), because the two events have different
  "worth a fresh notification" semantics: a relaunch is always new and
  notable, while an inactivity transition should show at most one active
  notification until the user clears it.

One row is written per recruiter in the job's `recruiter_emails`, via
`INSERT ... ON CONFLICT ... DO NOTHING RETURNING id` so concurrent
deliveries — webhook retries, racing uvicorn workers, or (for the job-level
types) an independent in-app/email retry — can't double-insert.

Every public function here does its DB work inside `asyncio.to_thread`:
these are all `async def` so callers (the webhook handler, the launch flow,
the inactivity poll cycle) can simply `await` them, but psycopg2 is
synchronous — calling it directly from an `async def` body blocks the event
loop for every other request on this worker for the duration of the
round-trip(s).
"""
from typing import Any, Dict, List, Optional
import asyncio
import json
import logging

import psycopg2.extras

from core.db import get_db_connection
from services.engage_status import (
    format_engage_status,
    hf_display_from_payload,
    score_from_payload,
    engage_display_sql,
)

logger = logging.getLogger(__name__)

# Candidate-level dedup: one row per (recipient, candidate, job, type) ever.
_CONFLICT_CANDIDATE_PASSED = "ON CONFLICT (recipient_email, candidate_id, jobdiva_id, type) DO NOTHING"
# Job-level dedup, each matching the partial unique index of the same name in
# routers/notifications.py's init_notifications_schema_sync — the column/
# expression list and WHERE clause must match an existing index exactly for
# Postgres to accept it as the ON CONFLICT arbiter. Both are fully static
# (no runtime values spliced in), so this is as safe to embed as the rest of
# the query string.
_CONFLICT_PAIR_INACTIVE = (
    "ON CONFLICT (recipient_email, jobdiva_id) "
    "WHERE type = 'pair_inactive' AND cleared_at IS NULL DO NOTHING"
)
_CONFLICT_PAIR_LAUNCHED = (
    "ON CONFLICT (recipient_email, jobdiva_id, date_trunc('minute', created_at)) "
    "WHERE type = 'pair_launched' DO NOTHING"
)


def _parse_recruiter_emails(val: Any) -> List[str]:
    """Same tolerant JSON-string/list/comma-list parsing as engagement.py's
    _parse_json_list, plus lowercasing + de-dup for use as notification
    recipients."""
    if isinstance(val, list):
        raw = val
    elif isinstance(val, str):
        try:
            parsed = json.loads(val)
            raw = parsed if isinstance(parsed, list) else []
        except Exception:
            raw = [e.strip() for e in val.split(",") if e.strip()]
    else:
        raw = []
    seen = set()
    emails: List[str] = []
    for e in raw:
        email = str(e or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def _insert_notification(
    cur,
    *,
    notification_type: str,
    conflict_clause: str,
    recipient_email: str,
    job_id: Optional[str],
    jobdiva_id: Optional[str],
    candidate_id: Optional[str],
    title: str,
    body: Optional[str],
    score: Optional[float],
) -> bool:
    """candidate_id is None for job-level event types (pair_launched,
    pair_inactive). Those pass their own type-specific `conflict_clause`
    (the _CONFLICT_PAIR_* constants above) rather than the candidate-level
    one, because the unique index behind the candidate-level clause keys on
    candidate_id — and NULL never equals another NULL there, so it would
    silently stop deduping at all for these two types."""
    cur.execute(
        f"""
        INSERT INTO notifications
            (recipient_email, type, job_id, jobdiva_id, candidate_id, title, body, score)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        {conflict_clause}
        RETURNING id
        """,
        (recipient_email, notification_type, job_id, jobdiva_id, candidate_id, title, body, score),
    )
    row = cur.fetchone()
    return row is not None


def _fetch_job_and_candidate(cur, job_id: str, candidate_id: str):
    cur.execute(
        """
        SELECT job_id, title, enhanced_title, recruiter_emails, jobdiva_id
        FROM monitored_jobs
        WHERE job_id = %s OR jobdiva_id = %s
        ORDER BY (job_id ~ '^[0-9]+$') DESC, created_at DESC
        LIMIT 1
        """,
        (job_id, job_id),
    )
    job_row = cur.fetchone()

    cur.execute(
        """
        SELECT name, data
        FROM sourced_candidates
        WHERE candidate_id = %s AND jobdiva_id = %s
        LIMIT 1
        """,
        (candidate_id, job_row["jobdiva_id"] if job_row else job_id),
    )
    cand_row = cur.fetchone()
    return job_row, cand_row


def _resolve_job_title(job_row: Dict[str, Any], job_id: str) -> str:
    job_title = job_row.get("enhanced_title") or job_row.get("title")
    if not job_title:
        logger.debug("notification_missing_job_title job=%s", job_id)
        return "this job"
    return job_title


def _create_candidate_passed_notifications_sync(job_id: str, candidate_id: str, score: Optional[float]) -> None:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            job_row, cand_row = _fetch_job_and_candidate(cur, job_id, candidate_id)
            if not job_row or not cand_row:
                return

            recruiter_emails = _parse_recruiter_emails(job_row.get("recruiter_emails"))
            if not recruiter_emails:
                return

            candidate_name = cand_row.get("name") or "A candidate"
            job_title = _resolve_job_title(job_row, job_id)
            title = f"{candidate_name} passed the interview for {job_title}"
            body = f"Score: {score:.0f}/100" if score is not None else None
            jobdiva_id = job_row.get("jobdiva_id") or job_id
            if not job_row.get("jobdiva_id"):
                logger.debug("notification_missing_jobdiva_id job=%s falling_back_to_job_id", job_id)
            app_job_id = job_row.get("job_id") or job_id

            any_inserted = False
            for recipient in recruiter_emails:
                if _insert_notification(
                    cur,
                    notification_type="candidate_passed",
                    conflict_clause=_CONFLICT_CANDIDATE_PASSED,
                    recipient_email=recipient,
                    job_id=app_job_id,
                    jobdiva_id=jobdiva_id,
                    candidate_id=candidate_id,
                    title=title,
                    body=body,
                    score=score,
                ):
                    any_inserted = True
            conn.commit()
            if any_inserted:
                logger.info(
                    "notification_created candidate=%s job=%s recipients=%d",
                    candidate_id, jobdiva_id, len(recruiter_emails),
                )
    finally:
        conn.close()


async def create_candidate_passed_notifications(
    job_id: str,
    candidate_id: str,
    detail_payload: Dict[str, Any],
) -> None:
    """Fire in-app Pass notifications for every recruiter on `job_id`.

    Safe to call from `asyncio.create_task` fire-and-forget: all failures are
    caught and logged, never raised back into the webhook handler.
    """
    try:
        if not job_id or not candidate_id:
            return

        interview_block = detail_payload.get("interview", {}) or {}
        engage_status = interview_block.get("status")
        payload_for_score = {
            "engage_score": interview_block.get("candidate_score"),
            "candidate_score": interview_block.get("candidate_score"),
        }
        score = score_from_payload(payload_for_score)
        hf_display = hf_display_from_payload(
            {"hard_filter_status": interview_block.get("hard_filter_status")}
        )

        if format_engage_status(engage_status, score, hf_display) != "Pass":
            return

        await asyncio.to_thread(_create_candidate_passed_notifications_sync, job_id, candidate_id, score)
    except Exception as e:  # noqa: BLE001 - must never raise into the webhook handler
        logger.error(
            "create_candidate_passed_notifications_failed candidate=%s job=%s: %s",
            candidate_id, job_id, e, exc_info=True,
        )


# Reconciliation lookback: catch anything the webhook-triggered path missed
# (a crashed worker, a status flip outside the webhook) without re-scanning
# the entire table every 10 minutes.
RECONCILIATION_LOOKBACK_HOURS = 24
# Upper bound per run so a backlog (e.g. after downtime) can't make one
# reconciliation tick scan/insert unboundedly; logged if hit so a real
# backlog is visible instead of silently dropped.
RECONCILIATION_MAX_ROWS = 1000


def _reconcile_missed_pass_notifications_sync() -> None:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            status_sql = engage_display_sql("sc.data")
            cur.execute(
                f"""
                SELECT sc.candidate_id, sc.name, sc.data,
                       mj.job_id, mj.jobdiva_id, mj.title, mj.enhanced_title,
                       mj.recruiter_emails
                FROM sourced_candidates sc
                JOIN monitored_jobs mj ON mj.jobdiva_id = sc.jobdiva_id
                WHERE {status_sql} = 'Pass'
                  AND sc.updated_at >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY sc.updated_at DESC
                LIMIT %s
                """,
                (RECONCILIATION_LOOKBACK_HOURS, RECONCILIATION_MAX_ROWS),
            )
            rows = cur.fetchall()
            if len(rows) >= RECONCILIATION_MAX_ROWS:
                logger.warning(
                    "notifications_reconciliation hit RECONCILIATION_MAX_ROWS=%d; "
                    "a backlog may exist beyond this run's window",
                    RECONCILIATION_MAX_ROWS,
                )

            backfilled = 0
            for row in rows:
                recruiter_emails = _parse_recruiter_emails(row.get("recruiter_emails"))
                if not recruiter_emails:
                    continue
                score = score_from_payload(row.get("data") or {})
                candidate_name = row.get("name") or "A candidate"
                job_title = _resolve_job_title(row, row.get("job_id") or "")
                title = f"{candidate_name} passed the interview for {job_title}"
                body = f"Score: {score:.0f}/100" if score is not None else None
                for recipient in recruiter_emails:
                    if _insert_notification(
                        cur,
                        notification_type="candidate_passed",
                        conflict_clause=_CONFLICT_CANDIDATE_PASSED,
                        recipient_email=recipient,
                        job_id=row.get("job_id"),
                        jobdiva_id=row.get("jobdiva_id"),
                        candidate_id=row.get("candidate_id"),
                        title=title,
                        body=body,
                        score=score,
                    ):
                        backfilled += 1
            conn.commit()
            if backfilled:
                logger.info("notifications_reconciliation backfilled=%d", backfilled)
    finally:
        conn.close()


async def reconcile_missed_pass_notifications() -> None:
    """Safety-net scheduler job: backfill Pass notifications the webhook path
    missed. Uses `engage_display_sql` (the SQL twin of `format_engage_status`)
    so this can never disagree with the webhook trigger or with what Rankings
    displays as Pass.

    Deliberately does NOT pre-filter candidates that already have a
    `notifications` row: that check was keyed on (candidate_id, jobdiva_id)
    only, blind to recipient_email, so a recruiter added to a job's
    recruiter_emails AFTER the first notification fired would never get
    backfilled. Every Pass candidate in the lookback window is re-evaluated
    each run; per-recipient dedup is the table's own unique index via
    `INSERT ... ON CONFLICT DO NOTHING` in `_insert_notification` — an
    index-only no-op for rows already notified, so this stays cheap.
    """
    try:
        await asyncio.to_thread(_reconcile_missed_pass_notifications_sync)
    except Exception as e:  # noqa: BLE001 - scheduler job must never crash the loop
        logger.error("reconcile_missed_pass_notifications_failed: %s", e, exc_info=True)


def _create_pair_launched_notification_sync(
    job_id: str, jobdiva_id: str, job_title: str, candidate_count: int, recipients: List[str]
) -> None:
    plural = "s" if candidate_count != 1 else ""
    title = f"PAIR launched for {job_title or 'this job'} — {candidate_count} candidate{plural}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for recipient in recipients:
                _insert_notification(
                    cur,
                    notification_type="pair_launched",
                    conflict_clause=_CONFLICT_PAIR_LAUNCHED,
                    recipient_email=recipient,
                    job_id=job_id,
                    jobdiva_id=jobdiva_id,
                    candidate_id=None,
                    title=title,
                    body=None,
                    score=None,
                )
            conn.commit()
    finally:
        conn.close()


async def create_pair_launched_notification(
    *,
    job_id: str,
    jobdiva_id: str,
    job_title: str,
    candidate_count: int,
    recruiter_emails: List[str],
) -> None:
    """Fire in-app "PAIR launched" notifications for every recruiter on the
    job, alongside (independently of) the existing email
    (routers/engagement.py's _send_pair_launch_email, which calls
    core.email.notify_pair_launched — call this regardless of whether that
    email send succeeded, it's a separate channel).

    Job-level — candidate_id is NULL, since a launch is a batch event with a
    candidate COUNT, never a single candidate. Dedup is the
    uniq_notifications_pair_launched_per_minute partial index (see
    routers/notifications.py): a relaunch is a new, separately-worth-
    notifying event, so only same-recipient/job/minute duplicates — a retry
    or racing workers handling what's really one launch — are collapsed.
    """
    try:
        if not job_id or not jobdiva_id or not recruiter_emails:
            return
        recipients = _parse_recruiter_emails(recruiter_emails)
        if not recipients:
            return
        await asyncio.to_thread(
            _create_pair_launched_notification_sync, job_id, jobdiva_id, job_title, candidate_count, recipients
        )
    except Exception as e:  # noqa: BLE001 - must never raise into the launch flow
        logger.error("create_pair_launched_notification_failed job=%s: %s", job_id, e, exc_info=True)


def _create_pair_inactive_notification_sync(
    job_id: str, jobdiva_id: str, job_title: str, recipients: List[str]
) -> None:
    title = f"PAIR is now inactive for {job_title or 'this job'}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for recipient in recipients:
                _insert_notification(
                    cur,
                    notification_type="pair_inactive",
                    conflict_clause=_CONFLICT_PAIR_INACTIVE,
                    recipient_email=recipient,
                    job_id=job_id,
                    jobdiva_id=jobdiva_id,
                    candidate_id=None,
                    title=title,
                    body=None,
                    score=None,
                )
            conn.commit()
    finally:
        conn.close()


async def create_pair_inactive_notification(
    *,
    job_id: str,
    jobdiva_id: str,
    job_title: str,
    recruiter_emails: List[str],
) -> None:
    """Fire in-app "PAIR inactive" notifications, alongside (independently
    of) the existing email (main.py's _fire_pair_inactive_notification,
    which calls core.email.notify_pair_inactive — call this regardless of
    whether that email send succeeded or the claim gets released for retry,
    it's a separate channel).

    Job-level, like create_pair_launched_notification, but a different dedup
    shape: the uniq_notifications_pair_inactive_active partial index (see
    routers/notifications.py) allows at most one ACTIVE (uncleared)
    pair_inactive notification per recipient/job. That — not call-site
    ordering — is what makes it safe to call this unconditionally even when
    the email keeps failing and _fire_pair_inactive_notification's claim
    gets released and retried: every retry's insert attempt for the same
    still-active notification is a no-op.
    """
    try:
        if not job_id or not jobdiva_id or not recruiter_emails:
            return
        recipients = _parse_recruiter_emails(recruiter_emails)
        if not recipients:
            return
        await asyncio.to_thread(_create_pair_inactive_notification_sync, job_id, jobdiva_id, job_title, recipients)
    except Exception as e:  # noqa: BLE001 - must never raise into the poll cycle
        logger.error("create_pair_inactive_notification_failed job=%s: %s", job_id, e, exc_info=True)
