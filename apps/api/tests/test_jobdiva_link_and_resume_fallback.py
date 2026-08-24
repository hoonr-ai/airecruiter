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

def test_jobdiva_source_numeric_id_links():
    """JobDiva-sourced candidate, numeric id, no stored jobdiva_candidate_id."""
    assert (
        _resolve_link_candidate_id("JobDiva-JobAgent", {}, "462058065251")
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
