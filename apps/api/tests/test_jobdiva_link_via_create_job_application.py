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
