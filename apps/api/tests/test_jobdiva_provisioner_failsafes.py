"""Provisioner fail-safes: a JobDiva-sourced person can never be re-created.

Drives ``_provision_batch_to_jobdiva`` end to end with the DB and the JobDiva
service faked, and pins the four walls around the profile-minting call:

1. a JobDiva row is linked on its OWN id with profile creation disallowed;
2. an id JobDiva returns that is not the person's own id is never persisted
   (the tripwire reports it and counts it as duplicate_suspected);
3. a JobDiva row the resolver failed to resolve is not provisioned at all;
4. provenance survives a renamed source label via the emitter stamp;
plus the applicant pre-check re-stamping a stale duplicate id.
"""
import asyncio
import logging
from typing import Any, Dict, List

import pytest

import routers.engagement as eng
from services.jobdiva import JOBDIVA_PROFILE_INVARIANT

OWN = "462058065251"
DUP = "999000111"
JOB = "26-1"


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self, **_kw):
        return _Cursor(self._rows)

    def close(self):
        pass


class _FakeJobDiva:
    def __init__(self, applicants=None, result=(True, OWN)):
        self.applicants = applicants or []
        self.result = result
        self.calls: List[Dict[str, Any]] = []

    async def get_job_applicants_detail(self, _job_id):
        return self.applicants

    async def create_job_application_with_resume(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _row(**over):
    row = {
        "candidate_id": OWN, "name": "Ada Lovelace", "email": "ada@example.com",
        "phone": "5551234567", "resume_text": "resume", "data": {}, "jobdiva_id": JOB,
        "source": "JobDiva-JobAgent",
    }
    row.update(over)
    return row


@pytest.fixture
def harness(monkeypatch):
    persisted: List[Dict[str, Any]] = []

    async def _job_ids(_job):
        return 31920032, JOB

    def run(rows, fake):
        monkeypatch.setattr(eng, "get_db_connection", lambda: _Conn(rows))
        monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
        monkeypatch.setattr(eng, "jobdiva_service", fake)
        monkeypatch.setattr(
            eng, "_persist_jobdiva_candidate_id",
            lambda cid, delta: persisted.append({"candidate_id": cid, **delta}),
        )
        return asyncio.run(eng._provision_batch_to_jobdiva([r["candidate_id"] for r in rows], JOB))

    run.persisted = persisted  # type: ignore[attr-defined]
    return run


def test_jobdiva_row_links_on_own_id_with_creation_disallowed(harness):
    fake = _FakeJobDiva(result=(True, OWN))
    results = harness([_row()], fake)

    assert results["success"] == 1 and results["duplicate_suspected"] == 0
    (call,) = fake.calls
    assert call["candidate_id"] == OWN
    assert call["allow_profile_creation"] is False
    assert harness.persisted == [{"candidate_id": OWN, "jobdiva_candidate_id": OWN}]


def test_unexpected_profile_id_is_never_persisted_and_is_reported(harness, caplog):
    """JobDiva answers with a stranger's id -> keep the person's own id, shout."""
    caplog.set_level(logging.ERROR)
    fake = _FakeJobDiva(result=(True, int(DUP)))
    results = harness([_row()], fake)

    assert results["success"] == 1
    assert results["duplicate_suspected"] == 1
    assert harness.persisted == [{"candidate_id": OWN, "jobdiva_candidate_id": OWN}]
    assert JOBDIVA_PROFILE_INVARIANT in caplog.text
    assert "unexpected_profile_id" in caplog.text
    assert DUP in caplog.text and OWN in caplog.text


def test_non_jobdiva_row_may_create_a_profile(harness):
    fake = _FakeJobDiva(result=(True, 777))
    results = harness([_row(candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa")], fake)

    assert results["success"] == 1 and results["duplicate_suspected"] == 0
    (call,) = fake.calls
    assert call["candidate_id"] is None
    assert call["allow_profile_creation"] is True
    # Persisted as text, like the save path stores it (JobDiva returns an int64).
    assert harness.persisted == [{"candidate_id": "exa_linkedin.com/in/ada", "jobdiva_candidate_id": "777"}]


def test_jobdiva_row_without_link_id_fails_closed(harness, caplog, monkeypatch):
    """Simulates a regression of the resolver (PR #493 shape): the JobDiva row
    must not reach the service at all."""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(eng, "_resolve_link_candidate_id", lambda *a, **k: None)
    fake = _FakeJobDiva()
    results = harness([_row()], fake)

    assert fake.calls == []
    assert results == {"success": 0, "skipped": 0, "failed": 1, "duplicate_suspected": 0}
    assert JOBDIVA_PROFILE_INVARIANT in caplog.text
    assert "jobdiva_row_without_link_id" in caplog.text


def test_renamed_source_label_still_links_via_emitter_stamp(harness):
    """`source` no longer starts with JobDiva, but the stamp (stored id == own id)
    proves provenance -> still linked, still no profile creation."""
    fake = _FakeJobDiva(result=(True, OWN))
    results = harness([_row(source="PAIR-Agent", data={"jobdiva_candidate_id": OWN})], fake)

    assert results["success"] == 1
    (call,) = fake.calls
    assert call["candidate_id"] == OWN
    assert call["allow_profile_creation"] is False


def test_stale_duplicate_id_is_restamped_when_person_is_already_an_applicant(harness):
    """Stored id points at the duplicate profile; JobDiva lists the real profile as
    the applicant -> skipped, and the row is re-pointed at its own id."""
    fake = _FakeJobDiva(applicants=[{"CANDIDATEID": OWN, "EMAIL": "other@example.com", "PHONE": ""}])
    results = harness([_row(data={"jobdiva_candidate_id": DUP})], fake)

    assert results["skipped"] == 1 and fake.calls == []
    assert harness.persisted == [{"candidate_id": OWN, "jobdiva_candidate_id": OWN}]


def test_stale_duplicate_id_is_replaced_by_own_id_on_link(harness, caplog):
    caplog.set_level(logging.WARNING)
    fake = _FakeJobDiva(result=(True, OWN))
    harness([_row(data={"jobdiva_candidate_id": DUP})], fake)

    (call,) = fake.calls
    assert call["candidate_id"] == OWN  # own id beats the stored duplicate
    assert harness.persisted == [{"candidate_id": OWN, "jobdiva_candidate_id": OWN}]
    assert "likely a duplicate profile" in caplog.text
