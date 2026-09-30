import pytest
from pydantic import ValidationError

from models import CandidateSearchRequest
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch


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
