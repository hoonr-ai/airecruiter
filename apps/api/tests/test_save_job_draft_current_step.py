"""routers/jobs.save_job_draft: current_step must only use GREATEST() on the
auto-save path.

Before this fix, GREATEST(current_step, %s) applied to manual saves too, so a
deliberate step change back (e.g. the wizard's step-indicator back-navigation)
was silently clamped to the highest step ever reached. A manual save must set
current_step directly; only a stray/out-of-order auto-save should be guarded
with GREATEST so it can't move the step backwards. Pinned per PR #749
reviewer feedback.
"""
import asyncio
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

from core.auth import UserIdentity
from models import JobDraftData
from routers.jobs import save_job_draft

USER = UserIdentity(email="recruiter@example.com", role="recruiter")

# Index of the (is_auto_saved, greatest_bound, direct_value) params bound to
# the UPDATE's `current_step = CASE WHEN %s THEN GREATEST(current_step, %s)
# ELSE %s END` clause (see the column order in jobs.py's save_job_draft UPDATE).
_CURRENT_STEP_CASE_PARAM_INDEX = 12


def _mock_connection():
    """A mocked pooled connection: the ref-code SELECT returns a row, and the
    UPDATE reports one row affected (so the INSERT fallback is skipped)."""
    cur = MagicMock()
    cur.fetchone.return_value = ("26-00001",)
    cur.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.__enter__.return_value = conn
    return conn, cur


def _save(*, is_auto_saved: bool, current_step: int):
    conn, cur = _mock_connection()
    draft = JobDraftData(job_id="123456", current_step=current_step, is_auto_saved=is_auto_saved)
    with patch("routers.jobs._verify_job_access_by_id", lambda *a, **k: None), \
         patch("routers.jobs._ensure_user_in_recruiter_emails", lambda *a, **k: None), \
         patch("routers.jobs.stamp_job_posted_by"), \
         patch("routers.jobs.get_db_connection", return_value=conn):
        asyncio.run(save_job_draft(
            job_id="123456",
            draft_data=draft,
            background_tasks=BackgroundTasks(),
            user=USER,
        ))
    return cur


def _update_calls(cur):
    return [c for c in cur.execute.call_args_list if "UPDATE monitored_jobs" in c.args[0]]


def test_manual_save_sets_current_step_directly_even_backwards():
    cur = _save(is_auto_saved=False, current_step=1)
    updates = _update_calls(cur)
    assert len(updates) == 1
    sql, params = updates[0].args
    assert "GREATEST(current_step" in sql
    is_auto, greatest_bound, direct_value = params[
        _CURRENT_STEP_CASE_PARAM_INDEX:_CURRENT_STEP_CASE_PARAM_INDEX + 3
    ]
    assert is_auto is False
    # Manual save: current_step is set to the requested value directly,
    # regardless of GREATEST — a deliberate backwards step isn't clamped.
    assert direct_value == 1


def test_auto_save_uses_greatest_so_it_cannot_move_current_step_back():
    cur = _save(is_auto_saved=True, current_step=1)
    updates = _update_calls(cur)
    assert len(updates) == 1
    sql, params = updates[0].args
    is_auto, greatest_bound, direct_value = params[
        _CURRENT_STEP_CASE_PARAM_INDEX:_CURRENT_STEP_CASE_PARAM_INDEX + 3
    ]
    assert is_auto is True
    # Auto-save: bounded by GREATEST(current_step, 1) at the SQL level, so an
    # out-of-order/stray auto-save can never regress the stored step.
    assert greatest_bound == 1
