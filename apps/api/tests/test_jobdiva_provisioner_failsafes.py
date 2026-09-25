"""Provisioner fail-safes: a JobDiva-sourced person can never be re-created.

Drives ``_provision_batch_to_jobdiva`` end to end with the DB and the JobDiva
service faked, and pins the four walls around the profile-minting call:

1. a JobDiva row is linked on its OWN id with profile creation disallowed;
2. an id JobDiva returns that is not the person's own id is never persisted
   (the tripwire reports it and counts it as duplicate_suspected);
3. a JobDiva row the resolver failed to resolve is not provisioned at all;
4. provenance survives a renamed source label via the emitter stamp;
plus the applicant pre-check re-stamping a stale duplicate id.

It also pins the provenance stamps (services/jobdiva.py "Provenance"): the
profile id and ``jobdiva_profile_origin`` are person-level, while
``jobdiva_application_origin`` / ``jobdiva_provisioned_at`` / ``_from`` are
scoped to the job's rows -- and the `source` column is never touched.
"""
import asyncio
import logging
from typing import Any, Dict, List

import pytest

import routers.engagement as eng
from services.jobdiva import JOBDIVA_PROFILE_INVARIANT, JobDivaApplicationOutcome

OWN = "462058065251"
DUP = "999000111"
JOB = "26-1"
NOW = "2026-09-16T12:00:00+00:00"
JOB_KEYS = [JOB, "31920032", "unknown"]


def _pair_job(provisioned_from: str) -> Dict[str, Any]:
    return {
        "jobdiva_application_origin": "pair",
        "jobdiva_provisioned_at": NOW,
        "jobdiva_provisioned_from": provisioned_from,
    }


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
        "source": "JobDiva-JobAgent", "headline": "Analytical Engine Programmer",
    }
    row.update(over)
    return row


@pytest.fixture
def harness(monkeypatch):
    persisted: List[Dict[str, Any]] = []

    async def _job_ids(_job):
        return 31920032, JOB

    def _record_link_state(cid, job_keys, *, person_delta=None, job_delta=None):
        persisted.append({
            "candidate_id": cid, "job_keys": list(job_keys),
            "person": person_delta, "job": job_delta,
        })

    reread_calls: List[str] = []

    def run(rows, fake, reread=None):
        # Never a real Unipile read from a test (see _provisioning_resume):
        # the LinkedIn re-read answers `reread` (a normalised profile) or {}.
        async def _linkedin_reread(row, _data):
            reread_calls.append(str(row.get("candidate_id")))
            return dict(reread or {})

        monkeypatch.setattr(eng, "_fetch_linkedin_profile_for_resume", _linkedin_reread)
        monkeypatch.setattr(eng, "get_db_connection", lambda: _Conn(rows))
        monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
        monkeypatch.setattr(eng, "jobdiva_service", fake)
        monkeypatch.setattr(eng, "_utc_now_iso", lambda: NOW)
        monkeypatch.setattr(eng, "_persist_jobdiva_link_state", _record_link_state)
        return asyncio.run(eng._provision_batch_to_jobdiva([r["candidate_id"] for r in rows], JOB))

    run.persisted = persisted  # type: ignore[attr-defined]
    run.reread_calls = reread_calls  # type: ignore[attr-defined]
    return run


def test_jobdiva_row_links_on_own_id_with_creation_disallowed(harness):
    fake = _FakeJobDiva(result=(True, OWN))
    results = harness([_row()], fake)

    assert results["success"] == 1 and results["duplicate_suspected"] == 0
    (call,) = fake.calls
    assert call["candidate_id"] == OWN
    assert call["allow_profile_creation"] is False
    assert call["origin_source"] == "JobDiva-JobAgent"
    assert harness.persisted == [{
        "candidate_id": OWN, "job_keys": JOB_KEYS,
        # A JobDiva-sourced person's profile pre-existed; PAIR filed this job's application.
        "person": {"jobdiva_candidate_id": OWN, "jobdiva_profile_origin": "jobdiva"},
        "job": _pair_job("JobDiva-JobAgent"),
    }]


def test_unexpected_profile_id_is_never_persisted_and_is_reported(harness, caplog):
    """JobDiva answers with a stranger's id -> keep the person's own id, shout."""
    caplog.set_level(logging.ERROR)
    fake = _FakeJobDiva(result=(True, int(DUP)))
    results = harness([_row()], fake)

    assert results["success"] == 1
    assert results["duplicate_suspected"] == 1
    assert harness.persisted[0]["person"] == {"jobdiva_candidate_id": OWN, "jobdiva_profile_origin": "jobdiva"}
    assert harness.persisted[0]["job"] == _pair_job("JobDiva-JobAgent")
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
    # The origin channel travels to the service so JobDiva files the application
    # under PAIR's Resume Source for that channel.
    assert call["origin_source"] == "LinkedIn-Exa"
    # Persisted as text, like the save path stores it (JobDiva returns an int64).
    # PAIR minted this profile AND filed this job's application; the row's
    # `source` (LinkedIn-Exa) is not part of either delta -- origin is immutable.
    assert harness.persisted == [{
        "candidate_id": "exa_linkedin.com/in/ada", "job_keys": JOB_KEYS,
        "person": {"jobdiva_candidate_id": "777", "jobdiva_profile_origin": "pair"},
        "job": _pair_job("LinkedIn-Exa"),
    }]


def test_jobdiva_row_without_link_id_fails_closed(harness, caplog, monkeypatch):
    """Simulates a regression of the resolver (PR #493 shape): the JobDiva row
    must not reach the service at all."""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(eng, "_resolve_link_candidate_id", lambda *a, **k: None)
    fake = _FakeJobDiva()
    results = harness([_row()], fake)

    assert fake.calls == []
    assert results == {
        "success": 0, "skipped": 0, "failed": 1, "duplicate_suspected": 0, "blank_profile_refused": 0,
    }
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
    # Already an applicant: nothing is known about who filed that application
    # for a JobAgent row, so no job-level stamp -- only the person-level facts.
    assert harness.persisted == [{
        "candidate_id": OWN, "job_keys": JOB_KEYS,
        "person": {"jobdiva_candidate_id": OWN, "jobdiva_profile_origin": "jobdiva"},
        "job": None,
    }]


def test_stale_duplicate_id_is_replaced_by_own_id_on_link(harness, caplog):
    caplog.set_level(logging.WARNING)
    fake = _FakeJobDiva(result=(True, OWN))
    harness([_row(data={"jobdiva_candidate_id": DUP})], fake)

    (call,) = fake.calls
    assert call["candidate_id"] == OWN  # own id beats the stored duplicate
    assert harness.persisted[0]["person"] == {"jobdiva_candidate_id": OWN, "jobdiva_profile_origin": "jobdiva"}
    assert "likely a duplicate profile" in caplog.text


# ---------------------------------------------------------------------------
# Provenance stamps: origin is never touched, linkage is recorded per scope
# ---------------------------------------------------------------------------

def test_create_outcome_marks_profile_and_application_as_pair_made(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    harness([_row(candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa")], fake)

    (rec,) = harness.persisted
    assert rec["person"] == {"jobdiva_candidate_id": "777", "jobdiva_profile_origin": "pair"}
    assert rec["job"] == _pair_job("LinkedIn-Exa")


def test_id_recovered_by_lookup_is_not_claimed_as_a_pair_minted_profile(harness):
    """The create response carried no id and searchCandidateProfile found one:
    the application is PAIR's, but that profile may have pre-existed."""
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 4242, path="created", found_via_search=True))
    harness([_row(candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa")], fake)

    (rec,) = harness.persisted
    assert rec["person"] == {"jobdiva_candidate_id": "4242", "jobdiva_profile_origin": "jobdiva"}
    assert rec["job"] == _pair_job("LinkedIn-Exa")


def test_lookup_link_keeps_an_existing_profile_origin_stamp(harness):
    """A stored `pair` profile-origin (this profile was minted on an earlier
    launch) is not overwritten when a later launch merely re-links via lookup."""
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, "777", path="linked", found_via_search=True))
    harness([_row(
        candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa",
        data={"jobdiva_candidate_id": "777", "jobdiva_profile_origin": "pair"},
    )], fake)

    (rec,) = harness.persisted
    assert rec["person"] == {"jobdiva_candidate_id": "777"}
    assert rec["job"] == _pair_job("LinkedIn-Exa")


def test_application_without_returned_id_still_records_pair_provenance(harness):
    """JobDiva made the application but sent no id and nothing could be recovered:
    the job-level stamp is what the applicant-origin audit / re-provision find."""
    fake = _FakeJobDiva(result=(True, None))
    results = harness([_row(candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa")], fake)

    assert results["success"] == 1
    assert harness.persisted == [{
        "candidate_id": "exa_linkedin.com/in/ada", "job_keys": JOB_KEYS,
        "person": None, "job": _pair_job("LinkedIn-Exa"),
    }]


def test_applicant_row_already_in_jobdiva_is_marked_organic(harness):
    """A JobDiva-Applicants row that JobDiva already lists as an applicant: the
    application is organic by definition; the profile pre-existed."""
    fake = _FakeJobDiva(applicants=[{"CANDIDATEID": OWN, "EMAIL": "ada@example.com", "PHONE": ""}])
    results = harness([_row(source="JobDiva-Applicants", data={"jobdiva_candidate_id": OWN})], fake)

    assert results["skipped"] == 1 and fake.calls == []
    assert harness.persisted == [{
        "candidate_id": OWN, "job_keys": JOB_KEYS,
        "person": {"jobdiva_profile_origin": "jobdiva"},
        "job": {"jobdiva_application_origin": "organic"},
    }]


def test_exa_row_already_an_applicant_learns_the_id_but_not_who_applied(harness):
    """An Exa person JobDiva already lists as an applicant (matched by email):
    the id is stamped, but whether PAIR filed that application on an earlier
    launch or the person applied themselves is unknown -- and stays unstamped."""
    # (example.com is a placeholder domain to the email index, so a real one here.)
    fake = _FakeJobDiva(applicants=[{"CANDIDATEID": "777", "EMAIL": "ada@lovelace.dev", "PHONE": ""}])
    results = harness([_row(
        candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa", email="ada@lovelace.dev", phone="",
    )], fake)

    assert results["skipped"] == 1 and fake.calls == []
    assert harness.persisted == [{
        "candidate_id": "exa_linkedin.com/in/ada", "job_keys": JOB_KEYS,
        "person": {"jobdiva_candidate_id": "777"}, "job": None,
    }]


def test_link_state_writes_person_and_job_scopes_separately(monkeypatch):
    """Person-level keys go on every row of the candidate; job-level keys only on
    this job's rows -- an application exists per job."""
    executed: List[Any] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params):
            executed.append((" ".join(sql.split()), params))

    class _C:
        committed = False

        def cursor(self):
            return _Cur()

        def commit(self):
            self.committed = True

        def close(self):
            pass

    conn = _C()
    monkeypatch.setattr(eng, "get_db_connection", lambda: conn)
    eng._persist_jobdiva_link_state(
        "exa_x", ["26-1", "123"],
        person_delta={"jobdiva_candidate_id": "777"},
        job_delta={"jobdiva_application_origin": "pair"},
    )
    assert conn.committed is True
    assert len(executed) == 2
    person_sql, person_params = executed[0]
    job_sql, job_params = executed[1]
    assert "WHERE candidate_id = %s" in person_sql and "jobdiva_id" not in person_sql
    assert person_params[1] == "exa_x"
    assert "AND jobdiva_id = ANY(%s)" in job_sql
    assert job_params[1:] == ("exa_x", ["26-1", "123"])
    # Neither statement can touch the origin label (only `data` is assigned).
    import re
    for sql in (person_sql, job_sql):
        assert re.search(r"SET\s+data\s*=", sql) and not re.search(r"\bsource\s*=", sql), sql


# ---------------------------------------------------------------------------
# Blank-profile fix (2026-09-25): what a created JobDiva profile is built from
# ---------------------------------------------------------------------------

from services.profile_resume import normalize_linkedin_profile  # noqa: E402

_LINKEDIN_PROFILE = normalize_linkedin_profile({
    "first_name": "Ada", "last_name": "Lovelace", "headline": "Principal Data Engineer",
    "summary": "Builds data platforms.",
    "work_experience": [{"position": "Principal Data Engineer", "company": "Acme Bank", "start": "1/2021",
                         "description": "Led the lakehouse migration."}],
    "education": [{"school": "Stevens Institute of Technology", "degree": "MS"}],
    "skills": ["Python", "Spark"],
})


def _linkedin_row(**over):
    row = _row(
        candidate_id="unipile_AEMAA1", source="LinkedIn-Unipile", email="ada@lovelace.dev",
        headline="Principal Data Engineer", location="Jersey City, New Jersey, United States",
        profile_url="https://www.linkedin.com/in/ada-lovelace", resume_text="",
        data={"linkedin_profile": _LINKEDIN_PROFILE},
    )
    row.update(over)
    return row


def test_linkedin_row_is_created_from_a_full_resume_file_and_address(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    results = harness([_linkedin_row()], fake)

    assert results["success"] == 1
    (call,) = fake.calls
    assert call["candidate_id"] is None and call["allow_profile_creation"] is True
    assert (call["first_name"], call["last_name"]) == ("Ada", "Lovelace")
    # A real Word document goes to JobDiva, not just a text field.
    assert call["resume_file"][:2] == b"PK"
    assert call["filename"] == "Ada_Lovelace_Resume.docx"
    text = call["resume_text"]
    for expected in ("Ada Lovelace", "Email: ada@lovelace.dev", "Phone: 5551234567",
                     "LinkedIn: https://www.linkedin.com/in/ada-lovelace", "PROFESSIONAL EXPERIENCE",
                     "Principal Data Engineer at Acme Bank", "Led the lakehouse migration.",
                     "Stevens Institute of Technology", "Python, Spark"):
        assert expected in text, expected
    assert "(Profile sourced via PAIR)" not in text
    assert call["profile_fields"] == {"city": "Jersey City", "state": "NJ", "countryid": "US"}
    # The rest of what LinkedIn gives: the profile link for JobDiva's LinkedIn field.
    assert call["social_links"] == {"LinkedIn": "https://www.linkedin.com/in/ada-lovelace"}
    assert call["alternate_email"] == ""
    # A full profile needs no LinkedIn re-read.
    assert harness.reread_calls == []


def test_name_only_row_is_refused_instead_of_creating_a_blank_profile(harness, caplog):
    caplog.set_level(logging.ERROR)
    fake = _FakeJobDiva(result=(True, 777))
    results = harness([_row(candidate_id="exa_linkedin.com/in/x", source="LinkedIn-Exa", headline="")], fake)

    assert fake.calls == []
    assert results["failed"] == 1 and results["blank_profile_refused"] == 1
    assert eng.JOBDIVA_BLANK_PROFILE_REFUSED in caplog.text and "no_profile_data" in caplog.text
    assert harness.persisted == []  # not provisioned: re-provision retries it


def test_placeholder_name_is_refused(harness, caplog):
    caplog.set_level(logging.ERROR)
    fake = _FakeJobDiva(result=(True, 777))
    results = harness([_linkedin_row(name="LinkedIn Candidate", data={})], fake)

    assert fake.calls == []
    assert results["blank_profile_refused"] == 1
    assert "no_usable_name" in caplog.text


def test_placeholder_name_is_rescued_by_the_linkedin_profile_name(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    harness([_linkedin_row(name="Principal Data Engineer | Spark")], fake)

    (call,) = fake.calls
    assert (call["first_name"], call["last_name"]) == ("Ada", "Lovelace")


def test_thin_row_rereads_the_linkedin_profile_before_creating(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    thin = _row(candidate_id="exadeep_1_ada", source="LinkedIn-DeepSearch", headline="Data Engineer",
                profile_url="https://www.linkedin.com/in/ada-lovelace",
                resume_text="Strong fit because of Spark", data={})
    harness([thin], fake, reread=_LINKEDIN_PROFILE)

    assert harness.reread_calls == ["exadeep_1_ada"]
    (call,) = fake.calls
    assert "Principal Data Engineer at Acme Bank" in call["resume_text"]
    assert "Strong fit" not in call["resume_text"]  # the agent's rationale is not a résumé


def test_linkedin_rereads_are_capped_per_batch(harness, monkeypatch):
    monkeypatch.setattr(eng, "JOBDIVA_PROVISION_LINKEDIN_REFETCH_CAP", 1)
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    rows = [
        _row(candidate_id=f"exadeep_{i}", source="LinkedIn-DeepSearch", headline="Data Engineer",
             email=f"a{i}@lovelace.dev", phone=f"555123456{i}", data={})
        for i in range(3)
    ]
    results = harness(rows, fake)

    assert len(harness.reread_calls) == 1
    # Thin but not blank (name + headline): still created, just without a re-read.
    assert results["success"] == 3


def test_linkedin_reread_can_be_switched_off(harness, monkeypatch):
    monkeypatch.setattr(eng, "JOBDIVA_PROVISION_LINKEDIN_REFETCH", False)
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    harness([_row(candidate_id="exadeep_1", source="LinkedIn-DeepSearch", headline="Data Engineer")], fake)

    assert harness.reread_calls == []
    assert len(fake.calls) == 1


def test_link_path_builds_no_resume_and_never_rereads(harness):
    fake = _FakeJobDiva(result=(True, OWN))
    harness([_row()], fake)

    (call,) = fake.calls
    assert call["candidate_id"] == OWN
    assert call["resume_text"] == "" and "resume_file" not in call
    assert harness.reread_calls == []


def test_profile_jobdiva_matched_to_an_existing_person_is_not_claimed_as_pair_minted(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 555, path="created", matched_existing=True))
    harness([_linkedin_row()], fake)

    (rec,) = harness.persisted
    assert rec["person"] == {"jobdiva_candidate_id": "555", "jobdiva_profile_origin": "jobdiva"}
    assert rec["job"] == _pair_job("LinkedIn-Unipile")


def test_synthetic_lookup_email_goes_to_the_service_but_not_onto_the_resume(harness):
    fake = _FakeJobDiva(result=JobDivaApplicationOutcome(True, 777, path="created"))
    harness([_linkedin_row(email="")], fake)

    (call,) = fake.calls
    assert call["email"] == "pair-5551234567@no-email.jobdiva.local"
    assert "no-email.jobdiva.local" not in call["resume_text"]
