"""Backfill of the JobDiva profiles PAIR created blank (services/jobdiva_profile_backfill.py).

Before 2026-09-25 every profile Launch PAIR created got a 0-byte résumé, the
Auto_ email and no phone/address. The backfill uploads the résumé PAIR can
build and fills only what is still blank -- and must never touch a JobDiva-
sourced person's own profile or overwrite what a recruiter entered.
"""
import asyncio
import base64
from unittest.mock import patch

import pytest

from services import jobdiva_profile_backfill as bf
from services.jobdiva import JobDivaService

JD = "20873925397812"


def _row(**over):
    row = {
        "candidate_id": "exa_linkedin.com/in/ada-lovelace", "jobdiva_id": "26-15314", "source": "LinkedIn-Exa",
        "name": "Ada Lovelace", "email": "ada@lovelace.dev", "phone": "+12015550100",
        "headline": "Principal Data Engineer", "location": "Jersey City, New Jersey, United States",
        "profile_url": "https://www.linkedin.com/in/ada-lovelace",
        "resume_text": "Principal Data Engineer\n[...]\n### Data Engineer at Acme Bank (Current)\n"
                       "Jan 2021 - Present\n- Led the lakehouse migration for 40 trading systems.",
        "data": {"jobdiva_candidate_id": JD},
    }
    row.update(over)
    return row


BLANK_PROFILE = {
    "ID": JD, "FIRSTNAME": "Ada", "LASTNAME": "Lovelace", "EMAIL": f"Auto_{JD}@jobdiva.com",
    "PHONE1": "", "CELLPHONE": "", "CITY": "", "STATE": "", "ZIPCODE": "", "COUNTRY": "US",
    "DATECREATED": "2026-06-10T12:00:00",
}


class _FakeJobDiva:
    def __init__(self, profile=None, resumes=None, after_upload=None, upload_ok=True, update_ok=True):
        self.profile = dict(profile if profile is not None else BLANK_PROFILE)
        self.after_upload = after_upload
        self.resumes = resumes if resumes is not None else [{"resume_id": "1", "date_created": "", "text": "  "}]
        self.upload_ok = upload_ok
        self.update_ok = update_ok
        self.uploads, self.updates, self.links, self.reads = [], [], [], 0
        self.refused = []

    async def fetch_candidate_profiles_batch(self, ids):
        self.reads += 1
        return {JD: self.profile} if self.profile else {}

    async def get_candidate_resume_texts(self, candidate_id):
        return self.resumes

    async def upload_resume(self, candidate_id, resume_text, **kw):
        self.uploads.append({"candidate_id": candidate_id, "text": resume_text, **kw})
        if self.after_upload:
            self.profile.update(self.after_upload)
        return {"ok": self.upload_ok, "status": 200 if self.upload_ok else 500, "filename": kw.get("filename")}

    async def authenticate(self, force_refresh=False):
        return "tok"

    async def _update_candidate_profile(self, token, candidate_id, fields):
        self.updates.append(dict(fields))
        return self.update_ok

    async def write_profile_fill(self, candidate_id, fields, current=None, token=None):
        self.updates.append(dict(fields))
        return {"ok": self.update_ok, "refused": list(self.refused), "email_held_elsewhere": "email" in self.refused}

    async def update_candidate_social_links(self, candidate_id, socialnetworks):
        self.links.append(list(socialnetworks))
        return True


def _run(row, fake, apply=True):
    return asyncio.run(bf.backfill_blank_profile(row, apply=apply, service=fake))


def test_dry_run_plans_the_repair_and_writes_nothing():
    fake = _FakeJobDiva()
    report = _run(_row(), fake, apply=False)

    assert report["status"] == "planned"
    assert report["upload_resume"] is True
    assert report["fill_fields"] == ["city", "email", "phones", "state"]
    assert report["social_links"] == ["LinkedIn"]
    assert fake.uploads == [] and fake.updates == [] and fake.links == []


def test_blank_profile_gets_the_resume_file_then_its_blank_fields():
    fake = _FakeJobDiva()
    report = _run(_row(), fake)

    assert report["status"] == "repaired"
    (upload,) = fake.uploads
    assert upload["candidate_id"] == JD
    assert upload["resume_file"][:2] == b"PK" and upload["filename"] == "Ada_Lovelace_Resume.docx"
    assert "Led the lakehouse migration" in upload["text"]
    assert upload["origin_source"] == "LinkedIn-Exa"
    (fill,) = fake.updates
    assert fill == {
        "email": "ada@lovelace.dev",
        "phones": [{"phone": "+12015550100", "type": "C", "action": 1}],
        "city": "Jersey City", "state": "NJ",
    }
    # The LinkedIn profile link lands in the profile's LinkedIn field.
    assert fake.links == [[{"name": "LinkedIn", "link": "https://www.linkedin.com/in/ada-lovelace"}]]


def test_fields_jobdiva_parsed_from_the_new_resume_are_not_written_again():
    fake = _FakeJobDiva(after_upload={"EMAIL": "ada@lovelace.dev", "CELLPHONE": "2015550100"})
    _run(_row(), fake)

    (fill,) = fake.updates
    assert "email" not in fill and "phones" not in fill
    assert fill == {"city": "Jersey City", "state": "NJ"}


def test_profile_with_a_real_resume_keeps_it():
    fake = _FakeJobDiva(resumes=[{"resume_id": "9", "date_created": "", "text": "A real résumé " * 20}])
    report = _run(_row(), fake)

    assert fake.uploads == []
    assert report["upload_resume"] is False
    assert report["status"] == "repaired"  # blank fields still filled
    assert fake.updates


def test_recruiter_entered_values_are_never_overwritten():
    fake = _FakeJobDiva(
        profile={**BLANK_PROFILE, "EMAIL": "ada.k@kingmail.dev", "CELLPHONE": "9735550199",
                 "CITY": "Newark", "STATE": "NJ", "FIRSTNAME": "Adelaide",
                 "LINKEDIN": "https://www.linkedin.com/in/ada-k", "ALTERNATEEMAIL": "ak@kingmail.dev"},
        resumes=[{"resume_id": "9", "date_created": "", "text": "A real résumé " * 20}],
    )
    report = _run(_row(), fake)

    assert report["status"] == "already_complete"
    assert fake.uploads == [] and fake.updates == [] and fake.links == []


def test_country_is_set_only_while_no_address_was_ever_entered():
    fake = _FakeJobDiva()
    _run(_row(location="Toronto, Ontario, Canada"), fake)
    assert fake.updates[0]["countryid"] == "CA"

    fake = _FakeJobDiva(profile={**BLANK_PROFILE, "CITY": "Buffalo"})
    _run(_row(location="Toronto, Ontario, Canada"), fake)
    assert "countryid" not in fake.updates[0]


def test_jobdiva_sourced_people_are_never_touched():
    fake = _FakeJobDiva()
    report = _run(_row(candidate_id=JD, source="JobDiva-TalentSearch"), fake)

    assert report["status"] == "skipped_jobdiva_sourced"
    assert fake.reads == 0 and fake.uploads == [] and fake.updates == []


def test_profiles_pair_recorded_as_preexisting_are_skipped():
    fake = _FakeJobDiva()
    report = _run(_row(data={"jobdiva_candidate_id": JD, "jobdiva_profile_origin": "jobdiva"}), fake)

    assert report["status"] == "skipped_preexisting_profile"
    assert fake.reads == 0


def test_placeholder_name_is_skipped():
    fake = _FakeJobDiva()
    report = _run(_row(name="LinkedIn Candidate"), fake)

    assert report["status"] == "skipped_no_usable_name"
    assert fake.uploads == [] and fake.updates == []


def test_unreadable_jobdiva_is_a_failure_not_a_write():
    fake = _FakeJobDiva()
    fake.resumes = None
    assert _run(_row(), fake)["status"] == "failed_resumes_not_readable"
    fake = _FakeJobDiva(profile={})
    assert _run(_row(), fake)["status"] == "failed_profile_not_readable"
    assert fake.uploads == [] and fake.updates == []


def test_failed_upload_still_fills_fields_and_says_so():
    fake = _FakeJobDiva(upload_ok=False)
    report = _run(_row(), fake)

    assert report["status"] == "partially_repaired"
    assert fake.updates  # the fill does not depend on the upload


# ---------------------------------------------------------------------------
# The two JobDiva calls
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, text="", json_data=None):
        self.status_code, self.text = status, text
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json


class _Client:
    def __init__(self, calls, answers):
        self._calls, self._answers = calls, answers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kw):
        self._calls.append({"url": url, "json": json})
        return self._answers.pop(0)


def _service():
    svc = JobDivaService()

    async def _auth(force_refresh=False):
        return "tok"

    svc.authenticate = _auth
    return svc


def _upload(answers, **kw):
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Client(calls, answers)):
        result = asyncio.run(_service().upload_resume(JD, "résumé text", **kw))
    return result, calls


def test_upload_resume_attaches_the_file_to_the_existing_candidate():
    result, calls = _upload([_Resp(200, "123456")], resume_file=b"PK docx", filename="Ada_Resume.docx")

    assert result == {"ok": True, "status": 200, "filename": "Ada_Resume.docx", "resume_id": "123456"}
    (call,) = calls
    assert call["url"].endswith("/apiv2/jobdiva/uploadResume")
    body = call["json"]
    assert body["candidateid"] == int(JD)
    assert base64.b64decode(body["filecontent"]) == b"PK docx"
    assert body["textfile"] == "résumé text"
    assert set(body) == {"candidateid", "filename", "filecontent", "textfile", "resumesource", "recruiterid", "resumeDate"}


def test_upload_resume_retries_a_rejected_document_as_text_but_not_a_server_error():
    result, calls = _upload([_Resp(415, "bad type"), _Resp(200, "77")], resume_file=b"PK", filename="A.docx")
    assert result["ok"] and [c["json"]["filename"] for c in calls] == ["A.docx", "A.txt"]

    result, calls = _upload([_Resp(500, "boom")], resume_file=b"PK", filename="A.docx")
    assert result["ok"] is False and len(calls) == 1


def _update(answers, fields):
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Client(calls, answers)):
        ok = asyncio.run(_service()._update_candidate_profile("tok", JD, fields))
    return ok, [c["json"] for c in calls]


FULL = {
    "firstName": "Ada", "lastName": "Lovelace", "email": "ada@lovelace.dev",
    "phones": [{"phone": "+12015550100", "type": "C", "action": 1}],
    "city": "Jersey City", "state": "NJ", "countryid": "US",
}


def test_profile_update_is_one_call_when_jobdiva_takes_it():
    ok, sent = _update([_Resp(200, "true")], FULL)
    assert ok is True and sent == [{"candidateid": int(JD), **FULL}]


def test_a_refused_email_does_not_cost_the_phone_or_the_address():
    """The documented failure: another profile already holds the email (500).
    Each group is then written on its own; only the email is lost."""
    ok, sent = _update([
        _Resp(500, "duplicate email"),   # full
        _Resp(500, "duplicate email"),   # name + email
        _Resp(200, "true"),              # name
        _Resp(200, "true"),              # phones
        _Resp(200, "true"),              # address
    ], FULL)
    assert ok is True
    assert sent[1] == {"candidateid": int(JD), "firstName": "Ada", "lastName": "Lovelace", "email": "ada@lovelace.dev"}
    assert sent[2] == {"candidateid": int(JD), "firstName": "Ada", "lastName": "Lovelace"}
    assert sent[3] == {"candidateid": int(JD), "phones": FULL["phones"]}
    assert sent[4] == {"candidateid": int(JD), "city": "Jersey City", "state": "NJ", "countryid": "US"}


def test_a_refused_country_is_dropped_but_the_rest_of_the_address_lands():
    ok, sent = _update([
        _Resp(500, "bad country"),       # full
        _Resp(200, "true"),              # name + email
        _Resp(200, "true"),              # phones
        _Resp(200, "false"),             # address with country: refused
        _Resp(200, "true"),              # address without country
    ], FULL)
    assert ok is True
    assert sent[-1] == {"candidateid": int(JD), "city": "Jersey City", "state": "NJ"}


def test_a_group_jobdiva_never_takes_is_reported():
    ok, sent = _update([
        _Resp(500, "x"), _Resp(200, "true"), _Resp(500, "bad phone"), _Resp(200, "true"),
    ], {k: FULL[k] for k in ("firstName", "lastName", "phones", "city")})
    assert ok is False
    assert sent[2] == {"candidateid": int(JD), "phones": FULL["phones"]}
    assert sent[3] == {"candidateid": int(JD), "city": "Jersey City"}


@pytest.mark.parametrize("status", [200])
def test_backfill_candidates_sql_is_bounded_and_excludes_jobdiva_sourced_rows(status):
    sql = " ".join(bf.BACKFILL_CANDIDATES_SQL.split())
    assert "LIMIT %(limit)s" in sql
    assert "NOT ILIKE 'JobDiva%%'" in sql
    assert "COALESCE(data->>'jobdiva_profile_origin', 'pair') = 'pair'" in sql


def test_other_linkedin_details_fill_the_matching_jobdiva_fields():
    """Every field the LinkedIn data can supply: a second email goes to
    alternateemail, websites to the matching social-network slots."""
    row = _row(data={
        "jobdiva_candidate_id": JD,
        "zoominfo_contact_enrichment": {"workEmail": "ada@acme-bank.dev", "personalEmail": "ada@lovelace.dev"},
        "linkedin_profile": {"websites": ["github.com/ada", "https://ada.dev", "https://x.com/ada"]},
    })
    fake = _FakeJobDiva()
    report = _run(row, fake)

    assert report["status"] == "repaired"
    assert fake.updates[0]["alternateemail"] == "ada@acme-bank.dev"
    (links,) = fake.links
    assert {link["name"]: link["link"] for link in links} == {
        "LinkedIn": "https://www.linkedin.com/in/ada-lovelace",
        "GitHub": "https://github.com/ada",
        "Professional Website": "https://ada.dev",
        "X": "https://x.com/ada",
    }


def test_email_held_by_another_jobdiva_record_is_reported_as_a_likely_duplicate():
    fake = _FakeJobDiva()
    fake.refused = ["email"]
    report = _run(_row(), fake)

    assert report["email_held_by_another_profile"] is True
    assert report["refused_fields"] == ["email"]


def _profile_writes(answers, fields, current=None):
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Client(calls, answers)):
        result = asyncio.run(_service().write_profile_fill(JD, fields, current or {}))
    return result, [c["json"] for c in calls]


def test_refused_email_moves_to_the_empty_alternate_slot():
    """ORA-00001 on IDX_TCANDIDATE_EMAIL: another candidate record holds the
    email. The profile still gets it -- as its alternate email."""
    unique = _Resp(500, '{"message":"ORA-00001: unique constraint (JOBDIVA.IDX_TCANDIDATE_EMAIL) violated"}')
    result, sent = _profile_writes(
        [unique, unique, _Resp(200, "true"), _Resp(200, "true")],
        {"email": "ada@lovelace.dev", "city": "Jersey City"},
        current={"ALTERNATEEMAIL": ""},
    )
    assert result == {"ok": False, "refused": ["email"], "email_held_elsewhere": True}
    assert sent[-1] == {"candidateid": int(JD), "alternateemail": "ada@lovelace.dev"}


def test_refused_email_never_replaces_an_alternate_email_already_there():
    unique = _Resp(500, "ORA-00001")
    result, sent = _profile_writes(
        [unique, unique], {"email": "ada@lovelace.dev"}, current={"ALTERNATEEMAIL": "ada@home.dev"},
    )
    assert result["email_held_elsewhere"] is True
    assert all("alternateemail" not in body for body in sent)


def test_a_partial_view_of_the_resumes_is_never_judged_blank():
    """JobDiva says it holds 2 résumés but only 1 (empty) was read: no upload."""
    fake = _FakeJobDiva(profile={**BLANK_PROFILE, "RESUMECOUNT": "2"})
    report = _run(_row(), fake)

    assert report["status"] == "failed_resumes_not_readable"
    assert fake.uploads == [] and fake.updates == []


class _GetClient:
    def __init__(self, answers):
        self._answers = answers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        key = url.rsplit("/", 1)[-1]
        return self._answers[key].pop(0)


def _resume_texts(answers, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("services.jobdiva.asyncio.sleep", _no_sleep)
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _GetClient(answers)):
        return asyncio.run(_service().get_candidate_resume_texts(JD))


def test_resume_reads_retry_a_rate_limit(monkeypatch):
    texts = _resume_texts({
        "CandidateResumesDetail": [_Resp(429, "Request Limit Exceeded"),
                                   _Resp(200, json_data={"data": [{"RESUMEID": f"{JD}_94_1", "DATECREATED": "2026-06-10T12:00:00"}]})],
        "ResumesTextDetail": [_Resp(200, json_data={"data": [{"PLAINTEXT": "A real résumé &amp; more"}]})],
    }, monkeypatch)
    assert texts == [{"resume_id": f"{JD}_94_1", "date_created": "2026-06-10T12:00:00", "text": "A real résumé & more"}]


def test_an_unreadable_resume_is_unknown_not_empty(monkeypatch):
    """A rate limit that outlasts the retries must not read as 'no résumé'."""
    limited = [_Resp(429, "Request Limit Exceeded") for _ in range(3)]
    assert _resume_texts({"CandidateResumesDetail": list(limited)}, monkeypatch) is None
    assert _resume_texts({
        "CandidateResumesDetail": [_Resp(200, json_data={"data": [{"RESUMEID": "1"}]})],
        "ResumesTextDetail": list(limited),
    }, monkeypatch) is None


def test_an_email_already_saved_as_the_alternate_is_not_retried_as_primary():
    fake = _FakeJobDiva(profile={**BLANK_PROFILE, "ALTERNATEEMAIL": "ada@lovelace.dev", "CELLPHONE": "2015550100",
                                 "CITY": "Jersey City", "STATE": "NJ", "LINKEDIN": "https://www.linkedin.com/in/ada-lovelace"},
                        resumes=[{"resume_id": "2", "date_created": "", "text": "A real résumé " * 20}])
    report = _run(_row(), fake)

    assert report["status"] == "already_complete"
    assert fake.updates == []
