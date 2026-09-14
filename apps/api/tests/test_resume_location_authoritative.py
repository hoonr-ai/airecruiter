"""Résumé is final for residence (2026-09-11, product).

A JobDiva JobAgent record can say "Dallas, TX" while the résumé header says
"Hyderabad, India". The explicitly stated résumé location now REPLACES the
source-native one for display, scoring and the location / country gates;
the profile value is kept as `profile_location` + `location_conflict` for
the UI. When the résumé is silent the source value stands. A conflicting
résumé also switches off the JobDiva-JobAgent location-veto exemption.
`RESUME_LOCATION_AUTHORITATIVE=False` restores fill-blank-only.
"""
import pytest  # noqa: E402

from core import sourcing_config  # noqa: E402
import services.unified_candidate_search as ucs  # noqa: E402
from services.unified_candidate_search import (  # noqa: E402
    SearchCriteria,
    UnifiedCandidateSearch,
)


@pytest.fixture(autouse=True)
def _matrix_on(monkeypatch):
    monkeypatch.setattr(ucs, "SCORING_MATRIX_V2", True)


@pytest.fixture
def svc():
    s = object.__new__(UnifiedCandidateSearch)
    s._current_family = None
    return s


def _criteria(**kw):
    base = dict(job_id="J1", location="", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


def _cand(*, profile="Dallas, TX", resume=None, source="LinkedIn-Exa", skills=("Python",)):
    city, state = (profile.split(", ") + [""])[:2] if profile else ("", "")
    enhanced = {"structured_skills": [{"skill": s} for s in skills]}
    if resume is not None:
        enhanced["current_location"] = resume
    return {
        "candidate_id": "c1",
        "source": source,
        "city": city,
        "state": state,
        "location": profile,
        "distance_miles": 3.2,
        "location_out_of_radius": False,
        "enhanced_info": enhanced,
    }


# ---------------------------------------------------------------- _apply_resume_location
def test_resume_location_replaces_profile_and_records_conflict(svc):
    cand = _cand(profile="Dallas, TX", resume="Hyderabad, India")
    assert svc._apply_resume_location(cand) is True
    assert cand["location"] == "Hyderabad, India"
    assert cand["location_source"] == "resume"
    assert cand["profile_location"] == "Dallas, TX"
    assert cand["location_conflict"] == {"resume": "Hyderabad, India", "profile": "Dallas, TX"}
    # Cached geo verdict against the profile location is dropped.
    assert "distance_miles" not in cand and "location_out_of_radius" not in cand


def test_silent_resume_keeps_profile_location(svc):
    cand = _cand(profile="Dallas, TX", resume=None)
    assert svc._apply_resume_location(cand) is False
    assert cand["location"] == "Dallas, TX"
    assert cand["location_source"] == "profile"
    assert "location_conflict" not in cand


def test_agreeing_resume_records_no_conflict(svc):
    cand = _cand(profile="Dallas, TX", resume="Dallas, TX")
    assert svc._apply_resume_location(cand) is True
    assert cand["location_source"] == "resume"
    assert "location_conflict" not in cand
    assert "profile_location" not in cand


def test_work_arrangement_in_resume_is_not_a_location(svc):
    cand = _cand(profile="Dallas, TX", resume="Remote")
    assert svc._apply_resume_location(cand) is False
    assert cand["location"] == "Dallas, TX"


def test_flag_off_restores_fill_blank_only(svc, monkeypatch):
    monkeypatch.setattr(sourcing_config, "RESUME_LOCATION_AUTHORITATIVE", False)
    cand = _cand(profile="Dallas, TX", resume="Hyderabad, India")
    assert svc._apply_resume_location(cand) is False
    assert cand["location"] == "Dallas, TX"
    blank = _cand(profile="", resume="Hyderabad, India")
    svc._apply_resume_location(blank)
    assert blank["location"] == "Hyderabad, India"


# ---------------------------------------------------------------- gates read the résumé
def test_structured_locations_are_resume_only_when_present(svc):
    cand = _cand(profile="Dallas, TX", resume="Austin, TX")
    assert svc._candidate_structured_locations(cand) == ["Austin, TX"]
    assert svc._candidate_profile(cand)["locations"] == ["austin, tx"]
    silent = _cand(profile="Dallas, TX", resume=None)
    assert svc._candidate_structured_locations(silent)[0] == "Dallas, TX"


def test_country_gate_uses_resume_location(svc):
    cand = _cand(profile="Dallas, TX", resume="Hyderabad, India")
    svc._apply_resume_location(cand)
    assert svc._is_likely_outside_country(cand, "US") is True
    res = svc._filter_assessment(cand, _criteria(location="Dallas, TX"), enforce_years=True)
    assert res["passes"] is False
    assert res["location_failure_reason"] == "non_us_candidate"


def test_scoring_vetoes_on_resume_location_not_profile(svc):
    """Profile says Plano (in-state for a CA job? no — profile TX, résumé TX)
    — use a state-only job: profile in CA, résumé in TX → veto on the résumé."""
    crit = _criteria(location="CA", within_miles=25,
                     skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = _cand(profile="San Jose, CA", resume="Austin, TX")
    svc._apply_resume_location(cand)
    res = svc._score_candidate(cand, crit)
    assert res["score"] == 0
    assert res["score_details"]["hard_filters"]["location"] == "fail"
    assert any(line.startswith("Location taken from résumé: Austin, TX") for line in res["explainability"])


def test_scoring_passes_when_resume_is_local_even_if_profile_is_not(svc):
    crit = _criteria(location="CA", within_miles=25,
                     skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = _cand(profile="Austin, TX", resume="San Jose, CA")
    svc._apply_resume_location(cand)
    res = svc._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False
    assert res["score"] == 100


# ---------------------------------------------------------------- JobAgent exemption
def test_jobagent_exemption_off_when_resume_contradicts_profile(svc):
    crit = _criteria(location="CA", within_miles=25,
                     skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = _cand(profile="San Jose, CA", resume="Austin, TX", source="JobDiva-JobAgent")
    svc._apply_resume_location(cand)
    res = svc._score_candidate(cand, crit)
    assert res["score"] == 0
    assert res["score_details"]["hard_filters"]["location"] == "fail"


def test_jobagent_exemption_still_applies_without_conflict(svc):
    crit = _criteria(location="CA", within_miles=25,
                     skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = _cand(profile="Austin, TX", resume=None, source="JobDiva-JobAgent")
    svc._apply_resume_location(cand)
    res = svc._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False
    assert res["score"] == 100
