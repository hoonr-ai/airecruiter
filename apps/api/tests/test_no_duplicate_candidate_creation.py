"""Duplicate candidate creation must stay fixed after the provenance change.

Two kinds of duplicate, two sets of walls:

JobDiva side -- "a person never gets a second JobDiva profile" (see
docs/jobdiva-provisioning-invariants.md; the four original fail-safes are pinned in
tests/test_jobdiva_provisioner_failsafes.py and
tests/test_jobdiva_link_via_create_job_application.py). This file adds the cases
the provenance change introduces or relies on:

* an Exa/LinkedIn person Launch PAIR already provisioned is LINKED on every later
  job (their stamped profile id travels with the person), never re-created;
* the id-recovery lookup after an id-less create never triggers a second create.

Local side -- "a person has one row per job" (the "everyone became a JobDiva
applicant" twins; the full cycle is pinned in tests/test_applicant_sync_provenance.py):

* a shared / placeholder phone line never merges two different people;
* the PAIR-application marker only reads application-level fields, so a profile
  PAIR minted cannot make that person's later genuine application to another job
  look PAIR-filed (and vanish);
* the launched-keys endpoint exposes the stored profile id, so Step 5 hides the
  JobDiva-labelled copy of a launched person instead of offering a second launch
  (which would have saved a JobDiva-labelled twin row).
"""
import asyncio
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.candidates as cands
import routers.engagement as eng
import services.jobdiva as jd
from core.auth import UserIdentity, get_current_user
from services.auto_assign_service import AutoAssignService, _phone_index_keys
from tests.test_jobdiva_provisioner_failsafes import _Conn, _FakeJobDiva, _row  # noqa: F401

EXA_ID = "exa_linkedin.com/in/ada"
PROFILE = "777"


# ---------------------------------------------------------------------------
# JobDiva side
# ---------------------------------------------------------------------------

def _provision(monkeypatch, rows, fake, job="26-2"):
    persisted: List[Dict[str, Any]] = []

    async def _job_ids(_job):
        return 31920033, job

    monkeypatch.setattr(eng, "get_db_connection", lambda: _Conn(rows))
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
    monkeypatch.setattr(eng, "jobdiva_service", fake)
    monkeypatch.setattr(eng, "_utc_now_iso", lambda: "2026-09-17T00:00:00+00:00")
    monkeypatch.setattr(
        eng, "_persist_jobdiva_link_state",
        lambda cid, keys, person_delta=None, job_delta=None: persisted.append(
            {"candidate_id": cid, "person": person_delta, "job": job_delta}
        ),
    )
    results = asyncio.run(eng._provision_batch_to_jobdiva([r["candidate_id"] for r in rows], job))
    return results, persisted


def test_provisioned_exa_person_is_linked_not_recreated_on_a_later_job(monkeypatch):
    """Job A minted profile 777 and stamped it on the person's rows (person-level
    write). Launching the same person on job B must attach to 777 with profile
    creation disallowed -- the shape that used to mint a duplicate per job."""
    fake = _FakeJobDiva(result=jd.JobDivaApplicationOutcome(True, PROFILE, path="linked"))
    row = _row(
        candidate_id=EXA_ID, source="LinkedIn-Exa", jobdiva_id="26-2",
        data={"jobdiva_candidate_id": PROFILE, "jobdiva_profile_origin": "pair"},
    )
    results, persisted = _provision(monkeypatch, [row], fake)

    assert results["success"] == 1 and results["failed"] == 0
    (call,) = fake.calls
    assert call["candidate_id"] == PROFILE
    assert call["allow_profile_creation"] is False
    # Job B's application is PAIR's; the profile stamp is left as job A wrote it.
    (rec,) = persisted
    assert rec["person"] == {"jobdiva_candidate_id": PROFILE}
    assert rec["job"]["jobdiva_application_origin"] == "pair"
    assert rec["job"]["jobdiva_provisioned_from"] == "LinkedIn-Exa"


def test_provisioned_exa_person_already_an_applicant_on_the_job_is_skipped(monkeypatch):
    """Re-provision / re-launch of someone JobDiva already lists: no JobDiva call at all."""
    fake = _FakeJobDiva(applicants=[{"CANDIDATEID": PROFILE, "EMAIL": "ada@lovelace.dev", "PHONE": ""}])
    row = _row(candidate_id=EXA_ID, source="LinkedIn-Exa", data={"jobdiva_candidate_id": PROFILE})
    results, persisted = _provision(monkeypatch, [row], fake)

    assert results["skipped"] == 1 and fake.calls == []
    assert persisted == []  # nothing new learned, nothing rewritten


def test_id_recovery_never_creates_twice():
    """Empty create body -> one lookup -> no second CreateJobApplicationWithResume."""
    from tests.test_jobdiva_link_via_create_job_application import _FakeResponse, _run, _service, _calls_to

    def dispatch(url, body):
        if url.endswith("/searchCandidateProfile"):
            return _FakeResponse(200, json_data=[])
        if url.endswith("/CreateJobApplicationWithResume"):
            return _FakeResponse(200, text="")
        return _FakeResponse(200, text="true")

    outcome, calls = _run(
        _service(), dispatch,
        candidate_id=None, job_id="31920032", resume_text="r", email="ada@lovelace.dev", phone="5551234567",
    )
    assert outcome == (True, None)
    assert len(_calls_to(calls, "CreateJobApplicationWithResume")) == 1
    assert len(_calls_to(calls, "searchCandidateProfile")) == 2  # before and after, never a re-create


# ---------------------------------------------------------------------------
# Local side
# ---------------------------------------------------------------------------

def test_placeholder_phone_lines_never_merge_two_people():
    assert _phone_index_keys("0000000000") == []
    assert _phone_index_keys("555-555-5555") == []
    assert _phone_index_keys("1112221111") == []          # three distinct digits
    assert _phone_index_keys("5551234567") == ["5551234567"]
    assert _phone_index_keys("15551234567") == ["15551234567", "5551234567"]
    assert _phone_index_keys("123456") == []              # too short


def test_two_applicants_sharing_a_switchboard_stay_two_rows():
    svc = AutoAssignService()
    from tests.test_applicant_sync_provenance import _IndexCursor, _index_row

    agency_row = _index_row(id=9, candidate_id="exa_agency_person", email="a@agency.example", email_lc="a@agency.example",
                            jcid=None, data={}, phone="000-000-0000", phone_norm="0000000000")
    idx = svc._build_candidate_lookup_index(_IndexCursor([agency_row]), "26-1")
    assert "0000000000" not in idx["by_phone"]
    assert svc._find_in_index(idx, {"candidate_id": "42", "email": "b@agency.example", "phone": "0000000000"}, "42") is None


def test_pair_marker_ignores_candidate_level_source_and_owner(monkeypatch):
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_ID", 12)
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RECRUITER_ID", 4242)

    # Application-level fields are read...
    assert jd.jobdiva_application_meta({"RESUMESOURCE": 12}) == {"resume_source": "12"}
    assert jd.jobdiva_application_meta({"applicationSource": "PAIR", "RECRUITERID": "4242"}) == {
        "resume_source": "PAIR", "recruiter_id": "4242",
    }
    # ...candidate-level ones are not: a PAIR-minted profile carries them for life.
    assert jd.jobdiva_application_meta({"SOURCE": "PAIR", "OWNERID": "4242", "RECRUITER": "4242"}) == {}

    assert jd.jobdiva_application_created_by_pair({"resume_source": "12"}) is True
    assert jd.jobdiva_application_created_by_pair({"resume_source": "pair"}) is True
    assert jd.jobdiva_application_created_by_pair({"recruiter_id": "4242"}) is True
    assert jd.jobdiva_application_created_by_pair({"resume_source": "Career Portal"}) is False
    assert jd.jobdiva_application_created_by_pair({"recruiter_id": "1"}) is False
    assert jd.jobdiva_application_created_by_pair({}) is False
    assert jd.jobdiva_application_created_by_pair(None) is False


def test_pair_marker_is_inert_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_ID", 0)
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_IDS_BY_CHANNEL", "")
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RECRUITER_ID", 0)
    # Only the default name "PAIR" can still match; ids and recruiter cannot.
    assert jd.jobdiva_application_created_by_pair({"resume_source": "0"}) is False
    assert jd.jobdiva_application_created_by_pair({"recruiter_id": "0"}) is False
    assert jd.jobdiva_application_created_by_pair({"resume_source": "PAIR"}) is True


class _KeysCursor:
    def __init__(self, rows):
        self._rows = rows
        self._result: Any = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self._result = ("26-1", "123") if "monitored_jobs" in sql else self._rows
        self.sql = " ".join(sql.split())

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result


class _KeysConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self, **_kw):
        return _KeysCursor(self.rows)

    def close(self):
        pass


def test_launched_keys_expose_the_stored_profile_id(monkeypatch):
    rows = [
        (EXA_ID, "LinkedIn-Exa", PROFILE),   # provisioned Exa person
        ("555", "JobDiva-Applicants", None),  # organic applicant, no stamp
    ]
    monkeypatch.setattr(cands, "get_db_connection", lambda: _KeysConn(rows))
    monkeypatch.setattr(cands, "_verify_job_access_by_id", lambda *_a, **_k: None)
    app = FastAPI()
    app.include_router(cands.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="who@pyramidci.com", role="recruiter")

    res = TestClient(app).get("/jobs/26-1/launched-candidate-keys")
    assert res.status_code == 200, res.text
    assert res.json() == {
        "status": "success",
        "launched": [
            {"candidate_id": EXA_ID, "source": "LinkedIn-Exa", "jobdiva_candidate_id": PROFILE},
            {"candidate_id": "555", "source": "JobDiva-Applicants"},
        ],
    }
