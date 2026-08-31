"""Unit tests for the JobDiva duplicate-applicant / missing-resume fix (#491).

Pins two pure helpers extracted from the engagement router:

  _resolve_link_candidate_id — decides whether to link a new JobDiva application
    to an existing profile id (only for trusted JobDiva ids), preventing duplicate
    "Unknown Unknown" applicants for JobDiva-sourced candidates while never
    mis-linking a non-JobDiva candidate's numeric internal id.

  _select_pass_email_resume — decides the resume attached to the Candidate Passed
    email, falling back to the locally stored resume when JobDiva returns
    empty/placeholder text.
"""
from routers.engagement import (  # noqa: E402
    _resolve_link_candidate_id,
    _select_pass_email_resume,
)


BLOCKED_MARKERS = (
    "Professional experience details available upon request",
    "Experienced professional with a strong background",
    "Contact information and detailed work history available upon request",
    "Resume content unavailable",
)


# ---------------------------------------------------------------------------
# _resolve_link_candidate_id
# ---------------------------------------------------------------------------

def test_jobdiva_jobagent_numeric_id_links():
    """JobAgent candidate, numeric id, no stored jobdiva_candidate_id MUST link.

    Reverses the PR #493 assertion. That PR excluded JobAgent on the premise
    that PAIR mints its own ids for those rows. It does not:
    ``_search_with_job_agent`` reads the id straight off JobDiva's own response
    (``get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"])``), and the
    sourcing stream depends on all three JobDiva pools sharing one id space --
    it dedups them against a single ``seen_ids`` set and tests JobAgent ids
    against TalentSearch rows via ``jobagent_matched_ids``.

    Excluding JobAgent meant every JobAgent launch was sent with
    ``link_candidate_id=None``, which is the instruction to JobDiva to mint a
    fresh duplicate profile.
    """
    assert (
        _resolve_link_candidate_id("JobDiva-JobAgent", {}, "462058065251")
        == "462058065251"
    )

def test_jobdiva_direct_numeric_id_links():
    """Direct JobDiva candidate, numeric id, no stored jobdiva_candidate_id MUST link."""
    assert (
        _resolve_link_candidate_id("JobDiva", {}, "462058065251")
        == "462058065251"
    )


def test_explicit_stored_jobdiva_candidate_id_links():
    """An explicitly stored jobdiva_candidate_id is trusted regardless of source."""
    assert (
        _resolve_link_candidate_id(
            "LinkedIn", {"jobdiva_candidate_id": "999"}, "999"
        )
        == "999"
    )


def test_non_jobdiva_numeric_id_does_not_link():
    """Non-JobDiva candidate with a numeric internal id must NOT link."""
    assert _resolve_link_candidate_id("LinkedIn", {}, "123456") is None


def test_non_numeric_id_does_not_link():
    """A non-numeric id (e.g. UUID) is never linked even for JobDiva sources."""
    assert _resolve_link_candidate_id("JobDiva-JobAgent", {}, "abc-uuid") is None


def test_jobdiva_applicants_and_talentsearch_link():
    """The remaining JobDiva pools link on their own numeric id too."""
    for src in ("JobDiva-Applicants", "JobDiva-TalentSearch", "jobdiva"):
        assert _resolve_link_candidate_id(src, {}, "847213") == "847213", src


def test_missing_id_returns_none():
    assert _resolve_link_candidate_id("JobDiva-JobAgent", {}, "") is None
    assert _resolve_link_candidate_id(None, None, None) is None


# ---------------------------------------------------------------------------
# _select_pass_email_resume
# ---------------------------------------------------------------------------

def test_prefers_jobdiva_resume():
    text, used_fallback = _select_pass_email_resume(
        "Real JobDiva resume", "local resume", BLOCKED_MARKERS
    )
    assert text == "Real JobDiva resume"
    assert used_fallback is False


def test_falls_back_to_local_when_jobdiva_empty():
    text, used_fallback = _select_pass_email_resume(
        "", "local resume", BLOCKED_MARKERS
    )
    assert text == "local resume"
    assert used_fallback is True


def test_falls_back_to_local_when_jobdiva_blocked():
    text, used_fallback = _select_pass_email_resume(
        "Resume content unavailable", "local resume", BLOCKED_MARKERS
    )
    assert text == "local resume"
    assert used_fallback is True


def test_no_resume_when_both_empty_or_blocked():
    text, used_fallback = _select_pass_email_resume(
        "Resume content unavailable",
        "Experienced professional with a strong background",
        BLOCKED_MARKERS,
    )
    assert text == ""
    assert used_fallback is False


def test_handles_none_inputs():
    text, used_fallback = _select_pass_email_resume(None, None, BLOCKED_MARKERS)
    assert text == ""
    assert used_fallback is False


# ---------------------------------------------------------------------------
# JobDivaService - search_candidate_profile & _update_candidate_name
# ---------------------------------------------------------------------------

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from services.jobdiva import JobDivaService

@pytest.fixture
def jobdiva_service():
    service = JobDivaService()
    service.authenticate = AsyncMock(return_value="fake_token")
    return service

import asyncio

def test_search_candidate_profile_payload(jobdiva_service):
    """Test phone is included in search payload and synthetic emails are excluded."""
    async def run_test():
        with patch("services.jobdiva.httpx.AsyncClient") as mock_client_class:
            mock_client = mock_client_class.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = [{"id": 123}]
            mock_client.post = AsyncMock(return_value=mock_response)

            # Search with real email and phone
            res = await jobdiva_service.search_candidate_profile("real@email.com", "John", "Doe", "555-1234")
            assert res == 123
            call_kwargs = mock_client.post.call_args.kwargs
            payload = call_kwargs["json"]
            assert payload["email"] == "real@email.com"
            assert payload["phone"] == "555-1234"
            
            # Search with synthetic email
            res2 = await jobdiva_service.search_candidate_profile("Auto_123@jobdiva.com", phone="555-4321")
            call_kwargs2 = mock_client.post.call_args.kwargs
            payload2 = call_kwargs2["json"]
            assert "email" not in payload2
            assert payload2["phone"] == "555-4321"
    asyncio.run(run_test())

def test_search_candidate_profile_id_priority(jobdiva_service):
    """Test that 'id' is prioritized over 'candidateId'."""
    async def run_test():
        with patch("services.jobdiva.httpx.AsyncClient") as mock_client_class:
            mock_client = mock_client_class.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post = AsyncMock(return_value=mock_response)

            # JD v2 style response
            mock_response.json.return_value = [{"id": 999, "candidateId": 888}]
            assert await jobdiva_service.search_candidate_profile("test@test.com") == 999

            # JD legacy style response
            mock_response.json.return_value = [{"candidateId": 888}]
            assert await jobdiva_service.search_candidate_profile("test@test.com") == 888
    asyncio.run(run_test())

def test_update_candidate_name_fallback(jobdiva_service):
    """Test that the name-only fallback strips email and phone when 500 occurs."""
    async def run_test():
        with patch("services.jobdiva.httpx.AsyncClient") as mock_client_class:
            mock_client = mock_client_class.return_value.__aenter__.return_value
            
            # First call fails with 500 (unique constraint error)
            # Second call (name-only fallback) succeeds with 200
            mock_res_500 = MagicMock()
            mock_res_500.status_code = 500
            mock_res_500.text = "Internal Server Error"
            
            mock_res_200 = MagicMock()
            mock_res_200.status_code = 200
            mock_res_200.text = "Success"
            
            mock_client.post = AsyncMock(side_effect=[mock_res_500, mock_res_200])

            success = await jobdiva_service._update_candidate_name(
                token="fake_token",
                candidate_id="123",
                first_name="John",
                last_name="Doe",
                email="conflict@email.com",
                phone="555-0000"
            )
            
            assert success is True
            assert mock_client.post.call_count == 2
            
            # Verify first payload had email and phone
            first_payload = mock_client.post.call_args_list[0].kwargs["json"]
            assert first_payload["email"] == "conflict@email.com"
            assert first_payload["phone"] == "555-0000"
            
            # Verify second payload stripped email and phone
            second_payload = mock_client.post.call_args_list[1].kwargs["json"]
            assert "email" not in second_payload
            assert "phone" not in second_payload
            assert second_payload["firstName"] == "John"
    asyncio.run(run_test())


# ---------------------------------------------------------------------------
# jobdiva_profile_id — the write-time stamp that makes the link possible
# ---------------------------------------------------------------------------

def test_jobdiva_profile_id_accepts_every_jobdiva_pool():
    from services.jobdiva import jobdiva_profile_id

    for src in (
        "JobDiva",
        "JobDiva-Applicants",
        "JobDiva-JobAgent",
        "JobDiva-TalentSearch",
        "jobdiva-applicants",
    ):
        assert jobdiva_profile_id(src, "847213") == "847213", src
    assert jobdiva_profile_id("JobDiva", 847213) == "847213"
    assert jobdiva_profile_id("JobDiva", "  847213  ") == "847213"


def test_jobdiva_profile_id_rejects_non_jobdiva_and_non_numeric():
    from services.jobdiva import jobdiva_profile_id

    # A LinkedIn row's numeric internal id must never be linked to a JobDiva
    # profile — that is the regression PR #493 was guarding against.
    assert jobdiva_profile_id("LinkedIn", "123456") is None
    assert jobdiva_profile_id("LinkedIn-Exa", "123456") is None
    assert jobdiva_profile_id("Dice", "123456") is None
    # Non-numeric ids are never JobDiva profile ids.
    assert jobdiva_profile_id("JobDiva", "exa_linkedin.com/in/foo") is None
    assert jobdiva_profile_id("JobDiva-JobAgent", "abc-uuid") is None
    # Missing / empty inputs.
    assert jobdiva_profile_id("JobDiva", "") is None
    assert jobdiva_profile_id("JobDiva", None) is None
    assert jobdiva_profile_id(None, "847213") is None
