"""Every recruiter email about one candidate links the same JobDiva profile.

The Cross Submissions email, the Candidate Passed email and the internal
submission email all build the link with ``core.email.jobdiva_candidate_link``
-- the format the rank list / Step-5 modal open (apps/web/lib/jobdiva.ts),
verified live against a signed-in JobDiva session on 2026-10-01.
"""

import pytest

from core import email as email_mod

PROFILE_ID = "21669124548397"
PROFILE_URL = (
    "https://www1.jobdiva.com/employers/myreports/viewcandidate2_real.jsp"
    f"?docids=-1&candidateid={PROFILE_ID}"
)


@pytest.fixture
def sent(monkeypatch):
    captured = {}

    def fake_send(to_list, subject, html_body, plain_body, **_kwargs):
        captured.update(html=html_body, plain=plain_body)
        return True

    monkeypatch.setattr("core.email._send", fake_send)
    return captured


def _passed(**overrides):
    kwargs = dict(
        candidate_name="Ada Lovelace",
        candidate_email="ada@example.com",
        candidate_phone="+1 555 0100",
        screen_score="8.0/10",
        summary="Passed.",
        screening_summary=[],
        jobdiva_id="26-30451",
        job_title="Product Manager",
        location="Dallas, TX",
        salary_range="—",
        recruiter_emails=["recruiter@pyramidci.com"],
        candidate_id="unipile_ACoAAB",
        job_id="33170000",
        app_base_url="https://pairqa.pyramidci.com",
    )
    kwargs.update(overrides)
    return email_mod.notify_candidate_passed(**kwargs)


def _internal(**overrides):
    kwargs = dict(
        manager_email="manager@pyramidci.com",
        recruiter_name="Recruiter One",
        recruiter_email="recruiter@pyramidci.com",
        candidate_name="Ada Lovelace",
        candidate_id="unipile_ACoAAB",
        job_id_or_ref="33170000",
        job_title="Product Manager",
        customer_name="Client",
        app_base_url="https://pairqa.pyramidci.com",
    )
    kwargs.update(overrides)
    return email_mod.notify_internal_submission_to_manager(**kwargs)


def test_link_matches_the_cross_submissions_and_web_format():
    assert email_mod.jobdiva_candidate_link(PROFILE_ID) == PROFILE_URL


@pytest.mark.parametrize("send_email", [_passed, _internal], ids=["candidate_passed", "internal_submission"])
def test_email_links_the_jobdiva_profile(sent, send_email):
    assert send_email(jobdiva_candidate_id=PROFILE_ID)

    assert f'href="{PROFILE_URL.replace("&", "&amp;")}"' in sent["html"]
    assert "JobDiva Profile" in sent["html"]
    assert "Open in JobDiva" in sent["html"]
    assert f"JobDiva Profile: {PROFILE_URL}" in sent["plain"]
    # The PAIR report link stays; the profile link is added, not swapped in.
    assert "https://pairqa.pyramidci.com/jobs/" in sent["html"]


@pytest.mark.parametrize("send_email", [_passed, _internal], ids=["candidate_passed", "internal_submission"])
def test_email_without_a_jobdiva_profile_shows_no_link(sent, send_email):
    assert send_email()  # unlinked LinkedIn row: no id known

    assert "viewcandidate2_real.jsp" not in sent["html"]
    assert "JobDiva Profile" not in sent["html"]
    assert "JobDiva Profile" not in sent["plain"]
