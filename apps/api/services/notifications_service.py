"""In-app notification creation for candidates who passed their interview.

Independent of (and in addition to) the pass-email trigger in
routers/engagement.py (`_check_and_fire_candidate_passed_notification`): that
function gates on the stricter, email-specific hard-filter check, while this
one gates on `engage_status.format_engage_status(...) == "Pass"` — the same
rule Rankings, the launch report and admin analytics use — so "you got
notified in-app" always agrees with "this candidate shows as Pass everywhere
else in the product."

One row is written per recruiter in the job's `recruiter_emails`. Dedup is a
unique index on (recipient_email, candidate_id, jobdiva_id, type), claimed
atomically via `INSERT ... ON CONFLICT DO NOTHING RETURNING id` so concurrent
webhook deliveries across uvicorn workers can't double-insert.
"""
from typing import Any, Dict, List, Optional
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
    recipient_email: str,
    job_id: Optional[str],
    jobdiva_id: Optional[str],
    candidate_id: str,
    title: str,
    body: Optional[str],
    score: Optional[float],
) -> bool:
    cur.execute(
        """
        INSERT INTO notifications
            (recipient_email, type, job_id, jobdiva_id, candidate_id, title, body, score)
        VALUES (%s, 'candidate_passed', %s, %s, %s, %s, %s, %s)
        ON CONFLICT (recipient_email, candidate_id, jobdiva_id, type) DO NOTHING
        RETURNING id
        """,
        (recipient_email, job_id, jobdiva_id, candidate_id, title, body, score),
    )
    return cur.fetchone() is not None


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
                job_title = job_row.get("enhanced_title") or job_row.get("title") or "this job"
                title = f"{candidate_name} passed the interview for {job_title}"
                body = f"Score: {score:.0f}/100" if score is not None else None
                jobdiva_id = job_row.get("jobdiva_id") or job_id
                app_job_id = job_row.get("job_id") or job_id

                any_inserted = False
                for recipient in recruiter_emails:
                    if _insert_notification(
                        cur,
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
                    job_title = row.get("enhanced_title") or row.get("title") or "this job"
                    title = f"{candidate_name} passed the interview for {job_title}"
                    body = f"Score: {score:.0f}/100" if score is not None else None
                    for recipient in recruiter_emails:
                        if _insert_notification(
                            cur,
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
    except Exception as e:  # noqa: BLE001 - scheduler job must never crash the loop
        logger.error("reconcile_missed_pass_notifications_failed: %s", e, exc_info=True)
