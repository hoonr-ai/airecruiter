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
import json
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks, HTTPException

from core.auth import UserIdentity
from models import CandidateFeedbackRequest
from routers.candidates import MAX_REJECT_REASON_CHANGES, _comparable_updated_at

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

# A multi-sourced candidate can have more than one sourced_candidates row for
# the same candidate_id + job; the fallback Reject lock can match several.
# Higher id is deliberately the staler row, lower id the current one, so
# "whichever comes back first"/"highest id" would still pass by accident
# unless id and recency disagree like this.
#
# Naive datetimes: the real column is TIMESTAMP (no time zone).
_FAKE_ROW_DUP_STALE_HIGHER_ID = (
    99999999, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Old stale reason from a different duplicate", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    datetime(2025, 1, 1),
)
_FAKE_ROW_DUP_CURRENT_LOWER_ID = (
    1, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    datetime(2026, 1, 1),
)

# One duplicate with a real updated_at, one NULL — the pairing that crashed
# in production (naive-vs-aware TypeError inside max(), -> HTTP 500).
_FAKE_ROW_DUP_NULL_UPDATED_AT = (
    99999999, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Old stale reason from a different duplicate", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    None,
)
_FAKE_ROW_DUP_REAL_UPDATED_AT = (
    1, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    datetime(2026, 1, 1),
)

# Both duplicates with a real updated_at, but one tz-AWARE (e.g. a column
# that's already migrated to TIMESTAMPTZ) and one naive — the shape
# _comparable_updated_at's own unit tests cover directly; this pins the same
# mix down at the full save_candidate_feedback endpoint, not just the helper
# in isolation.
_FAKE_ROW_DUP_AWARE_OLDER = (
    99999999, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Old stale reason from a different duplicate", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    datetime(2025, 1, 1, tzinfo=timezone.utc),
)
_FAKE_ROW_DUP_NAIVE_NEWER = (
    1, "21562841721070", "26-12137",
    {"feedback_type": "Reject", "feedback_reason": "Skills do not meet requirements", "feedback_synced": True},
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
    datetime(2026, 1, 1),
)

# Already rejected, and the reason has already been changed once —
# reject_reason_change_count carries that history forward. A SECOND change
# (still under MAX_REJECT_REASON_CHANGES=2) must be allowed.
_FAKE_CAND_ROW_ONE_CHANGE_ALREADY = (
    55148489, "21562841721070", "26-12137",
    {
        "feedback_type": "Reject", "feedback_reason": "Communication skills",
        "feedback_synced": True, "reject_reason_change_count": 1,
    },
    "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
)

# Already changed twice — at MAX_REJECT_REASON_CHANGES. A THIRD change must
# be blocked.
_FAKE_CAND_ROW_TWO_CHANGES_ALREADY = (
    55148489, "21562841721070", "26-12137",
    {
        "feedback_type": "Reject", "feedback_reason": "Communication skills",
        "feedback_synced": True, "reject_reason_change_count": 2,
    },
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


@pytest.mark.anyio
async def test_reject_update_locks_the_row_before_deciding(monkeypatch):
    """PR #869 review (medium): the "unchanged vs changed" decision and the
    write must happen atomically, or two concurrent Reject requests for the
    same candidate could both read the same prior reason and both decide
    "changed" (duplicate JobDiva notes) or both decide "unchanged" against
    each other's not-yet-visible write (a dropped note for a real change).
    Regression guard: the UPDATE for a Reject must take the row lock (FOR
    UPDATE) and read its pre-write state back atomically (RETURNING), not
    rely on the far-earlier, separately-connected/transacted `data_blob`
    from step 1."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    await _reject(cand_module, "Communication skills")

    # Step 3's feedback write specifically (bumps updated_at) — distinct from
    # the later feedback_synced marker update on a successful JobDiva sync,
    # which also matches "UPDATE sourced_candidates" but isn't what this
    # guards.
    update_calls = [
        query for cur in fake_conn.cursors for query, _params in cur.executed_queries
        if "UPDATE sourced_candidates" in query and "updated_at = now()" in query
    ]
    assert len(update_calls) == 1
    assert "FOR UPDATE" in update_calls[0]
    assert "RETURNING prev.data" in update_calls[0]


@pytest.mark.anyio
async def test_non_reject_feedback_does_not_lock_the_row(monkeypatch):
    """The row lock above is scoped to Reject only — same reasoning as the
    Callback Scheduled "read without FOR UPDATE" comment earlier in this
    file: locking here too would serialize every OTHER feedback_type
    through this one row's lock for a race that, for them, doesn't corrupt
    anything the UPDATE's own atomic JSONB merge doesn't already handle."""
    cand_module, _jobdiva_calls, fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Unreachable")

    await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    update_calls = [
        query for cur in fake_conn.cursors for query, _params in cur.executed_queries
        if "UPDATE sourced_candidates" in query
    ]
    assert len(update_calls) == 1
    assert "FOR UPDATE" not in update_calls[0]


@pytest.mark.anyio
async def test_fallback_lock_picks_the_most_recently_updated_duplicate_row(monkeypatch):
    """PR #870 review (medium): the fallback path (no sourced_row_id pin) can
    lock more than one sourced_candidates row for a multi-sourced candidate.
    The "reason unchanged" decision must deliberately pick the most recently
    updated one — not whichever row the driver happens to return first, and
    not just the highest id, which is why the fixture's lower-id row is the
    newer one."""
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=None,
        select_rows=[_FAKE_ROW_DUP_STALE_HIGHER_ID, _FAKE_ROW_DUP_CURRENT_LOWER_ID],
    )

    # Same reason as the most-recently-updated duplicate (lower id) — must
    # read as unchanged. Picking the stale higher-id duplicate instead would
    # wrongly see this as a change (its reason text differs) and spam a note.
    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0

    lock_calls = [
        query for cur in fake_conn.cursors for query, _params in cur.executed_queries
        if "FOR UPDATE" in query
    ]
    assert len(lock_calls) == 1
    # Deterministic lock-acquisition order (PR #870 review): without this,
    # two concurrent requests matching the same duplicate rows could lock
    # them in different orders and deadlock instead of one waiting.
    assert "ORDER BY id" in lock_calls[0]


@pytest.mark.anyio
async def test_fallback_lock_reports_change_against_the_current_duplicate_row(monkeypatch):
    """Same fixture as above, but with an actually different new reason —
    the note must say it changed FROM the most-recently-updated duplicate's
    reason, not the stale one."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=None,
        select_rows=[_FAKE_ROW_DUP_STALE_HIGHER_ID, _FAKE_ROW_DUP_CURRENT_LOWER_ID],
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    note_text = jobdiva_calls[0]["note_text"]
    assert note_text.startswith(
        "The rejection reason was updated from \"Skills do not meet requirements\" to \"Communication skills\"."
    )
    assert "Old stale reason" not in note_text


@pytest.mark.anyio
async def test_fallback_lock_does_not_crash_when_a_duplicate_has_a_null_updated_at(monkeypatch):
    """Production regression: a NULL updated_at next to a real one used to
    raise a naive/aware TypeError in the tiebreak's max() -> HTTP 500. The
    row with an actual timestamp must still win over the null one."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=None,
        select_rows=[_FAKE_ROW_DUP_NULL_UPDATED_AT, _FAKE_ROW_DUP_REAL_UPDATED_AT],
    )

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    # Picked the row with a real updated_at (reason: "Skills do not meet
    # requirements") over the null-updated_at one, so this reads as
    # unchanged — not a crash, and not a false "changed" against the stale
    # null row.
    assert len(jobdiva_calls) == 0


# --- _comparable_updated_at -----------------------------------------------
# Direct unit coverage so a future TIMESTAMPTZ migration of this column
# (which would start handing _cur2 tz-aware values instead of naive ones)
# doesn't reopen this crash.

def test_comparable_updated_at_none_sorts_as_the_oldest():
    assert _comparable_updated_at(None) == datetime.min


def test_comparable_updated_at_keeps_naive_values_as_is():
    naive = datetime(2026, 1, 1, 12, 0)
    assert _comparable_updated_at(naive) == naive


def test_comparable_updated_at_strips_tz_from_aware_values():
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    result = _comparable_updated_at(aware)
    assert result.tzinfo is None
    assert result == datetime(2026, 1, 1, 12, 0)


def test_comparable_updated_at_lets_naive_and_aware_rows_compare():
    """The actual bug: comparing a naive and an aware datetime directly
    raises TypeError. Both sides funneled through here must not."""
    naive = datetime(2026, 1, 1)
    aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert max([naive, aware], key=_comparable_updated_at) == naive
    assert max([None, naive, aware], key=_comparable_updated_at) == naive


@pytest.mark.anyio
async def test_fallback_lock_does_not_crash_with_a_mix_of_aware_and_naive_duplicates(monkeypatch):
    """Same naive/aware mix as the _comparable_updated_at unit tests above,
    pinned down at the full save_candidate_feedback endpoint too — not just
    the helper in isolation — in case a future change re-introduces a raw
    comparison somewhere between the DB read and here."""
    cand_module, jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=None,
        select_rows=[_FAKE_ROW_DUP_AWARE_OLDER, _FAKE_ROW_DUP_NAIVE_NEWER],
    )

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0  # naive-newer row's reason matches -> unchanged


# --- MAX_REJECT_REASON_CHANGES ---------------------------------------------


def _count_patch_payloads(fake_conn):
    """json.loads of every UPDATE's `reject_reason_change_count` patch
    payload issued across every cursor this request opened, in call order."""
    payloads = []
    for cur in fake_conn.cursors:
        for query, params in cur.executed_queries:
            if "UPDATE sourced_candidates" in query and params and "reject_reason_change_count" in str(params[0]):
                payloads.append(json.loads(params[0]))
    return payloads


@pytest.mark.anyio
async def test_first_change_is_allowed_and_recorded_as_one(monkeypatch):
    """A first-ever reason CHANGE (not a first rejection) must be allowed and
    must patch reject_reason_change_count to 1."""
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    payloads = _count_patch_payloads(fake_conn)
    assert payloads == [{"reject_reason_change_count": 1}]


@pytest.mark.anyio
async def test_second_change_is_allowed_and_recorded_as_two(monkeypatch):
    """Still under MAX_REJECT_REASON_CHANGES (2) — allowed, and the count
    advances from 1 to 2."""
    assert MAX_REJECT_REASON_CHANGES == 2
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ONE_CHANGE_ALREADY,
    )

    res = await _reject(cand_module, "Domain experience mismatch")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 1
    payloads = _count_patch_payloads(fake_conn)
    assert payloads == [{"reject_reason_change_count": 2}]


@pytest.mark.anyio
async def test_third_change_is_blocked_with_422(monkeypatch):
    """At MAX_REJECT_REASON_CHANGES already — a third distinct reason must be
    rejected outright: no JobDiva note, no commit, and the already-executed
    row-lock UPDATE must be rolled back rather than left to commit."""
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_TWO_CHANGES_ALREADY,
    )

    with pytest.raises(HTTPException) as exc_info:
        await _reject(cand_module, "Overqualified for the role")

    assert exc_info.value.status_code == 422
    assert str(MAX_REJECT_REASON_CHANGES) in exc_info.value.detail
    assert len(jobdiva_calls) == 0
    assert fake_conn.rolled_back is True
    assert fake_conn.committed is False
    # No count-patch UPDATE either — the request was blocked before that point.
    assert _count_patch_payloads(fake_conn) == []


@pytest.mark.anyio
async def test_reconfirming_same_reason_at_the_limit_is_still_allowed(monkeypatch):
    """Re-confirming the SAME reason that's already on file is not a
    "change" at all (reject_reason_unchanged, not is_rejection_reason_change)
    — must succeed even when reject_reason_change_count is already at the
    limit, and must not advance the count further."""
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_TWO_CHANGES_ALREADY,
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["status"] == "success"
    assert len(jobdiva_calls) == 0
    assert _count_patch_payloads(fake_conn) == []


@pytest.mark.anyio
async def test_fallback_path_reads_the_limit_from_the_highest_duplicate_count(monkeypatch):
    """PR #874 review (medium): the fallback path's count-patch below always
    writes the SAME incremented value to every matched duplicate row, so the
    prior count must be read as the MAX across all of them — not just the
    single most-recently-updated "current" row locked_prev_data uses for the
    reason-change decision. A stale duplicate sitting on a LOWER count than
    the current row must not let the limit be bypassed."""
    _fake_row_dup_current_low_count = (
        1, "21562841721070", "26-12137",
        {
            "feedback_type": "Reject", "feedback_reason": "Communication skills",
            "feedback_synced": True, "reject_reason_change_count": 0,
        },
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
        datetime(2026, 1, 1),
    )
    _fake_row_dup_stale_high_count = (
        99999999, "21562841721070", "26-12137",
        {
            "feedback_type": "Reject", "feedback_reason": "Old stale reason from a different duplicate",
            "feedback_synced": True, "reject_reason_change_count": 2,
        },
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
        datetime(2025, 1, 1),
    )
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=None,
        select_rows=[_fake_row_dup_stale_high_count, _fake_row_dup_current_low_count],
    )

    # The "current" row (lower id, most recently updated) only shows a count
    # of 0 — but a stale duplicate already shows 2. Trusting only the current
    # row's count would wrongly allow this change; the max across both must
    # block it instead.
    with pytest.raises(HTTPException) as exc_info:
        await _reject(cand_module, "Overqualified for the role")

    assert exc_info.value.status_code == 422
    assert len(jobdiva_calls) == 0


@pytest.mark.anyio
async def test_fallback_path_also_enforces_the_limit(monkeypatch):
    """The candidate_id/jobdiva_id fallback (no sourced_row_id pin) must
    enforce the same limit, reading the count off the most-recently-updated
    duplicate row — same row `_reject_prior` itself is read from."""
    _fake_row_dup_two_changes = (
        1, "21562841721070", "26-12137",
        {
            "feedback_type": "Reject", "feedback_reason": "Communication skills",
            "feedback_synced": True, "reject_reason_change_count": 2,
        },
        "33087136", "Srinivasan Subramanian", "Senior Java Developer", "Pyramid Consulting",
        datetime(2026, 1, 1),
    )
    cand_module, jobdiva_calls, fake_conn = _base_setup(
        monkeypatch, select_row=None, select_rows=[_fake_row_dup_two_changes],
    )

    with pytest.raises(HTTPException) as exc_info:
        await _reject(cand_module, "Overqualified for the role")

    assert exc_info.value.status_code == 422
    assert len(jobdiva_calls) == 0
    assert fake_conn.rolled_back is True


# --- response reject_reason_change_count (PR #876 review) ------------------
# Without this, the frontend's "N remaining" button/banner reads stale until
# the next reload — it needs the count AFTER this save, not just whatever it
# fetched before the recruiter opened the modal.

@pytest.mark.anyio
async def test_response_reports_zero_on_a_first_ever_rejection(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)

    res = await _reject(cand_module, "Skills do not meet requirements")

    assert res["reject_reason_change_count"] == 0


@pytest.mark.anyio
async def test_response_reports_one_after_the_first_change(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ALREADY_REJECTED,
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["reject_reason_change_count"] == 1


@pytest.mark.anyio
async def test_response_reports_two_after_the_second_change(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ONE_CHANGE_ALREADY,
    )

    res = await _reject(cand_module, "Domain experience mismatch")

    assert res["reject_reason_change_count"] == 2


@pytest.mark.anyio
async def test_response_reports_the_unchanged_count_on_a_same_reason_reconfirm(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(
        monkeypatch, select_row=_FAKE_CAND_ROW_ONE_CHANGE_ALREADY,
    )

    res = await _reject(cand_module, "Communication skills")

    assert res["reject_reason_change_count"] == 1


@pytest.mark.anyio
async def test_response_omits_the_count_for_non_reject_feedback(monkeypatch):
    cand_module, _jobdiva_calls, _fake_conn = _base_setup(monkeypatch)
    user = UserIdentity(email="recruiter@pyramidci.com", role="recruiter")
    request = CandidateFeedbackRequest(feedback_type="Unreachable")

    res = await cand_module.save_candidate_feedback(
        job_id_or_ref="26-12137", candidate_id="55148489",
        request=request, background_tasks=BackgroundTasks(), user=user,
    )

    assert res["reject_reason_change_count"] is None
