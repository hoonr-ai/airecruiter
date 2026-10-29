"""Regression guard for the two new candidate feedback statuses added
alongside Submit/Reject/Unreachable: 'Allotment Slot Filled' (a plain
selectable status, no other wiring) and 'Callback Scheduled' (opens a
modal for a future datetime, schedules an in-app reminder 5 minutes before
it via task_outbox, never touches JobDiva).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

from core.auth import UserIdentity
from models import CandidateFeedbackRequest


class _FakeCursor:
    def __init__(self, select_row=None, select_rows=None):
        self.select_row = select_row
        # Several real sourced_candidates rows for one candidate+job (a
        # multi-sourced candidate — JobDiva + LinkedIn + manual, …), each
        # (id, data, updated_at), for the fallback Reject lock's fetchall().
        # Falls back to wrapping select_row as the sole row when not given,
        # so every existing single-row test needs no changes.
        self.select_rows = select_rows
        self.executed_queries = []
        self._last_query = ""

    def execute(self, query, params=None):
        self.executed_queries.append((query, params))
        self._last_query = query

    # Keyed on "FOR UPDATE" (present on both Reject row-lock queries) rather
    # than exact RETURNING column text — survives the lock SQL growing or
    # reordering columns, unlike matching the old literal "RETURNING
    # prev.data" substring (PR #870 review: flagged as brittle, and it had
    # in fact already gone stale against the fallback path's own RETURNING
    # once that grew prev.id/prev.updated_at for the ordering fix below).
    def _is_lock_query(self) -> bool:
        return "FOR UPDATE" in self._last_query

    def fetchone(self):
        # The sc_row_id fast-path Reject lock always targets exactly one
        # row, so it returns a single `prev.data` column — not the
        # multi-column row the initial candidate-resolution SELECT returns.
        if self._is_lock_query():
            return (self.select_row[3],) if self.select_row else None
        return self.select_row

    def fetchall(self):
        # The fallback Reject lock (candidate_id/jobdiva_id match) can lock
        # more than one duplicate row; the real query returns one
        # (id, data, updated_at) row per match.
        if self._is_lock_query():
            if self.select_rows is not None:
                return [(r[0], r[3], r[8] if len(r) > 8 else None) for r in self.select_rows]
            return [(self.select_row[0], self.select_row[3], None)] if self.select_row else []
        return [self.select_row] if self.select_row else []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeConnection:
    def __init__(self, select_row=None, select_rows=None):
        self.select_row = select_row
        self.select_rows = select_rows
        self.committed = False
        self.closed = False
        # save_candidate_feedback opens several get_db_connection() calls
        # across the flow, each getting a fresh cursor — keep every one so
        # a test can inspect the UPDATE this handler issued after the fact.
        self.cursors = []

    def cursor(self):
        cur = _FakeCursor(self.select_row, self.select_rows)
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


_FAKE_CAND_ROW = (
    55148489, "21562841721070", "26-12137", {}, "33087136",
    "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# Same candidate, but auto-provisioned: data carries its own JobDiva
# candidate id, distinct from sourced_candidates.candidate_id (row[1]) —
# the shape that exposes the jd_candidate_id/sc_candidate_id bug (PR #848
# review).
_FAKE_CAND_ROW_AUTO_PROVISIONED = (
    55148489, "21562841721070", "26-12137", {"jobdiva_candidate_id": "99999999"}, "33087136",
    "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# Already has an ACTIVE (not done) callback pending — this write is a real
# Reschedule.
_FAKE_CAND_ROW_WITH_ACTIVE_CALLBACK = (
    55148489, "21562841721070", "26-12137",
    {"feedback_type": "Callback Scheduled", "callback_scheduled_at": "2026-01-01T10:00:00+00:00"},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# Had a callback, but it was already marked done — a fresh Schedule, not a
# Reschedule of something still pending.
_FAKE_CAND_ROW_WITH_DONE_CALLBACK = (
    55148489, "21562841721070", "26-12137",
    {
        "feedback_type": "Callback Scheduled",
        "callback_scheduled_at": "2026-01-01T10:00:00+00:00",
        "callback_done_at": "2026-01-01T10:05:00+00:00",
    },
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)


def _base_setup(monkeypatch, select_row=_FAKE_CAND_ROW, select_rows=None):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
    fake_conn = _FakeConnection(select_row=select_row, select_rows=select_rows)
    monkeypatch.setattr(cand_module, "get_db_connection", lambda: fake_conn)
    monkeypatch.setattr(cand_module, "refresh_feedback_metrics_sync", lambda job_ref: {"feedback_completed": 1, "pair_submits": 0})

    jobdiva_calls = []

    async def fake_create_note(**kwargs):
        jobdiva_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "create_candidate_note", fake_create_note)
    return cand_module, jobdiva_calls, fake_conn


@pytest.mark.anyio
async def test_allotment_slot_filled_skips_jobdiva_note(monkeypatch):
    cand_module, jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Allotment Slot Filled")

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0


@pytest.mark.anyio
async def test_callback_scheduled_without_callback_at_is_400(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled")

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 400
    assert "callback_at" in exc_info.value.detail


@pytest.mark.anyio
async def test_callback_scheduled_rejects_invalid_datetime(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at="not-a-date")

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_callback_scheduled_rejects_a_past_time(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=past)

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 400
    assert "future" in exc_info.value.detail


@pytest.mark.anyio
async def test_callback_scheduled_skips_jobdiva_note_and_schedules_reminder(monkeypatch):
    cand_module, jobdiva_calls, _fake_conn = _base_setup(monkeypatch)

    outbox_calls = []

    def fake_add(cur, task_name, *args, dedupe_key=None, available_at=None):
        outbox_calls.append({"task_name": task_name, "args": args, "dedupe_key": dedupe_key, "available_at": available_at})
        return 1

    monkeypatch.setattr("services.task_outbox.add", fake_add)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0

    # PR #851 review: notifications_service._fetch_job_and_candidate's
    # duplicate-row-ordering fix relies on this write actually bumping
    # updated_at — there is no trigger doing it automatically.
    update_calls = [
        (query, params) for cur in _fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    assert len(update_calls) == 1
    assert "updated_at = now()" in update_calls[0][0].lower()

    assert len(outbox_calls) == 1
    call = outbox_calls[0]
    from core.queue_tasks import CANDIDATE_CALLBACK_REMINDER
    assert call["task_name"] == CANDIDATE_CALLBACK_REMINDER
    assert call["args"][2] == "recruiter@pyramidci.com"
    # Reminder fires 5 minutes before the recruiter's chosen callback time,
    # not at the callback time itself.
    expected_remind_at = future - timedelta(minutes=5)
    assert abs((call["available_at"] - expected_remind_at).total_seconds()) < 1
    assert call["dedupe_key"] is not None


@pytest.mark.anyio
async def test_callback_scheduled_reminder_uses_sourced_candidate_id_for_auto_provisioned(monkeypatch):
    """PR #848 review: for an auto-provisioned candidate (one with its own
    JobDiva id in data["jobdiva_candidate_id"], distinct from
    sourced_candidates.candidate_id), the reminder must be queued with the
    latter. notifications_service's own candidate lookup
    (_fetch_job_and_candidate) matches on that column — queuing the JobDiva
    id instead (the original bug) meant the lookup silently found nothing
    and the reminder never fired, with the outbox row still marked done.
    """
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch, select_row=_FAKE_CAND_ROW_AUTO_PROVISIONED)

    outbox_calls = []

    def fake_add(cur, task_name, *args, dedupe_key=None, available_at=None):
        outbox_calls.append({"task_name": task_name, "args": args, "dedupe_key": dedupe_key})
        return 1

    monkeypatch.setattr("services.task_outbox.add", fake_add)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert res["status"] == "success"
    assert len(outbox_calls) == 1
    # args: (job_id, candidate_id, recruiter_email, callback_at)
    queued_candidate_id = outbox_calls[0]["args"][1]
    assert queued_candidate_id == "21562841721070"  # sourced_candidates.candidate_id
    assert queued_candidate_id != "99999999"  # not data["jobdiva_candidate_id"]
    # The dedupe key stays on the old (JobDiva-id) shape regardless — see
    # candidates.py's own comment at the call site for why a key already
    # queued before this fix must keep deduping the same way after it.
    assert outbox_calls[0]["dedupe_key"] == f"callback_reminder:99999999:26-12137:{future.isoformat()}"


@pytest.mark.anyio
async def test_callback_scheduled_persists_callback_time_in_feedback_data(monkeypatch):
    cand_module, _jobdiva_calls, fake_conn = _base_setup(monkeypatch)
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    assert len(update_calls) == 1
    _query, params = update_calls[0]
    feedback_payload = json.loads(params[0])
    assert feedback_payload["feedback_type"] == "Callback Scheduled"
    assert feedback_payload["callback_scheduled_at"] == future.isoformat()


@pytest.mark.anyio
async def test_callback_scheduled_clears_a_previous_callback_done_at(monkeypatch):
    """PR #844 review (High): a fresh Callback Scheduled write — the first
    time, or a reschedule after a previous one was marked done via
    POST .../callback/done — must clear callback_done_at. Otherwise
    list_upcoming_callbacks (filtered on callback_done_at IS NULL) would
    silently never show the new callback even though a reminder is about
    to fire for it."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(monkeypatch)
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    feedback_payload = json.loads(update_calls[0][1][0])
    assert feedback_payload["callback_done_at"] is None


@pytest.mark.anyio
async def test_callback_scheduled_marks_is_reschedule_false_for_a_fresh_schedule(monkeypatch):
    """No callback pending beforehand (the default _FAKE_CAND_ROW has no
    feedback_type/callback_scheduled_at at all) — this is a first-time
    Schedule, not a Reschedule."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(monkeypatch)
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    feedback_payload = json.loads(update_calls[0][1][0])
    assert feedback_payload["callback_is_reschedule"] is False


@pytest.mark.anyio
async def test_callback_scheduled_marks_is_reschedule_true_when_one_was_already_active(monkeypatch):
    """An ACTIVE (not yet done) callback already pending — this write is
    changing its time, the actual Reschedule case the Upcoming Callbacks
    page's own Reschedule button produces."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_WITH_ACTIVE_CALLBACK,
    )
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    feedback_payload = json.loads(update_calls[0][1][0])
    assert feedback_payload["callback_is_reschedule"] is True


@pytest.mark.anyio
async def test_callback_scheduled_marks_is_reschedule_false_after_a_done_callback(monkeypatch):
    """The previous callback already got marked done (POST .../callback/
    done) — a brand new Schedule for this candidate afterward is a fresh
    booking, not a Reschedule of something that already concluded."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_WITH_DONE_CALLBACK,
    )
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    feedback_payload = json.loads(update_calls[0][1][0])
    assert feedback_payload["callback_is_reschedule"] is False


@pytest.mark.anyio
async def test_callback_scheduled_payload_omits_submit_only_fields(monkeypatch):
    """PR #844 review (Medium): a reschedule from the Upcoming Callbacks page
    sends only feedback_type + callback_at — confirms that write never
    includes submission_type/manager_email/recruiter_notes (Submit-only
    fields). sourced_candidates.data is merged with Postgres's `||` (only
    overwrites keys present in this dict), so their absence here is exactly
    what guarantees a reschedule can't wipe out an unrelated prior Submit's
    fields."""
    cand_module, jobdiva_calls, fake_conn = _base_setup(monkeypatch)
    monkeypatch.setattr("services.task_outbox.add", lambda *a, **kw: 1)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=3)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert len(jobdiva_calls) == 0  # no JobDiva note fired for the reschedule either
    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    feedback_payload = json.loads(update_calls[0][1][0])
    assert "submission_type" not in feedback_payload
    assert "manager_email" not in feedback_payload
    assert "recruiter_notes" not in feedback_payload


@pytest.mark.anyio
async def test_callback_scheduled_rejects_a_callback_under_five_minutes_away(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    soon = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=soon)

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 400
    assert "5 minutes" in exc_info.value.detail


@pytest.mark.anyio
async def test_callback_scheduled_rejects_a_callback_more_than_a_year_out(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    far = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=far)

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 400
    assert "year" in exc_info.value.detail


@pytest.mark.anyio
async def test_reject_after_callback_scheduled_clears_the_stale_callback_field(monkeypatch):
    """PR #841 review: callback_scheduled_at must not survive a later
    Reject/Submit/Unreachable/Allotment Slot Filled — otherwise the queued
    reminder task (which checks this field before notifying) would still
    see what looks like a pending callback. callback_is_reschedule
    (PR #856 review) gets the same treatment — stale otherwise, since it
    would survive the JSONB merge the same way."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_WITH_ACTIVE_CALLBACK,
    )
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Reject", reason="Skills do not meet requirements")

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        (query, params) for cur in fake_conn.cursors for query, params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    assert len(update_calls) >= 1
    _query, params = update_calls[0]
    feedback_payload = json.loads(params[0])
    assert feedback_payload["callback_scheduled_at"] is None
    assert feedback_payload["callback_is_reschedule"] is None


@pytest.mark.anyio
async def test_callback_scheduled_failure_to_queue_reminder_rolls_back_the_feedback_write(monkeypatch):
    """PR #841 review: if task_outbox.add fails, the feedback write must not
    silently commit without its reminder — the caller gets a 500 and the
    transaction's own commit() is never reached."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(monkeypatch)

    def fake_add_raises(*a, **kw):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("services.task_outbox.add", fake_add_raises)

    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    request = CandidateFeedbackRequest(feedback_type="Callback Scheduled", callback_at=future.isoformat())

    with pytest.raises(HTTPException) as exc_info:
        await cand_module.save_candidate_feedback(
            job_id_or_ref="26-12137", candidate_id="55148489",
            request=request, background_tasks=BackgroundTasks(), user=user,
        )
    assert exc_info.value.status_code == 500
    # The UPDATE was issued on the same cursor/transaction as the (never
    # reached) task_outbox insert, so it never committed — the DB-level
    # equivalent of a rollback for a connection that commits explicitly.
    assert fake_conn.committed is False


@pytest.mark.anyio
async def test_unreachable_still_skips_jobdiva_note(monkeypatch):
    """Guard against accidentally narrowing the skip condition to only the
    two new types when extending it."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Unreachable")

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0
