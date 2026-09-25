"""JobDiva provisioning must ATTACH a known profile, never re-create it.

Pins the endpoint contract behind ``JobDivaService.create_job_application_with_resume``
to JobDiva's own Swagger v2 (https://api.jobdiva.com/swagger?group=Version%202):

* ``POST /apiv2/jobdiva/CreateJobApplicationWithResume`` -- body ``UploadResumeAndApplyJob``
  = {filecontent, filename, jobid, recruiterid, resumeDate, resumesource, textfile}.
  There is NO ``candidateid`` field: the call always parses the resume into a NEW
  profile and returns the new profile's id.
* ``POST /apiv2/jobdiva/createJobApplication`` -- body ``CreateJobApplicationDef``
  = {candidateid, jobid, dateapplied?, globalid?, resumesource?} -> boolean. Attaches an
  existing profile to a job and cannot create one.

Regression being pinned: the provisioner used to pass ``candidateid`` to the first
endpoint. JobDiva ignored it, so every Launch PAIR of a JobDiva-sourced person
(TalentSearch / JobAgent / Applicants) minted a duplicate profile -- and the
provisioner then persisted the duplicate's id over the real one.
"""
import asyncio
from unittest.mock import patch

from services.jobdiva import JobDivaService

# The exact UploadResumeAndApplyJob schema from JobDiva's Swagger v2.
UPLOAD_RESUME_AND_APPLY_JOB_FIELDS = {
    "filecontent", "filename", "jobid", "recruiterid", "resumeDate", "resumesource", "textfile",
}

JOB_ID = 31920032
EXISTING_PROFILE = "462058065251"


class _FakeResponse:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code = status
        self.text = text
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Records outbound calls; the test supplies the per-endpoint responses."""

    def __init__(self, calls, dispatch, *args, **kwargs):
        self._calls = calls
        self._dispatch = dispatch

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._calls.append({"method": "POST", "url": url, "json": json})
        return self._dispatch(url, json)

    async def get(self, url, params=None, headers=None, **kw):
        self._calls.append({"method": "GET", "url": url, "json": None, "params": params})
        return self._dispatch(url, None)


def _service():
    svc = JobDivaService()

    async def _auth(force_refresh=False):
        return "test-token"

    svc.authenticate = _auth  # type: ignore[assignment]
    return svc


def _run(svc, dispatch, **kwargs):
    calls = []

    def factory(*args, **kw):
        return _FakeAsyncClient(calls, dispatch)

    with patch("services.jobdiva.httpx.AsyncClient", factory):
        result = asyncio.run(svc.create_job_application_with_resume(**kwargs))
    return result, calls


def _calls_to(calls, endpoint):
    return [c for c in calls if c["url"].endswith(f"/apiv2/jobdiva/{endpoint}")]


def _dispatch_with(link_status=200, link_body="true", search_result=None, create_body="777"):
    def dispatch(url, body):
        if url.endswith("/createJobApplication"):
            return _FakeResponse(link_status, text=link_body)
        if url.endswith("/searchCandidateProfile"):
            return _FakeResponse(200, json_data=search_result or [])
        if url.endswith("/CreateJobApplicationWithResume"):
            return _FakeResponse(200, text=create_body)
        if url.endswith("/updateCandidateProfile"):
            return _FakeResponse(200, text="true")
        return _FakeResponse(200)
    return dispatch


# ---------------------------------------------------------------------------
# Known profile -> createJobApplication only
# ---------------------------------------------------------------------------

def test_known_profile_is_linked_via_create_job_application_and_never_recreated():
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), resume_text="resume",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    assert ok is True
    assert str(jd_id) == EXISTING_PROFILE

    link = _calls_to(calls, "createJobApplication")
    assert len(link) == 1
    assert link[0]["json"] == {"candidateid": int(EXISTING_PROFILE), "jobid": JOB_ID}
    # The profile-minting endpoint must not be touched for a known person...
    assert _calls_to(calls, "CreateJobApplicationWithResume") == []
    # ...and neither should the existing profile be renamed/re-emailed by PAIR.
    assert _calls_to(calls, "updateCandidateProfile") == []
    # Nothing to look up either: the id is already trusted.
    assert _calls_to(calls, "searchCandidateProfile") == []


def test_known_profile_link_failure_never_mints_a_profile():
    """JobDiva refuses the attach (5xx) and the email/phone lookup finds nobody:
    report failure. Creating a fresh profile here is exactly the duplicate."""
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(link_status=500, link_body="boom", search_result=[]),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), resume_text="resume",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    assert (ok, jd_id) == (False, None)
    assert _calls_to(calls, "CreateJobApplicationWithResume") == []
    assert _calls_to(calls, "updateCandidateProfile") == []


def test_known_profile_link_failure_with_boolean_false_body_is_a_refusal():
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(link_status=200, link_body="false", search_result=[]),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), email="ada@example.com",
    )
    assert (ok, jd_id) == (False, None)
    assert _calls_to(calls, "CreateJobApplicationWithResume") == []


def test_stale_known_profile_retries_only_with_a_different_matched_profile():
    """A stale id (profile merged away in JobDiva) may be swapped for the id the
    email/phone lookup returns -- but only when that id is different."""
    attempts = []

    def dispatch(url, body):
        if url.endswith("/createJobApplication"):
            attempts.append(body["candidateid"])
            # first (stale) id refused, the re-found one accepted
            return _FakeResponse(404, text="no such candidate") if body["candidateid"] == 111 else _FakeResponse(200, text="true")
        if url.endswith("/searchCandidateProfile"):
            return _FakeResponse(200, json_data=[{"id": 222}])
        if url.endswith("/CreateJobApplicationWithResume"):
            raise AssertionError("must not mint a profile for a known person")
        return _FakeResponse(200, text="true")

    (ok, jd_id), calls = _run(
        _service(), dispatch,
        candidate_id="111", job_id=str(JOB_ID), email="ada@example.com",
    )
    assert ok is True
    assert str(jd_id) == "222"
    assert attempts == [111, 222]
    assert _calls_to(calls, "updateCandidateProfile") == []


def test_non_numeric_known_id_is_refused_not_recreated():
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(),
        candidate_id="exa_linkedin.com/in/ada", job_id=str(JOB_ID), email="ada@example.com",
    )
    assert (ok, jd_id) == (False, None)
    assert calls == []


# ---------------------------------------------------------------------------
# Unknown person -> lookup first, link if found, create only as a last resort
# ---------------------------------------------------------------------------

def test_unknown_person_found_by_lookup_is_linked_not_recreated():
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(search_result=[{"id": 9001, "candidateId": 1}]),
        candidate_id=None, job_id=str(JOB_ID), resume_text="resume",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    assert ok is True
    assert str(jd_id) == "9001"  # searchCandidateProfile v2 returns `id`
    assert _calls_to(calls, "searchCandidateProfile")[0]["json"]["email"] == "ada@example.com"
    link = _calls_to(calls, "createJobApplication")
    assert len(link) == 1 and link[0]["json"] == {"candidateid": 9001, "jobid": JOB_ID}
    assert _calls_to(calls, "CreateJobApplicationWithResume") == []
    assert _calls_to(calls, "updateCandidateProfile") == []


def test_unknown_person_not_in_jobdiva_is_created_with_schema_exact_payload():
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(search_result=[], create_body="777"),
        candidate_id=None, job_id=str(JOB_ID), resume_text="resume body",
        filename="Ada_Lovelace_Resume.txt",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    assert ok is True
    assert jd_id == 777

    assert _calls_to(calls, "createJobApplication") == []
    create = _calls_to(calls, "CreateJobApplicationWithResume")
    assert len(create) == 1
    payload = create[0]["json"]
    # Exactly JobDiva's UploadResumeAndApplyJob schema -- in particular no
    # `candidateid`, which the endpoint does not define and silently ignores.
    assert set(payload) == UPLOAD_RESUME_AND_APPLY_JOB_FIELDS
    assert "candidateid" not in payload
    assert payload["jobid"] == JOB_ID
    assert payload["filename"] == "Ada_Lovelace_Resume.txt"
    assert payload["textfile"].startswith("Name: Ada Lovelace\nEmail: ada@example.com\nPhone: 5551234567")
    assert payload["textfile"].endswith("resume body")

    # The profile PAIR just created is the one it fixes up.
    update = _calls_to(calls, "updateCandidateProfile")
    assert len(update) == 1
    assert update[0]["json"]["candidateid"] == 777
    assert update[0]["json"]["firstName"] == "Ada"
    assert update[0]["json"]["email"] == "ada@example.com"


def test_kill_switch_refuses_to_mint_even_when_nobody_matches():
    """allow_profile_creation=False is the caller saying 'this person is in
    JobDiva'. With no link id and an empty lookup the only remaining path is
    the profile-minting one -- it must be refused, not taken."""
    (ok, jd_id), calls = _run(
        _service(), _dispatch_with(search_result=[]),
        candidate_id=None, job_id=str(JOB_ID), email="ada@example.com", phone="5551234567",
        allow_profile_creation=False,
    )
    assert (ok, jd_id) == (False, None)
    assert _calls_to(calls, "CreateJobApplicationWithResume") == []
    assert _calls_to(calls, "createJobApplication") == []


def test_link_candidate_to_job_flags_a_non_boolean_2xx_body(caplog):
    """JobDiva documents a boolean body. An empty/odd 2xx body still counts as
    success (so a benign format change cannot stall provisioning) but must be
    visible in the logs."""
    import logging

    caplog.set_level(logging.WARNING)
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient",
               lambda *a, **k: _FakeAsyncClient(calls, _dispatch_with(link_status=200, link_body=""))):
        ok = asyncio.run(_service().link_candidate_to_job(EXISTING_PROFILE, str(JOB_ID)))
    assert ok is True
    assert "non-boolean body" in caplog.text
    assert EXISTING_PROFILE in caplog.text


def test_link_candidate_to_job_refreshes_token_once_on_401():
    seen_tokens = []
    statuses = iter([401, 200])

    class _Client(_FakeAsyncClient):
        async def post(self, url, json=None, headers=None, **kw):
            seen_tokens.append(headers["Authorization"])
            self._calls.append({"method": "POST", "url": url, "json": json})
            return _FakeResponse(next(statuses), text="true")

    svc = JobDivaService()
    tokens = iter(["stale", "fresh"])

    async def _auth(force_refresh=False):
        return next(tokens)

    svc.authenticate = _auth  # type: ignore[assignment]
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Client(calls, None)):
        ok = asyncio.run(svc.link_candidate_to_job(EXISTING_PROFILE, str(JOB_ID)))
    assert ok is True
    assert seen_tokens == ["Bearer stale", "Bearer fresh"]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Provenance: outcome shape, PAIR Resume Source, id recovery
# ---------------------------------------------------------------------------

def test_outcome_unpacks_like_the_old_tuple_and_reports_the_path():
    from services.jobdiva import JobDivaApplicationOutcome

    outcome, _calls = _run(
        _service(), _dispatch_with(),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), email="ada@example.com",
    )
    assert isinstance(outcome, JobDivaApplicationOutcome)
    ok, jd_id = outcome
    assert (ok, jd_id) == (True, EXISTING_PROFILE)
    assert outcome == (True, EXISTING_PROFILE)
    assert outcome.path == "linked" and outcome.found_via_search is False

    created, _calls = _run(
        _service(), _dispatch_with(create_body="777"),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r", email="new@example.com",
    )
    assert created == (True, 777)
    assert created.path == "created" and created.found_via_search is False

    found, _calls = _run(
        _service(), _dispatch_with(search_result=[{"id": 4242}]),
        candidate_id=None, job_id=str(JOB_ID), email="known@example.com",
    )
    assert found == (True, "4242")
    assert found.path == "linked" and found.found_via_search is True

    failed, _calls = _run(
        _service(), _dispatch_with(link_status=500, link_body="boom"),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID),
    )
    assert failed == (False, None) and failed.path == "failed"


def test_configured_pair_resume_source_is_sent_on_link_and_create(monkeypatch):
    """With a PAIR Resume Source configured, both endpoints file the application
    under it: JobDiva then knows PAIR made it, whichever path ran."""
    import services.jobdiva as jd

    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_ID", 12)
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_IDS_BY_CHANNEL", "")

    _outcome, calls = _run(
        _service(), _dispatch_with(),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), origin_source="JobDiva-TalentSearch",
    )
    (link,) = _calls_to(calls, "createJobApplication")
    assert link["json"] == {"candidateid": int(EXISTING_PROFILE), "jobid": JOB_ID, "resumesource": 12}

    _outcome, calls = _run(
        _service(), _dispatch_with(create_body="777"),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r", origin_source="Dice",
    )
    (create,) = _calls_to(calls, "CreateJobApplicationWithResume")
    assert create["json"]["resumesource"] == 12
    assert set(create["json"]) == UPLOAD_RESUME_AND_APPLY_JOB_FIELDS


def test_per_channel_resume_source_beats_the_default(monkeypatch):
    import services.jobdiva as jd

    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_ID", 12)
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_IDS_BY_CHANNEL", "LinkedIn:13,Dice:14")

    assert jd.jobdiva_pair_resume_source_id("LinkedIn-Exa") == 13   # family match
    assert jd.jobdiva_pair_resume_source_id("linkedin") == 13       # case-insensitive
    assert jd.jobdiva_pair_resume_source_id("Dice") == 14
    assert jd.jobdiva_pair_resume_source_id("JobDiva-TalentSearch") == 12  # default
    assert jd.jobdiva_pair_resume_source_id("") == 12
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_IDS_BY_CHANNEL", '{"LinkedIn-Exa": 34}')
    assert jd.jobdiva_pair_resume_source_id("LinkedIn-Exa") == 34
    monkeypatch.setattr(jd, "JOBDIVA_PAIR_RESUME_SOURCE_ID", 0)
    assert jd.jobdiva_pair_resume_source_id("Dice") == 0


def test_unconfigured_resume_source_keeps_legacy_payloads():
    import services.jobdiva as jd

    assert jd.jobdiva_pair_resume_source_id("LinkedIn-Exa") == 0
    _outcome, calls = _run(
        _service(), _dispatch_with(),
        candidate_id=EXISTING_PROFILE, job_id=str(JOB_ID), origin_source="LinkedIn-Exa",
    )
    (link,) = _calls_to(calls, "createJobApplication")
    assert link["json"] == {"candidateid": int(EXISTING_PROFILE), "jobid": JOB_ID}


def test_create_without_a_returned_id_recovers_the_profile_via_lookup():
    """JobDiva 200s the create with an empty body. Without an id the row would
    never be linked and the applicant sync would re-import the person as a new
    applicant -- so the id is recovered from the profile JobDiva just parsed."""
    lookups = []

    def dispatch(url, body):
        if url.endswith("/searchCandidateProfile"):
            lookups.append(body)
            return _FakeResponse(200, json_data=[{"id": 4242}] if len(lookups) > 1 else [])
        if url.endswith("/CreateJobApplicationWithResume"):
            return _FakeResponse(200, text="")
        if url.endswith("/updateCandidateProfile"):
            raise AssertionError("a recovered id may be a pre-existing profile: never rename it")
        return _FakeResponse(200, text="true")

    outcome, calls = _run(
        _service(), dispatch,
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    # First lookup (pre-create) found nobody -> create ran -> second lookup recovered the id.
    assert len(lookups) == 2
    assert len(_calls_to(calls, "CreateJobApplicationWithResume")) == 1
    assert outcome == (True, 4242)
    assert outcome.path == "created" and outcome.found_via_search is True


def test_create_without_a_returned_id_and_no_match_is_still_a_success_without_id():
    outcome, calls = _run(
        _service(), _dispatch_with(create_body=""),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r", email="ada@example.com",
    )
    assert outcome == (True, None)
    assert outcome.path == "created" and outcome.found_via_search is False
    assert _calls_to(calls, "updateCandidateProfile") == []


# ---------------------------------------------------------------------------
# Blank-profile fix (2026-09-25): the résumé is uploaded as a FILE, and the
# created profile's blank fields are filled from what PAIR knows.
#
# JobDiva reads the résumé from `filecontent` (base64); `textfile` is only the
# "Alternate text resume". PAIR sent `filecontent: ""`, and every profile it
# created came back with a 0-byte résumé, an Auto_ email and no phone/address.
# ---------------------------------------------------------------------------

import base64
from datetime import datetime

from services.jobdiva import (
    created_profile_fill,
    jobdiva_profile_created_recently,
)


def _now_et_str():
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S")


def _dispatch_create_flow(read_back=None, create_statuses=(200,), create_body="777", read_status=200):
    """Nobody matches; the create answers `create_statuses` in turn; the
    CandidatesProfileDetail read-back returns `read_back` (a record or None)."""
    statuses = iter(create_statuses)

    def dispatch(url, body):
        if url.endswith("/searchCandidateProfile"):
            return _FakeResponse(200, json_data=[])
        if url.endswith("/CreateJobApplicationWithResume"):
            status = next(statuses)
            return _FakeResponse(status, text=create_body if status in (200, 201) else "rejected")
        if url.endswith("/CandidatesProfileDetail"):
            payload = {"data": [read_back]} if read_back else {"data": []}
            return _FakeResponse(read_status, json_data=payload)
        if url.endswith("/updateCandidateProfile"):
            return _FakeResponse(200, text="true")
        return _FakeResponse(200)
    return dispatch


def test_resume_document_is_uploaded_in_filecontent():
    document = b"PK\x03\x04 a real docx"
    outcome, calls = _run(
        _service(), _dispatch_create_flow(),
        candidate_id=None, job_id=str(JOB_ID), resume_text="Ada Lovelace\nPrincipal Data Engineer",
        resume_file=document, filename="Ada_Lovelace_Resume.docx",
        first_name="Ada", last_name="Lovelace", email="ada@example.com", phone="5551234567",
    )
    assert outcome == (True, 777)
    (create,) = _calls_to(calls, "CreateJobApplicationWithResume")
    payload = create["json"]
    assert set(payload) == UPLOAD_RESUME_AND_APPLY_JOB_FIELDS
    assert payload["filename"] == "Ada_Lovelace_Resume.docx"
    assert base64.b64decode(payload["filecontent"]) == document
    # The alternate text résumé still carries the confirmed contact header.
    assert payload["textfile"].startswith("Name: Ada Lovelace\nEmail: ada@example.com\nPhone: 5551234567")
    assert payload["textfile"].endswith("Ada Lovelace\nPrincipal Data Engineer")


def test_without_a_document_the_resume_text_is_uploaded_as_a_txt_file():
    _outcome, calls = _run(
        _service(), _dispatch_create_flow(),
        candidate_id=None, job_id=str(JOB_ID), resume_text="the whole résumé",
        filename="Ada_Lovelace_Resume.docx", first_name="Ada", last_name="Lovelace",
    )
    (create,) = _calls_to(calls, "CreateJobApplicationWithResume")
    assert create["json"]["filename"] == "Ada_Lovelace_Resume.txt"
    assert base64.b64decode(create["json"]["filecontent"]).decode("utf-8") == "the whole résumé"


def test_filecontent_is_never_empty_even_without_resume_text():
    _outcome, calls = _run(
        _service(), _dispatch_create_flow(),
        candidate_id=None, job_id=str(JOB_ID), resume_text="", first_name="Ada", last_name="Lovelace",
        email="ada@example.com",
    )
    (create,) = _calls_to(calls, "CreateJobApplicationWithResume")
    assert base64.b64decode(create["json"]["filecontent"]).decode("utf-8").startswith("Name: Ada Lovelace")


def test_rejected_document_is_retried_once_as_text():
    outcome, calls = _run(
        _service(), _dispatch_create_flow(create_statuses=(415, 200)),
        candidate_id=None, job_id=str(JOB_ID), resume_text="résumé text",
        resume_file=b"PK docx", filename="Ada_Resume.docx", first_name="Ada", last_name="Lovelace",
    )
    assert outcome == (True, 777)
    first, second = _calls_to(calls, "CreateJobApplicationWithResume")
    assert first["json"]["filename"] == "Ada_Resume.docx"
    assert second["json"]["filename"] == "Ada_Resume.txt"
    assert base64.b64decode(second["json"]["filecontent"]).decode("utf-8") == "résumé text"


def test_server_error_is_not_retried_because_the_profile_may_exist():
    """After a 5xx JobDiva may already have created the profile; a second upload
    would mint a duplicate. Re-provision looks the person up first."""
    outcome, calls = _run(
        _service(), _dispatch_create_flow(create_statuses=(500, 200)),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r", resume_file=b"PK", filename="A.docx",
        first_name="Ada", last_name="Lovelace",
    )
    assert outcome == (False, None)
    assert len(_calls_to(calls, "CreateJobApplicationWithResume")) == 1


def test_created_profile_gets_its_blank_fields_filled_from_what_pair_knows():
    """JobDiva parsed the name and email from the résumé but left phone and
    address blank: only those are written, the phone as phones[]."""
    read_back = {
        "ID": "777", "FIRSTNAME": "Ada", "LASTNAME": "Lovelace", "EMAIL": "ada@lovelace.dev",
        "PHONE1": "", "CELLPHONE": "", "CITY": "", "STATE": "", "ZIPCODE": "", "COUNTRY": "US",
        "DATECREATED": _now_et_str(), "RESUMECOUNT": "1",
    }
    outcome, calls = _run(
        _service(), _dispatch_create_flow(read_back=read_back),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email="ada@lovelace.dev", phone="+12015550100",
        profile_fields={"city": "Toronto", "state": "ON", "countryid": "CA"},
    )
    assert outcome == (True, 777) and outcome.matched_existing is False
    (update,) = _calls_to(calls, "updateCandidateProfile")
    assert update["json"] == {
        "candidateid": 777,
        "phones": [{"phone": "+12015550100", "type": "C", "action": 1}],
        "city": "Toronto", "state": "ON", "countryid": "CA",
    }


def test_auto_placeholder_profile_gets_name_email_and_phone():
    read_back = {"ID": "777", "FIRSTNAME": "Unknown", "LASTNAME": "Unknown",
                 "EMAIL": "Auto_777@jobdiva.com", "DATECREATED": _now_et_str()}
    _outcome, calls = _run(
        _service(), _dispatch_create_flow(read_back=read_back),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email="ada@lovelace.dev", phone="5551234567",
    )
    (update,) = _calls_to(calls, "updateCandidateProfile")
    assert update["json"]["firstName"] == "Ada" and update["json"]["lastName"] == "Lovelace"
    assert update["json"]["email"] == "ada@lovelace.dev"
    assert update["json"]["phones"] == [{"phone": "5551234567", "type": "C", "action": 1}]
    assert "phone" not in update["json"]


def test_resume_matched_to_an_existing_profile_keeps_that_profiles_data():
    """JobDiva filed the application on a profile it already had (created years
    ago): nothing it holds is overwritten, and the outcome says so."""
    read_back = {"ID": "777", "FIRSTNAME": "Adelaide", "LASTNAME": "Lovelace-King",
                 "EMAIL": "ada.king@kingmail.dev", "PHONE2": "(201) 555-0199", "CITY": "Newark",
                 "STATE": "", "COUNTRY": "US", "DATECREATED": "2019-04-02T10:00:00"}
    outcome, calls = _run(
        _service(), _dispatch_create_flow(read_back=read_back),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email="ada@lovelace.dev", phone="5551234567",
        profile_fields={"city": "Jersey City", "state": "NJ", "countryid": "CA"},
    )
    assert outcome == (True, 777)
    assert outcome.path == "created" and outcome.matched_existing is True
    (update,) = _calls_to(calls, "updateCandidateProfile")
    # Only the blank state; name, email, phone, city and country are left alone.
    assert update["json"] == {"candidateid": 777, "state": "NJ"}


def test_failed_read_back_falls_back_to_writing_everything():
    _outcome, calls = _run(
        _service(), _dispatch_create_flow(read_back=None, read_status=500),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email="ada@lovelace.dev", phone="5551234567",
        profile_fields={"city": "Jersey City", "state": "NJ", "countryid": "US"},
    )
    (update,) = _calls_to(calls, "updateCandidateProfile")
    assert update["json"] == {
        "candidateid": 777, "firstName": "Ada", "lastName": "Lovelace", "email": "ada@lovelace.dev",
        "phones": [{"phone": "5551234567", "type": "C", "action": 1}],
        "city": "Jersey City", "state": "NJ", "countryid": "US",
    }


def test_synthetic_lookup_email_stays_off_the_resume_but_lands_on_the_profile():
    synthetic = "pair-5551234567@no-email.jobdiva.local"
    read_back = {"ID": "777", "FIRSTNAME": "Ada", "LASTNAME": "Lovelace",
                 "EMAIL": "Auto_777@jobdiva.com", "PHONE1": "5551234567", "DATECREATED": _now_et_str()}
    _outcome, calls = _run(
        _service(), _dispatch_create_flow(read_back=read_back),
        candidate_id=None, job_id=str(JOB_ID), resume_text="r",
        first_name="Ada", last_name="Lovelace", email=synthetic, phone="5551234567",
    )
    (create,) = _calls_to(calls, "CreateJobApplicationWithResume")
    assert synthetic not in create["json"]["textfile"]
    (update,) = _calls_to(calls, "updateCandidateProfile")
    assert update["json"] == {"candidateid": 777, "email": synthetic}  # the re-provision lookup key


def test_profile_created_recently_reads_naive_eastern_timestamps():
    now = datetime(2026, 9, 25, 9, 0, 0)
    assert jobdiva_profile_created_recently({"DATECREATED": "2026-09-25T08:59:00"}, now_et=now) is True
    assert jobdiva_profile_created_recently({"DATECREATED": "2026-09-24T08:00:00"}, now_et=now) is False
    assert jobdiva_profile_created_recently({"DATECREATED": "09/25/2026 08:30:00"}, now_et=now) is True
    assert jobdiva_profile_created_recently({}, now_et=now) is None
    assert jobdiva_profile_created_recently({"DATECREATED": "garbage"}, now_et=now) is None


def test_created_profile_fill_never_overwrites_real_values():
    current = {"FIRSTNAME": "Ada", "LASTNAME": "Lovelace", "EMAIL": "ada@real.dev",
               "CELLPHONE": "2015550100", "CITY": "Newark", "STATE": "NJ", "ZIPCODE": "07102", "COUNTRY": "US"}
    assert created_profile_fill(
        current, first_name="Ada", last_name="Lovelace", email="other@real.dev", phone="5551234567",
        address={"city": "Jersey City", "state": "NJ", "zipCode": "07302", "countryid": "US"}, fresh=True,
    ) == {}
