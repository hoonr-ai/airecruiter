"""Unit tests for the 2026-06 rubric rework of `_score_candidate`.

Covers: weight-set integrity, redistribution of absent dimensions, the
must-have/preferred blend, recency, the evidence-based location hard gate,
and the currently-employed-by-client veto.

The service's __init__ touches external clients, so we build a bare instance
via object.__new__ and exercise the pure scoring methods directly.
"""
import os

# Config requires these at import time; set dummies before importing anything.
for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from core.config import (  # noqa: E402
    SCORING_WEIGHTS_DEFAULT,
    SCORING_WEIGHTS_BY_FAMILY,
)
from services.unified_candidate_search import (  # noqa: E402
    UnifiedCandidateSearch,
    SearchCriteria,
)


@pytest.fixture
def scorer():
    s = object.__new__(UnifiedCandidateSearch)
    s._current_family = None  # IT / default weights
    return s


def _criteria(**kw):
    base = dict(job_id="J1", location="", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


# ---------------------------------------------------------------- weight sets
def test_all_weight_sets_sum_to_100_with_identical_keys():
    keyset = set(SCORING_WEIGHTS_DEFAULT)
    for name, w in [("default", SCORING_WEIGHTS_DEFAULT), *SCORING_WEIGHTS_BY_FAMILY.items()]:
        assert round(sum(w.values()), 4) == 100.0, f"{name} != 100"
        assert set(w) == keyset, f"{name} key mismatch"


def test_cert_heavy_families_elevate_education_certs():
    for fam in ("healthcare", "legal", "finance", "accounting"):
        assert (
            SCORING_WEIGHTS_BY_FAMILY[fam]["education_certs"]
            > SCORING_WEIGHTS_DEFAULT["education_certs"]
        )


# ---------------------------------------------------------------- redistribution
def test_perfect_skill_match_scores_high_when_other_dims_absent(scorer):
    """Only a skills rubric is provided; every data-less dimension drops out, so
    a perfect skill match must score ~100 (weight redistributes)."""
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = {"enhanced_info": {"skills": [{"name": "Python"}]}}
    res = scorer._score_candidate(cand, crit)
    assert res["score"] >= 95


def test_partial_must_have_below_full(scorer):
    crit = _criteria(skill_criteria=[
        {"value": "Python", "match_type": "must"},
        {"value": "Kubernetes", "match_type": "must"},
    ])
    cand = {"enhanced_info": {"skills": [{"name": "Python"}]}}
    res = scorer._score_candidate(cand, crit)
    assert 0 < res["score"] < 95


# ---------------------------------------------------------------- must/preferred
def test_must_have_dominates_preferred(scorer):
    # Hits the must-have, misses the preferred → should still score well (0.70 weight).
    crit = _criteria(skill_criteria=[
        {"value": "Python", "match_type": "must"},
        {"value": "Rust", "match_type": "can"},
    ])
    cand = {"enhanced_info": {"skills": [{"name": "Python"}]}}
    res = scorer._score_candidate(cand, crit)
    assert res["score"] >= 70


# ---------------------------------------------------------------- yoe dimension
def test_yoe_dimension_redistributes_without_target(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = {"enhanced_info": {"skills": [{"name": "Python"}], "years_of_experience": 8}}
    res = scorer._score_candidate(cand, crit)
    # No min_experience_years → YOE dim must not appear.
    assert "Total Relevant YOE" not in res["score_details"]


def test_yoe_dimension_scores_when_target_set(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        min_experience_years=10,
    )
    cand = {"enhanced_info": {"skills": [{"name": "Python"}], "years_of_experience": 5}}
    res = scorer._score_candidate(cand, crit)
    assert "Total Relevant YOE" in res["score_details"]
    assert res["score_details"]["Total Relevant YOE"]["value"] == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------- location gate
def test_location_unknown_does_not_veto(scorer):
    crit = _criteria(
        location="Plano, TX", within_miles=25,
        skill_criteria=[{"value": "Python", "match_type": "must"}],
    )
    cand = {"enhanced_info": {"skills": [{"name": "Python"}]}}  # no location
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False
    assert res["score"] >= 95


def test_location_confirmed_outside_state_vetoes(scorer):
    crit = _criteria(
        location="CA", within_miles=25,
        skill_criteria=[{"value": "Python", "match_type": "must"}],
    )
    cand = {
        "enhanced_info": {"skills": [{"name": "Python"}], "current_location": "Austin, TX"},
        "city": "Austin", "state": "TX",
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is True
    assert res["score"] == 0


# ---------------------------------------------------------------- client veto
def test_currently_employed_by_client_vetoes(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        resume_match_filters=[
            {"category": "customer", "value": "Must not be employed by: Acme Corp", "active": True}
        ],
    )
    cand = {
        "enhanced_info": {
            "skills": [{"name": "Python"}],
            "company_experience": [
                {"company": "Acme Corp", "start_date": "2022", "end_date": "Present"}
            ],
        }
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is True
    assert res["score"] == 0


def test_past_employment_at_client_does_not_veto(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        resume_match_filters=[
            {"category": "customer", "value": "Must not be employed by: Acme Corp", "active": True}
        ],
    )
    cand = {
        "enhanced_info": {
            "skills": [{"name": "Python"}],
            "company_experience": [
                {"company": "Acme Corp", "start_date": "2015", "end_date": "2018"},
                {"company": "Globex", "start_date": "2018", "end_date": "Present"},
            ],
        }
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False


# ---------------------------------------------------------------- synthetic helpers
def test_career_stability_job_hopper_low(scorer):
    cand = {"enhanced_info": {"company_experience": [
        {"company": "A", "start_date": "2020", "end_date": "2020"},
        {"company": "B", "start_date": "2021", "end_date": "2021"},
    ]}}
    assert scorer._score_career_stability(cand) == 0.3


def test_career_stability_none_when_undated(scorer):
    cand = {"enhanced_info": {"company_experience": [{"company": "A"}]}}
    assert scorer._score_career_stability(cand) is None


def test_profile_linkedin_full_credit(scorer):
    assert scorer._score_profile({"urls": {"linkedin": "https://li/x"}}) == 1.0
    assert scorer._score_profile({}) is None


# ---------------------------------------------------------------- N/A policy
# apply_score_na_policy: a displayed 0% must mean "assessed and failed a
# filter". Any other unscoreable shape (detail_failed, exact-0 with no
# hard-veto and no exclusion penalty) becomes match_score=None → "N/A".
def test_na_policy_zero_without_filter_hit_becomes_none():
    from services.unified_candidate_search import apply_score_na_policy

    cand = {
        "match_score": 0,
        "explainability": ["something"],
        "match_score_details": {"hard_veto": {"triggered": False, "reasons": []}},
    }
    out = apply_score_na_policy(cand)
    assert out["match_score"] is None
    assert out["explainability"][0].startswith("Not enough candidate data")


def test_na_policy_hard_veto_keeps_zero():
    from services.unified_candidate_search import apply_score_na_policy

    cand = {
        "match_score": 0,
        "match_score_details": {"hard_veto": {"triggered": True, "reasons": ["x"]}},
    }
    assert apply_score_na_policy(cand)["match_score"] == 0


def test_na_policy_exclusion_penalty_keeps_zero():
    from services.unified_candidate_search import apply_score_na_policy

    cand = {
        "match_score": 0,
        "match_score_details": {
            "hard_veto": {"triggered": False, "reasons": []},
            "Skills Match": {"weight": 45.0, "score": 0.0, "exclusion_penalty": 12.0},
        },
    }
    assert apply_score_na_policy(cand)["match_score"] == 0


def test_na_policy_detail_failed_becomes_none_positive_score_kept():
    from services.unified_candidate_search import apply_score_na_policy

    failed = {"match_score": 34, "detail_failed": True, "match_score_details": {}}
    assert apply_score_na_policy(failed)["match_score"] is None

    scored = {"match_score": 34, "match_score_details": {}}
    assert apply_score_na_policy(scored)["match_score"] == 34


def test_na_policy_detail_failed_hard_veto_precedence():
    from services.unified_candidate_search import apply_score_na_policy

    cand = {
        "match_score": 0,
        "detail_failed": True,
        "match_score_details": {"hard_veto": {"triggered": True, "reasons": ["x"]}},
    }
    assert apply_score_na_policy(cand)["match_score"] == 0
