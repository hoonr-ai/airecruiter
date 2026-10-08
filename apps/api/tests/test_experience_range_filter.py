from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from models import CandidateSearchRequest
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

_NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def _scorer():
    """The range assessment is pure; avoid initializing provider clients."""
    scorer = object.__new__(UnifiedCandidateSearch)
    scorer._current_family = None
    return scorer


def test_experience_range_rejects_candidates_above_the_maximum():
    assessment = _scorer()._filter_assessment(
        {"enhanced_info": {"years_of_experience": 6}},
        SearchCriteria(job_id="26-100", min_experience_years=2, max_experience_years=5),
        enforce_years=True,
    )

    assert assessment["passes"] is False
    assert assessment["max_years_failure"] is True


def test_experience_range_accepts_candidate_inside_inclusive_bounds():
    assessment = _scorer()._filter_assessment(
        {"enhanced_info": {"years_of_experience": 5}},
        SearchCriteria(job_id="26-100", min_experience_years=2, max_experience_years=5),
        enforce_years=True,
    )

    assert assessment["passes"] is True


def test_only_an_upper_cap_makes_jobagent_range_filtering_strict():
    scorer = _scorer()

    assert scorer._has_experience_years_range(
        SearchCriteria(job_id="26-100", min_experience_years=2, max_experience_years=5)
    ) is True
    assert scorer._has_experience_years_range(
        SearchCriteria(job_id="26-100", min_experience_years=2)
    ) is False


@pytest.mark.parametrize("model, kwargs", [
    (CandidateSearchRequest, {"job_id": "26-100"}),
    (SearchCriteria, {"job_id": "26-100"}),
])
def test_experience_range_rejects_an_inverted_bound(model, kwargs):
    with pytest.raises(ValidationError, match="min_experience_years cannot exceed"):
        model(**kwargs, min_experience_years=6, max_experience_years=5)


@pytest.mark.parametrize("model, kwargs", [
    (CandidateSearchRequest, {"job_id": "26-100"}),
    (SearchCriteria, {"job_id": "26-100"}),
])
def test_maximum_experience_must_be_positive_when_present(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs, max_experience_years=0)


def test_resume_timeline_is_total_years_and_excludes_over_max():
    """Min 1 / Max 6 is the whole career, not a skill line.

    A résumé that says 6+ and runs past six years is over the ceiling.
    A later 'java: 9 years' line is not the total.
    """
    scorer = _scorer()
    criteria = SearchCriteria(job_id="26-100", min_experience_years=1, max_experience_years=6)
    over = {
        "source": "JobDiva-JobAgent",
        "experience_years": 6,
        "enhanced_info": {"years_of_experience": 6},
        "resume_text": (
            "Data Analyst with 6+ years of experience delivering insights.\n"
            "EXPERIENCE\n"
            "Cigna Healthcare | Data Analyst | Apr 2025 – Current\n"
            "Comcast | Data Analyst | Oct 2023 – Mar 2025\n"
            "BCBS | Data Analyst | Jun 2021 – Sep 2023\n"
            "BNY | Data Analyst | Jun 2020 – May 2021\n"
            "EDUCATION\n"
            "Bachelor in Computer Science | Aug 2015 – May 2019\n"
        ),
    }
    assessment = scorer._filter_assessment(over, criteria, enforce_years=True)
    assert assessment["max_years_failure"] is True

    java = {
        "source": "Dice",
        "resume_text": (
            "Full-Stack Java Developer with 7+ years of experience designing applications.\n"
            "Professional Experience\n"
            "Bank of America | June 2024 – Present\n"
            "Capital One | July 2021 – May 2024\n"
            "Deloitte | April 2019 - June 2021\n"
            "java: 6 years, last used in 2024\n"
            "angular: 9 years, last used in 2026\n"
            "Education\n"
            "Master of Science | Aug 2016 – May 2018\n"
        ),
    }
    assert scorer._filter_assessment(java, criteria, enforce_years=True)["max_years_failure"] is True
    assert scorer._candidate_outside_years_range_pre_llm(java, criteria) is True

    inside = {
        "resume_text": "Engineer with 4 years of experience.\nEXPERIENCE\nAcme | Jan 2022 – Jan 2026\n",
    }
    kept = scorer._filter_assessment(inside, criteria, enforce_years=True)
    assert kept["passes"] is True
    assert kept.get("max_years_failure") is not True


def test_education_dates_are_not_career_years_and_a_claim_cannot_outrank_jobs():
    scorer = _scorer()
    criteria = SearchCriteria(job_id="26-100", min_experience_years=1, max_experience_years=6)
    candidate = {
        "resume_text": (
            "Analyst with 15+ years of experience.\n"
            "EDUCATION\n"
            "State University | Aug 2015 – May 2019\n"
            "EXPERIENCE\n"
            "Acme | 01/2024 - 01/2026\n"
        ),
    }
    years = scorer._measured_total_years(candidate, now=_NOW)
    assert years is not None and 2.0 <= years < 2.3
    kept = scorer._filter_assessment(candidate, criteria, enforce_years=True)
    assert kept.get("max_years_failure") is not True


def test_timeline_below_min_is_rejected_and_blank_dates_do_not_invent_years():
    scorer = _scorer()
    short = {
        "resume_text": "EXPERIENCE\nAcme | Jan 2026 – Mar 2026\n",
    }
    below = scorer._filter_assessment(
        short,
        SearchCriteria(job_id="26-100", min_experience_years=1, max_experience_years=6),
        enforce_years=True,
    )
    assert below["min_years_failure"] is True

    education_only = {
        "experience_years": 5,
        "resume_text": "EDUCATION\nState University | Aug 2015 – May 2019\n",
    }
    years = scorer._measured_total_years(education_only, now=_NOW)
    assert years is None
    kept = scorer._filter_assessment(
        education_only,
        SearchCriteria(job_id="26-100", min_experience_years=1, max_experience_years=6),
        enforce_years=True,
    )
    assert kept["passes"] is True


def test_jobdiva_scored_gate_drops_only_a_real_range_failure():
    criteria = SearchCriteria(job_id="26-100", min_experience_years=1, max_experience_years=6)
    assert UnifiedCandidateSearch._experience_range_failure(
        criteria, {"max_years_failure": True}
    ) is True
    assert UnifiedCandidateSearch._experience_range_failure(
        criteria, {"min_years_failure": "yes"}
    ) is True
    assert UnifiedCandidateSearch._experience_range_failure(
        criteria, {"passes": True}
    ) is False
    assert UnifiedCandidateSearch._experience_range_failure(criteria, None) is False
