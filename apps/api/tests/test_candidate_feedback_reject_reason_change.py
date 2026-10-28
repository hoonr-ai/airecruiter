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
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# Same prior reason, but the PRIOR create_candidate_note call never actually
# succeeded (feedback_synced stayed False) — a recruiter re-submitting the
# same reason here is retrying a failed sync, not re-confirming a no-op, and
# must still get a note.
_FAKE_CAND_ROW_REJECTED_SYNC_FAILED = (
    55148489, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": False},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# A prior Submit (not a Reject) with the same string coincidentally left in
# feedback_reason (recruiter_notes-style leftover) must not be mistaken for
# an unchanged rejection — feedback_type doesn't match "Reject".
_FAKE_CAND_ROW_PRIOR_SUBMIT = (
    55148489, "21562841721070", "26-12137",
    {"feedback_type": "Submit", "feedback_reason": "Skills do not meet requirements", "feedback_synced": True},
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
    # A first-ever rejection's note must read exactly as it always has — only
    # a genuine reason CHANGE (see test_changing_to_a_different_reason_
    # creates_a_new_note below) gets the extra "updated from ... to ..." sentence.
    assert "The rejection reason was updated" not in jobdiva_calls[0]["note_text"]
    assert jobdiva_calls[0]["note_text"].startswith("<a href=")
    assert jobdiva_calls[0]["note_text"].endswith("Click Here</a> to view the report.")


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
    # Confirmed with product (Somya): an updated rejection's note must say so
    # in its own text, so it reads differently from a first-time rejection
    # even though both show under the same "PAIR Reject - <reason>" action.
    note_text = jobdiva_calls[0]["note_text"]
    assert note_text.startswith("The rejection reason was updated from \"Skills do not meet requirements\" to \"Communication skills\".")
    assert note_text.endswith("Click Here</a> to view the report.")


@pytest.mark.anyio
async def test_retry_after_failed_sync_creates_note_even_with_same_reason(monkeypatch):
    """PR #868 review (medium): if the prior Reject's own create_candidate_note
    call failed, feedback_synced stays False — re-submitting the identical
    reason must still create a note (it's a retry, not a no-op), or JobDiva
    would be left permanently out of sync while the response reports
    success."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_REJECTED_SYNC_FAILED,
    )

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Skills do not meet requirements"
    # Same reason as before — a retried sync, not a reason change, so the
    # note must read like a plain (first-time-shaped) rejection note.
    assert "The rejection reason was updated" not in jobdiva_calls[0]["note_text"]


@pytest.mark.anyio
async def test_prior_submit_with_same_text_is_not_treated_as_unchanged_reject(monkeypatch):
    """A prior feedback_type of "Submit" must never satisfy the "unchanged"
    check just because feedback_reason happens to hold the same string —
    only a prior Reject can be "unchanged"; anything else is a fresh
    rejection and must get its own note."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_PRIOR_SUBMIT,
    )

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject - Skills do not meet requirements"
    # Prior feedback_type wasn't "Reject", so this is a fresh rejection, not
    # a reason change — same plain note shape as a genuine first rejection.
    assert "The rejection reason was updated" not in jobdiva_calls[0]["note_text"]


@pytest.mark.anyio
async def test_empty_reason_falls_back_to_generic_reject_action_and_still_notes(monkeypatch):
    """No reason provided (reason=None) must map to the generic "PAIR
    Reject" action (REJECTION_REASON_TO_JOBDIVA_ACTION's own default) and
    must not be mistaken for "unchanged" against a prior reason-less
    rejection — both sides normalize None/"" the same way via str(...).strip(),
    so two genuinely reason-less Rejects in a row DO count as unchanged,
    which is correct: there's no reason text to have "changed"."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(monkeypatch)

    res = await _reject(cand_module, None)

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    assert jobdiva_calls[0]["action"] == "PAIR Reject"


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
