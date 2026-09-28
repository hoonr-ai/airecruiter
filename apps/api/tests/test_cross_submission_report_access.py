"""The screen report a cross submission links to must open for the new job's team.

Cross submissions (services/cross_submissions.py) email and list, for a NEW
job, candidates PAIR screened for OTHER jobs, each linked to the PRIOR job's
report (``/jobs/<prior ref>/report?candidateId=<id>``). The recipients — the
new job's recruiters — are usually not assigned to the prior job, so the
report endpoint's job-access check 403'd those links ("the report does not
come"). ``_cross_submission_report_grant`` lets access to a job whose
cross-submission list carries the candidate from that prior job read the
report, and nothing else.
"""
import asyncio

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from core.auth import UserIdentity
from routers import candidates as cr
from services import cross_submissions as cs

RECRUITER = UserIdentity(email="new-job-rec@pyramidci.com", role="recruiter")


def _denied(*_a, **_k):
    raise HTTPException(status_code=403, detail="Access denied.")


def _access_only_to(*refs):
    def check(ref, _user, *_a, **_k):
        if ref not in refs:
            _denied()
    return check


def test_grant_returns_the_first_accessible_sharing_job(monkeypatch):
    monkeypatch.setattr(cs, "jobs_sharing_screen_report", lambda prior, cid: ["26-33333", "26-22222"])
    monkeypatch.setattr(cr, "_verify_job_access_by_id", _access_only_to("26-22222"))
    assert cr._cross_submission_report_grant("26-11111", "c1", RECRUITER) == "26-22222"


def test_grant_denies_when_no_sharing_job_is_accessible(monkeypatch):
    monkeypatch.setattr(cs, "jobs_sharing_screen_report", lambda prior, cid: ["26-33333"])
    monkeypatch.setattr(cr, "_verify_job_access_by_id", _denied)
    assert cr._cross_submission_report_grant("26-11111", "c1", RECRUITER) is None


def test_grant_fails_closed_when_the_lookup_breaks(monkeypatch):
    def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(cs, "jobs_sharing_screen_report", boom)
    monkeypatch.setattr(cr, "_verify_job_access_by_id", lambda *_a, **_k: None)
    assert cr._cross_submission_report_grant("26-11111", "c1", RECRUITER) is None


def _report(job_id="26-11111", candidate_id="c1"):
    return asyncio.run(cr.get_candidate_evaluation_report(user=RECRUITER, job_id=job_id, q_candidate_id=candidate_id))


def test_report_without_job_access_or_share_is_still_403(monkeypatch):
    monkeypatch.setattr(cr, "_verify_job_access_by_id", _denied)
    monkeypatch.setattr(cs, "jobs_sharing_screen_report", lambda prior, cid: [])
    db = MagicMock(side_effect=AssertionError("must not read the report"))
    monkeypatch.setattr(cr, "get_db_connection", db)
    with pytest.raises(HTTPException) as exc:
        _report()
    assert exc.value.status_code == 403
    db.assert_not_called()


def test_shared_report_reads_only_the_prior_jobs_row(monkeypatch):
    """Past the guard via the share — and no 'any row for this candidate' fallback."""
    monkeypatch.setattr(cr, "_verify_job_access_by_id", _access_only_to("26-22222"))
    monkeypatch.setattr(cs, "jobs_sharing_screen_report", lambda prior, cid: ["26-22222"])
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None  # the candidate has no row on the prior job
    monkeypatch.setattr(cr, "get_db_connection", lambda: conn)
    with pytest.raises(HTTPException) as exc:
        _report()
    assert exc.value.status_code == 404
    sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
    assert len(sqls) == 1 and "JOIN monitored_jobs" in sqls[0]
