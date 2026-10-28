"""Changing a candidate's rejection reason after it was already recorded.

Previously the frontend's Select-based "Reject" action could never be
re-triggered once a candidate was already rejected (Radix only calls
onValueChange when the picked value differs from the Select's current
controlled value, so re-picking "Reject" while it's already "Reject" was a
no-op there) — the UI now exposes a dedicated "Change reason" action that
re-opens the same reject flow regardless. This file guards the backend side
of that: save_candidate_feedback must create a new JobDiva note whenever the
rejection reason actually changes (first rejection, or one reason to
another), but must NOT create a duplicate note when the same reason is
re-confirmed unchanged. See also test_rejection_reasons_sync.py (the
reason->JobDiva-action mapping itself) and queue_tasks.CREATE_JOBDIVA_PASS_NOTE
(the unrelated, still-pinned "Pass" note flow this must not touch).
"""
import pytest
from fastapi import BackgroundTasks

from core.auth import UserIdentity
from models import CandidateFeedbackRequest

from tests.test_candidate_feedback_new_statuses import _base_setup

_FAKE_CAND_ROW_ALREADY_REJECTED = (
    55148489, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements"},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)


async def _reject(cand_module, reason):
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Reject", reason=reason)
    return await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )


@pytest.mark.anyio
async def test_first_rejection_creates_jobdiva_note(monkeypatch):
    cand_module, jobdiva_calls, _fake_conn = _base_setup(monkeypatch)

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Skills do not meet requirements"


@pytest.mark.anyio
async def test_reconfirming_same_reason_does_not_duplicate_the_note(monkeypatch):
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0


@pytest.mark.anyio
async def test_changing_to_a_different_reason_creates_a_new_note(monkeypatch):
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Communication skills"


@pytest.mark.anyio
async def test_changed_reason_note_is_never_pinned(monkeypatch):
    """Requirement: a changed/new rejection note must not be pinned — only
    the unrelated Pass-note flow (queue_tasks.CREATE_JOBDIVA_PASS_NOTE) pins
    notes, and that must stay untouched by this change."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    pin_calls = []
    from services.jobdiva import jobdiva_service

    async def fake_pin(**kwargs):
        pin_calls.append(kwargs)
        return {"status": "success"}

    monkeypatch.setattr(jobdiva_service, "pin_candidate_note", fake_pin)

    res = await _reject(cand_module, "Communication skills")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert len(pin_calls) == 0
