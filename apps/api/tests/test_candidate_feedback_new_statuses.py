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
    def __init__(self, select_row=None):
        self.select_row = select_row
        self.executed_queries = []

    def execute(self, query, params=None):
        self.executed_queries.append((query, params))

    def fetchone(self):
        return self.select_row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeConnection:
    def __init__(self, select_row=None):
        self.select_row = select_row
        self.committed = False
        self.closed = False
        # save_candidate_feedback opens several get_db_connection() calls
        # across the flow, each getting a fresh cursor — keep every one so
        # a test can inspect the UPDATE this handler issued after the fact.
        self.cursors = []

    def cursor(self):
        cur = _FakeCursor(self.select_row)
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


def _base_setup(monkeypatch):
    from routers import candidates as cand_module
    from services.jobdiva import jobdiva_service

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda *a, **kw: None)
    fake_conn = _FakeConnection(select_row=_FAKE_CAND_ROW)
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
