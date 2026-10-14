"""In-app notification creation, alongside (never instead of) this app's
existing email notifications. Nine types today:

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

- candidate_rejected (create_candidate_rejected_notifications): fired from
  routers/candidates.py's save_candidate_feedback on feedback_type ==
  "Reject" (not "Submit" or "Unreachable"), to every OTHER recruiter on the
  job (the rejecter is excluded from their own notification). Uses
  _create_candidate_feedback_notifications_sync and candidate_passed's
  conflict clause/index — `type` keeps the two dedup spaces independent, so
  the same candidate can be Passed and (later) Rejected without either
  blocking the other, but a re-rejection (feedback can be edited and
  resaved — see save_candidate_feedback's docstring) only ever notifies once
  per (recipient, candidate, job, type). There is no generic
  candidate_submitted counterpart — see
  create_candidate_rejected_notifications's own docstring for why.

- pair_launched / pair_inactive (create_pair_launched_notification /
  create_pair_inactive_notification): job-level events with no single
  candidate (a launch has a candidate COUNT; an inactivity transition has
  none at all) — candidate_id is NULL, so the index above never applies to
  them. Mirror core/email.py's notify_pair_launched / notify_pair_inactive,
  fired from the same call sites, but run INDEPENDENTLY of whether that
  email send succeeds — a transient email-provider failure shouldn't also
  silently cost the in-app notification. pair_inactive dedupes via a
  dedicated partial unique index (see routers/notifications.py's schema init
  and _CONFLICT_PAIR_INACTIVE below) — a real dedup GUARANTEE, enforced by
  Postgres itself; pair_launched dedupes via a plain pre-insert existence
  check instead (see _job_level_duplicate_in_last_minute below), because
  its natural dedup key involves a time window, and Postgres can't build a
  UNIQUE index on that in a way it will accept (see that function's
  docstring for why) — this is a check-then-insert race, not a guarantee,
  though a plain (non-unique) index still backs the check's own lookup for
  performance (see routers/notifications.py). The two events have
  different "worth a fresh notification" semantics: a relaunch is always
  new and notable, while an inactivity transition should show at most one
  active notification until the user clears it.

- recruiter_assigned (create_recruiter_assigned_notifications): fired from
  routers/jobs.py's save_job_draft / save_job_to_monitored_jobs_only
  (the job wizard's save endpoints) whenever a diff of the job's
  recruiter_emails before vs. after that save shows an email present now
  that wasn't before — covers both "assigned to a brand-new requisition"
  (old list is empty/nonexistent) and "added to an existing active search."
  Job-level like pair_launched/pair_inactive (candidate_id NULL), and
  shares pair_launched's dedup shape (_job_level_duplicate_in_last_minute)
  for the same reason: the wizard autosaves frequently, so a diff-driven
  trigger needs a race guard, not an "only ever once" index — being
  removed and later re-added to the same job is a legitimately new,
  separately-worth-notifying event.

- zero_match_warning (create_zero_match_notification): fired from
  routers/candidates.py's /candidates/search endpoint when a FULL search's
  (not a cheap "sample" probe's) summary event reports total_candidates ==
  0. The ONLY type here with a single, specific recipient — the recruiter
  who ran that search — rather than a job's whole recruiter_emails: a
  search finishing empty is that recruiter's own signal to tweak
  keywords/filters/rate bands, not something every co-recruiter on the job
  needs to hear about. Job-level (candidate_id NULL — there is, by
  definition, no candidate), same dedup shape as pair_launched/
  recruiter_assigned (_job_level_duplicate_in_last_minute): re-running a
  tweaked search later and getting 0 again is a fresh, separately-worth-
  notifying result.

- daily_digest (create_daily_digest_notifications): the one type NOT fired
  from a request/webhook — main.py schedules it once a day (19:00
  Asia/Kolkata; see that function's docstring for why that IST time, not a
  US one, is what's configured). One row per recruiter with at least one
  unread candidate_passed notification, summarizing how many and across how
  many jobs. Spans every job a recipient has unread Pass notifications on,
  so unlike every other type here it has no single job to attach to —
  jobdiva_id is NULL (a schema change from every earlier type, which only
  ever had candidate_id go NULL; see routers/notifications.py's schema
  init). Dedup is a daily-scale version of _job_level_duplicate_in_last_minute
  (_recipient_already_digested_recently, by recipient_email alone since
  there's no jobdiva_id to key on).

- candidate_internal_review_requested / candidate_externally_submitted
  (create_candidate_internal_review_requested_notification /
  create_candidate_externally_submitted_notification): the internal-
  submission review loop. A recruiter can submit a candidate "internally"
  (to a named manager for review, rather than out to JobDiva) via
  routers/candidates.py's save_candidate_feedback
  (submission_type == "internal"); candidate_internal_review_requested
  tells that manager, by name, that a review is waiting — the manager
  usually isn't one of the job's other recruiters, so nothing else would
  have reached them, and even a generic "X was submitted for Y" wouldn't
  say THEY are the one being asked to act. When that manager later submits
  the SAME candidate externally, candidate_externally_submitted tells the
  ORIGINAL requester their review request resolved — save_candidate_feedback
  reads a dedicated `internal_review_requested_by` field off the
  candidate's immediately-prior state (set on an internal submission,
  cleared on the next external one, untouched by an intervening
  Reject/Unreachable) to recognize "this external submission is that
  internal request's follow-through" and to know who to notify. Both share
  _create_candidate_notification_for_recipient_sync
  (candidate-level, single specific recipient rather than a list derived
  from recruiter_emails) but — unlike candidate_passed/rejected —
  dedupe via a same-minute pre-insert check
  (_candidate_level_duplicate_in_last_minute), not a permanent index: a
  genuine second review cycle must still notify.

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

from core.advisory_lock import AdvisoryLock
from core.db import get_db_connection
from services.engage_status import (
    format_engage_status,
    hf_display_from_payload,
    score_from_payload,
    engage_display_sql,
)

logger = logging.getLogger(__name__)

# Candidate-level dedup: one row per (recipient, candidate, job, type) ever.
# Shared by every candidate-level "once ever" type (candidate_passed,
# candidate_rejected) — `type` is part of the unique index this targets
# (uniq_notifications_recipient_candidate_job_type), so each type gets its own
# independent dedup slot for the same (recipient, candidate, job).
_CONFLICT_CANDIDATE_LEVEL = "ON CONFLICT (recipient_email, candidate_id, jobdiva_id, type) DO NOTHING"
# Job-level dedup, matching the partial unique index of the same name in
# routers/notifications.py's init_notifications_schema_sync — the column
# list and WHERE clause must match an existing index exactly for Postgres to
# accept it as the ON CONFLICT arbiter. Fully static (no runtime values
# spliced in), so this is as safe to embed as the rest of the query string.
_CONFLICT_PAIR_INACTIVE = (
    "ON CONFLICT (recipient_email, jobdiva_id) "
    "WHERE type = 'pair_inactive' AND cleared_at IS NULL DO NOTHING"
)
# pair_launched (and recruiter_assigned, below) has NO index-backed ON
# CONFLICT (see _job_level_duplicate_in_last_minute below for why) — its
# caller omits conflict_clause entirely (bare INSERT) rather than passing an
# empty one.


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
    conflict_clause: Optional[str] = None,
    recipient_email: str,
    job_id: Optional[str],
    jobdiva_id: Optional[str],
    candidate_id: Optional[str],
    title: str,
    body: Optional[str],
    score: Optional[float],
) -> bool:
    """candidate_id is None for job-level event types (pair_launched,
    pair_inactive). pair_inactive passes its own type-specific
    `conflict_clause` (_CONFLICT_PAIR_INACTIVE above) rather than the
    candidate-level one, because the unique index behind the candidate-level
    clause keys on candidate_id — and NULL never equals another NULL there,
    so it would silently stop deduping at all for this type. pair_launched
    and recruiter_assigned omit conflict_clause entirely (bare INSERT, no ON
    CONFLICT) since their dedup is an application-level pre-insert check
    done by their caller instead (see _job_level_duplicate_in_last_minute)."""
    cur.execute(
        f"""
        INSERT INTO notifications
            (recipient_email, type, job_id, jobdiva_id, candidate_id, title, body, score)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        {conflict_clause or ""}
        RETURNING id
        """,
        (recipient_email, notification_type, job_id, jobdiva_id, candidate_id, title, body, score),
    )
    row = cur.fetchone()
    return row is not None


def _fetch_job_and_candidate(cur, job_id: str, candidate_id: str):
    job_row = _fetch_job_sync(cur, job_id)

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
                    conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
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


def _create_candidate_feedback_notifications_sync(
    job_id: str, candidate_id: str, actor_email: str, notification_type: str, title_template: str
) -> None:
    """Used by create_candidate_rejected_notifications: notify every OTHER
    recruiter on the job (the recruiter who just acted is excluded from
    their own notification). Generic enough to share with another
    feedback-outcome type later if one comes along — notification_type and
    title_template are the only per-type pieces. Previously also shared
    with the now-retired candidate_submitted."""
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            job_row, cand_row = _fetch_job_and_candidate(cur, job_id, candidate_id)
            if not job_row or not cand_row:
                return

            actor = (actor_email or "").strip().lower()
            recipients = [
                e for e in _parse_recruiter_emails(job_row.get("recruiter_emails")) if e != actor
            ]
            if not recipients:
                return

            candidate_name = cand_row.get("name") or "A candidate"
            job_title = _resolve_job_title(job_row, job_id)
            title = title_template.format(candidate_name=candidate_name, job_title=job_title)
            jobdiva_id = job_row.get("jobdiva_id") or job_id
            app_job_id = job_row.get("job_id") or job_id

            any_inserted = False
            for recipient in recipients:
                if _insert_notification(
                    cur,
                    notification_type=notification_type,
                    conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
                    recipient_email=recipient,
                    job_id=app_job_id,
                    jobdiva_id=jobdiva_id,
                    candidate_id=candidate_id,
                    title=title,
                    body=None,
                    score=None,
                ):
                    any_inserted = True
            conn.commit()
            if any_inserted:
                logger.info(
                    "notification_created type=%s candidate=%s job=%s recipients=%d",
                    notification_type, candidate_id, jobdiva_id, len(recipients),
                )
    finally:
        conn.close()


async def create_candidate_rejected_notifications(
    job_id: str,
    candidate_id: str,
    rejecter_email: str,
) -> None:
    """Fire in-app "candidate rejected" notifications for every OTHER
    recruiter on `job_id` — the rejecter is excluded so they don't get
    notified for their own action. Called from routers/candidates.py's
    save_candidate_feedback when feedback_type == "Reject".

    There is no generic "candidate submitted" counterpart: a Submit is
    always either internal or external, and each already has its own
    specific notification (create_candidate_internal_review_requested_notification
    for the named manager; create_candidate_externally_submitted_notification
    for the original requester once an internal review resolves). A prior
    `candidate_submitted` type existed here and was retired for exactly that
    reason — every Submit it could have fired for was already covered more
    specifically by one of those two, or by neither (a plain external Submit
    with no pending internal review to resolve).

    Safe to call unconditionally (including on a re-rejection — feedback can
    be edited and resaved, see save_candidate_feedback's docstring): dedup is
    the same (recipient_email, candidate_id, jobdiva_id, type) unique index
    candidate_passed uses, so a repeat call is a no-op rather than a second
    notification. All failures are caught and logged here, never raised back
    into the feedback endpoint's response.
    """
    try:
        if not job_id or not candidate_id:
            return
        await asyncio.to_thread(
            _create_candidate_feedback_notifications_sync,
            job_id, candidate_id, rejecter_email,
            "candidate_rejected", "{candidate_name} was rejected for {job_title}",
        )
    except Exception as e:  # noqa: BLE001 - must never raise into the feedback endpoint
        logger.error(
            "create_candidate_rejected_notifications_failed candidate=%s job=%s: %s",
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
                        conflict_clause=_CONFLICT_CANDIDATE_LEVEL,
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


def _job_level_duplicate_in_last_minute(cur, recipient_email: str, jobdiva_id: str, notification_type: str) -> bool:
    """True if this recipient already has a `notification_type` row for this
    job within the last minute. Shared by every job-level event type whose
    natural dedup key is "the same event, fired again within a moment" —
    pair_launched (a racing retry re-processing what's really one launch)
    and recruiter_assigned (an auto-saving wizard racing itself) today.

    This used to be, for pair_launched specifically, a DB-level partial
    unique index on date_trunc('minute', created_at) — removed: Postgres
    only allows IMMUTABLE expressions in an index, and
    date_trunc(text, timestamptz) is STABLE (its result depends on the
    session timezone), so that CREATE INDEX failed on every boot. Every
    statement in init_notifications_schema runs in one transaction that
    commits once at the end, so that one failure rolled back the WHOLE
    schema-init pass each time — silently preventing the feature from ever
    working: create_pair_launched_notification's INSERT referenced an index
    that had never actually been created, which fails outright, caught and
    swallowed by that function's own top-level try/except (the candidate
    still got the separate PAIR-launched EMAIL, which doesn't go through
    this table at all — that's why only the email ever arrived).

    This existence check + insert is a two-step race (two truly concurrent
    calls could both pass it), but that's an accepted trade-off per the
    original pair_launched design ("a client retry or racing workers
    handling what's really one launch") — a rare duplicate row is a far
    smaller problem than the notification never being created at all. This
    runs once per recipient on every call site's event, so the lookup below
    is backed by a per-type partial index in routers/notifications.py
    (idx_notifications_pair_launched_recent,
    idx_notifications_recruiter_assigned_recent) rather than a sequential
    scan.
    """
    cur.execute(
        """
        SELECT 1 FROM notifications
        WHERE recipient_email = %s AND jobdiva_id = %s AND type = %s
          AND created_at >= now() - INTERVAL '1 minute'
        LIMIT 1
        """,
        (recipient_email, jobdiva_id, notification_type),
    )
    return cur.fetchone() is not None


def _candidate_level_duplicate_in_last_minute(
    cur, recipient_email: str, candidate_id: str, jobdiva_id: str, notification_type: str
) -> bool:
    """Candidate-level counterpart to _job_level_duplicate_in_last_minute
    above, for candidate_internal_review_requested /
    candidate_externally_submitted. Those two are deliberately excluded from
    uniq_notifications_recipient_candidate_job_type (see
    routers/notifications.py) because a genuine second review cycle — a
    manager rejects, the recruiter resubmits internally — must still notify;
    "once ever" would silently swallow that. This only guards against the
    same request firing twice within a moment (a retry, a double-click),
    backed by idx_notifications_candidate_review_loop_recent."""
    cur.execute(
        """
        SELECT 1 FROM notifications
        WHERE recipient_email = %s AND candidate_id = %s AND jobdiva_id = %s AND type = %s
          AND created_at >= now() - INTERVAL '1 minute'
        LIMIT 1
        """,
        (recipient_email, candidate_id, jobdiva_id, notification_type),
    )
    return cur.fetchone() is not None


def _create_pair_launched_notification_sync(
    job_id: str, jobdiva_id: str, job_title: str, candidate_count: int, recipients: List[str]
) -> None:
    plural = "s" if candidate_count != 1 else ""
    title = f"PAIR launched for {job_title or 'this job'} — {candidate_count} candidate{plural}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for recipient in recipients:
                if _job_level_duplicate_in_last_minute(cur, recipient, jobdiva_id, "pair_launched"):
                    continue
                _insert_notification(
                    cur,
                    notification_type="pair_launched",
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
    candidate COUNT, never a single candidate. Dedup is an application-level
    pre-insert existence check (_job_level_duplicate_in_last_minute), not a
    Postgres-enforced UNIQUE-index guarantee (a plain, non-unique index
    still backs the check's own lookup for performance — see
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


def _create_recruiter_assigned_notifications_sync(
    job_id: str, jobdiva_id: str, job_title: str, recipients: List[str]
) -> None:
    title = f"You were added as a recruiter for {job_title or 'this job'}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for recipient in recipients:
                if _job_level_duplicate_in_last_minute(cur, recipient, jobdiva_id, "recruiter_assigned"):
                    continue
                _insert_notification(
                    cur,
                    notification_type="recruiter_assigned",
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


async def create_recruiter_assigned_notifications(
    *,
    job_id: str,
    jobdiva_id: str,
    job_title: str,
    assigned_emails: List[str],
) -> None:
    """Fire in-app "added as a recruiter" notifications for every email in
    `assigned_emails` — the caller (routers/jobs.py's save_job_draft /
    save_job_to_monitored_jobs_only) has already diffed the job's
    recruiter_emails before vs. after this save and passes only the ones
    newly present (and already excludes the person who made the change).

    Job-level — candidate_id is NULL, same as pair_launched/pair_inactive.
    Dedup is the same application-level pre-insert existence check
    pair_launched uses (_job_level_duplicate_in_last_minute) — not a
    Postgres-enforced UNIQUE-index guarantee, though a plain index still
    backs the check's own lookup for performance: the wizard autosaves
    frequently, so this guards against a rapid double-save re-notifying for
    what's really one edit, while still
    letting a genuine later remove-then-re-add notify again.
    """
    try:
        if not job_id or not jobdiva_id or not assigned_emails:
            return
        recipients = _parse_recruiter_emails(assigned_emails)
        if not recipients:
            return
        await asyncio.to_thread(
            _create_recruiter_assigned_notifications_sync, job_id, jobdiva_id, job_title, recipients
        )
    except Exception as e:  # noqa: BLE001 - must never raise into the job-save endpoint
        logger.error("create_recruiter_assigned_notifications_failed job=%s: %s", job_id, e, exc_info=True)


def _fetch_job_sync(cur, job_id: str):
    """The job half of _fetch_job_and_candidate, standalone — for
    notification types with no candidate involved at all
    (zero_match_warning's "a search found nobody" has no candidate_id by
    definition). _fetch_job_and_candidate itself calls this rather than
    duplicating the query."""
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
    return cur.fetchone()


def _create_zero_match_notification_sync(job_id: str, searcher_email: str) -> None:
    recipient = (searcher_email or "").strip().lower()
    if not recipient:
        return

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            job_row = _fetch_job_sync(cur, job_id)
            if not job_row:
                return
            jobdiva_id = job_row.get("jobdiva_id") or job_id
            app_job_id = job_row.get("job_id") or job_id
            job_title = _resolve_job_title(job_row, job_id)

            if _job_level_duplicate_in_last_minute(cur, recipient, jobdiva_id, "zero_match_warning"):
                return

            title = f"Zero candidates matched your search for {job_title}"
            inserted = _insert_notification(
                cur,
                notification_type="zero_match_warning",
                recipient_email=recipient,
                job_id=app_job_id,
                jobdiva_id=jobdiva_id,
                candidate_id=None,
                title=title,
                body=None,
                score=None,
            )
            conn.commit()
            if inserted:
                logger.info("notification_created type=zero_match_warning job=%s recipient=%s", jobdiva_id, recipient)
    finally:
        conn.close()


async def create_zero_match_notification(*, job_id: str, searcher_email: str) -> None:
    """Fire an in-app "zero candidates matched" notification to the single
    recruiter who ran the search — called from routers/candidates.py's
    /candidates/search endpoint when a FULL search's summary event reports
    total_candidates == 0.

    Unlike every other type here, this one has a specific actor (the person
    who clicked Search) rather than a job's whole recruiter_emails list —
    the point is telling that one recruiter their own search came up empty
    so they can tweak keywords/filters/rate bands, not broadcasting it to
    co-recruiters who didn't run it.

    Job-level — candidate_id is NULL, same as pair_launched/pair_inactive/
    recruiter_assigned. Dedup is the same _job_level_duplicate_in_last_minute
    same-minute collapse pair_launched/recruiter_assigned use — not a
    Postgres-enforced UNIQUE-index guarantee, though a plain index
    (idx_notifications_zero_match_warning_recent, see
    routers/notifications.py) still backs the check's own lookup for
    performance: a recruiter tweaking criteria and re-running a search 5
    minutes later and getting 0 again is a fresh, separately-worth-notifying
    result, not a duplicate of the first.
    """
    try:
        if not job_id or not searcher_email:
            return
        await asyncio.to_thread(_create_zero_match_notification_sync, job_id, searcher_email)
    except Exception as e:  # noqa: BLE001 - must never raise into the search stream
        logger.error("create_zero_match_notification_failed job=%s: %s", job_id, e, exc_info=True)


# Daily digest is a scheduled job (main.py), not a per-request trigger, so
# it has no single job/jobdiva_id to dedupe against the way the other
# job-level types do — the check below looks up by recipient_email alone,
# over a window wide enough to survive the job running a little early/late
# without re-firing for what's really the same morning, but short enough
# that the next genuine day's digest isn't blocked.
_DIGEST_DEDUP_WINDOW_HOURS = 20
# Every uvicorn worker's own APScheduler instance fires this job at the
# same cron tick, and _recipient_already_digested_recently is check-then-
# insert, not atomic — without this lock, two workers could both see "not
# digested yet" for the same recipient and both insert (PR review
# follow-up). core/advisory_lock.py's cross-worker session lock is the
# existing pattern for exactly this (see main.py's AUTO_SYNC_LOCK_KEY,
# core/startup_once.py's STARTUP_SCHEMA_LOCK_KEY) — one worker runs the
# whole digest pass, the rest skip outright rather than racing the dedup
# check. 728193-728196 are already taken (poll, jobdiva_bi_sync, AutoSync,
# startup schema); this is the next free key in that registry.
_DAILY_DIGEST_LOCK_KEY = 728197


def _recipient_already_digested_recently(cur, recipient_email: str) -> bool:
    """True if `recipient_email` already got a daily_digest notification
    within the last _DIGEST_DEDUP_WINDOW_HOURS. Same check-then-insert
    shape as _job_level_duplicate_in_last_minute — not a Postgres-enforced
    UNIQUE-index guarantee (a time-window dedup key can't be one — see that
    function's docstring for why), just sized for "once a day" instead of
    "once a minute": the scheduled job that calls this runs once daily, so
    the realistic race here is the job firing twice close together (e.g. a
    restart near the boundary), not concurrent requests.
    """
    cur.execute(
        """
        SELECT 1 FROM notifications
        WHERE recipient_email = %s AND type = 'daily_digest'
          AND created_at >= now() - (%s * INTERVAL '1 hour')
        LIMIT 1
        """,
        (recipient_email, _DIGEST_DEDUP_WINDOW_HOURS),
    )
    return cur.fetchone() is not None


def _digest_title(passed_count: int, job_count: int) -> str:
    """A friendly, honest summary line — the greeting and "for this job" /
    "across N jobs" phrase vary with count/job spread, but the closing
    clause is the same words (just singular/plural agreement) in both
    branches, not two different sign-offs."""
    where = "for this job" if job_count <= 1 else f"across {job_count} jobs"
    if passed_count == 1:
        return f"Good morning! 1 candidate passed their interview {where} and is waiting on your review."
    return f"Good morning! {passed_count} candidates passed their interviews {where} and are waiting on your review."


def _create_daily_digest_notifications_sync() -> None:
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Every recipient with at least one unread, uncleared
            # candidate_passed notification right now. Deliberately counts
            # ALL currently-unread rows, not just ones from the last 24h —
            # so a recruiter who doesn't clear their queue gets the same
            # (growing) count again tomorrow rather than the backlog going
            # silently uncounted. That's intended: the digest is meant to
            # reflect "how much is waiting on you right now," which only
            # shrinks once they actually read/clear those notifications.
            # COALESCE(jobdiva_id, job_id) because COUNT(DISTINCT ...)
            # ignores NULLs outright — a row with neither would still count
            # as "no job," which _digest_title already renders sensibly
            # ("for this job" at job_count <= 1), so no extra handling
            # needed, just the right count.
            cur.execute(
                """
                SELECT recipient_email, COUNT(*) AS passed_count,
                       COUNT(DISTINCT COALESCE(jobdiva_id, job_id)) AS job_count
                FROM notifications
                WHERE type = 'candidate_passed' AND read_at IS NULL AND cleared_at IS NULL
                GROUP BY recipient_email
                """
            )
            rows = cur.fetchall()

            sent = 0
            for row in rows:
                recipient = row["recipient_email"]
                if _recipient_already_digested_recently(cur, recipient):
                    continue
                title = _digest_title(row["passed_count"], row["job_count"])
                if _insert_notification(
                    cur,
                    notification_type="daily_digest",
                    recipient_email=recipient,
                    job_id=None,
                    jobdiva_id=None,
                    candidate_id=None,
                    title=title,
                    body=None,
                    score=None,
                ):
                    sent += 1
            conn.commit()
            if sent:
                logger.info("notification_created type=daily_digest recipients=%d", sent)
    finally:
        conn.close()


async def create_daily_digest_notifications() -> None:
    """Scheduled once-a-day summary — main.py registers this at 19:00
    Asia/Kolkata. India has no DST, so that fixed IST time lands reliably
    at either 9:30am EDT or 8:30am EST depending on the time of year (see
    the PR description's timezone math) — close to when a US-based
    recruiter starts their day, without this code needing to track the US
    DST transition dates itself.

    For every recruiter with at least one unread candidate_passed
    notification, fires one daily_digest row summarizing how many and
    across how many jobs. Scoped to candidate_passed specifically, not
    every unread type: "N candidates passed and are awaiting your review"
    is the one summary a recruiter can act on at a glance — submitted/
    rejected/job-level types don't have the same "something is waiting on
    me" shape.

    Job-level in form only — jobdiva_id is NULL, since a digest spans every
    job a recipient has unread Pass notifications on, not one (see
    routers/notifications.py's schema init for the nullable-jobdiva_id
    migration this required). All failures are caught and logged here,
    never raised back into the scheduler loop.

    Every uvicorn worker schedules this same job, so it takes a cross-
    worker advisory lock first (_DAILY_DIGEST_LOCK_KEY) — only one worker
    actually runs the digest pass per cron tick; the rest skip immediately.
    Without this, _recipient_already_digested_recently's check-then-insert
    could let two workers both insert for the same recipient.
    """
    lock = AdvisoryLock(_DAILY_DIGEST_LOCK_KEY, "DailyDigest")
    if not await lock.try_acquire():
        logger.info("DailyDigest: another worker is running this cycle; skipping.")
        return
    try:
        await asyncio.to_thread(_create_daily_digest_notifications_sync)
    except Exception as e:  # noqa: BLE001 - must never crash the scheduler loop
        logger.error("create_daily_digest_notifications_failed: %s", e, exc_info=True)
    finally:
        await lock.release()


_TITLE_INTERNAL_REVIEW_REQUESTED = "{candidate_name} was internally submitted for {job_title} and has requested a review."
_TITLE_EXTERNALLY_SUBMITTED = "{candidate_name} was externally submitted for {job_title}."


def _create_candidate_notification_for_recipient_sync(
    job_id: str, candidate_id: str, recipient_email: str, notification_type: str, title_template: str
) -> None:
    """Shared by create_candidate_internal_review_requested_notification and
    create_candidate_externally_submitted_notification: candidate-level, but
    for a single specific recipient the caller already knows (the manager on
    an internal submission; the original requester on its external
    follow-through), not derived from the job's recruiter_emails at all.
    Dedup is _candidate_level_duplicate_in_last_minute, not the permanent
    uniq_notifications_recipient_candidate_job_type index candidate_passed/
    submitted/rejected share — a later, genuine review cycle must still
    notify."""
    recipient = (recipient_email or "").strip().lower()
    if not recipient:
        return

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            job_row, cand_row = _fetch_job_and_candidate(cur, job_id, candidate_id)
            if not job_row or not cand_row:
                return

            candidate_name = cand_row.get("name") or "A candidate"
            job_title = _resolve_job_title(job_row, job_id)
            title = title_template.format(candidate_name=candidate_name, job_title=job_title)
            jobdiva_id = job_row.get("jobdiva_id") or job_id
            app_job_id = job_row.get("job_id") or job_id

            if _candidate_level_duplicate_in_last_minute(cur, recipient, candidate_id, jobdiva_id, notification_type):
                return

            inserted = _insert_notification(
                cur,
                notification_type=notification_type,
                conflict_clause=None,
                recipient_email=recipient,
                job_id=app_job_id,
                jobdiva_id=jobdiva_id,
                candidate_id=candidate_id,
                title=title,
                body=None,
                score=None,
            )
            conn.commit()
            if inserted:
                logger.info(
                    "notification_created type=%s candidate=%s job=%s recipient=%s",
                    notification_type, candidate_id, jobdiva_id, recipient,
                )
    finally:
        conn.close()


async def create_candidate_internal_review_requested_notification(
    *, job_id: str, candidate_id: str, manager_email: str
) -> None:
    """Notify the manager named on an internal submission that a review is
    waiting — called from save_candidate_feedback alongside (independently
    of) the existing notify_internal_submission_to_manager email. The
    manager usually isn't one of the job's other recruiters, so nothing
    else would reach them, and this wording says what's actually being
    asked of them rather than a generic "X was submitted for Y"."""
    try:
        if not job_id or not candidate_id or not manager_email:
            return
        await asyncio.to_thread(
            _create_candidate_notification_for_recipient_sync,
            job_id, candidate_id, manager_email,
            "candidate_internal_review_requested",
            _TITLE_INTERNAL_REVIEW_REQUESTED,
        )
    except Exception as e:  # noqa: BLE001 - must never raise into the feedback endpoint
        logger.error(
            "create_candidate_internal_review_requested_notification_failed candidate=%s job=%s: %s",
            candidate_id, job_id, e, exc_info=True,
        )


async def create_candidate_externally_submitted_notification(
    *, job_id: str, candidate_id: str, recipient_email: str
) -> None:
    """Notify the original internal-review requester once that review
    resolves in an external submission — called from save_candidate_feedback
    when this submission is external and the candidate's immediately-prior
    state shows a still-pending internal request. Without this the
    requester would have no notification at all for that resolution — there
    is no generic "candidate submitted" notification to fall back on; see
    create_candidate_rejected_notifications's docstring for why."""
    try:
        if not job_id or not candidate_id or not recipient_email:
            return
        await asyncio.to_thread(
            _create_candidate_notification_for_recipient_sync,
            job_id, candidate_id, recipient_email,
            "candidate_externally_submitted",
            _TITLE_EXTERNALLY_SUBMITTED,
        )
    except Exception as e:  # noqa: BLE001 - must never raise into the feedback endpoint
        logger.error(
            "create_candidate_externally_submitted_notification_failed candidate=%s job=%s: %s",
            candidate_id, job_id, e, exc_info=True,
        )
