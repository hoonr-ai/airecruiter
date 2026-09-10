"""Search radius is recruiter-controlled and no longer capped at 100 miles.

Reported on job 26-26189: typing anything above 100 in Step 5's "Within N mi"
snapped back to 100, and a candidate 135 miles out scored 0% via the location
hard gate with no way to widen the search. `clamp_search_radius` is now the
single source of truth for what the recruiter asked for; the hard gate
measures against that value rather than a fixed 100.
"""
import pytest  # noqa: E402

from core import config  # noqa: E402
from core.config import clamp_search_radius  # noqa: E402
from services.unified_candidate_search import (  # noqa: E402
    UnifiedCandidateSearch,
    SearchCriteria,
)


@pytest.fixture
def scorer():
    s = object.__new__(UnifiedCandidateSearch)
    s._current_family = None
    return s


def _criteria(**kw):
    base = dict(job_id="J1", location="", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


# ── clamp_search_radius ───────────────────────────────────────────────────

def test_radius_above_100_survives():
    assert clamp_search_radius(150) == 150
    assert clamp_search_radius(600) == 600


def test_radius_accepts_the_digits_scraped_from_a_chip_label():
    assert clamp_search_radius("150") == 150


def test_missing_or_junk_radius_falls_back_to_the_default():
    assert clamp_search_radius(None) == 25
    assert clamp_search_radius("") == 25
    assert clamp_search_radius("miles") == 25
    assert clamp_search_radius(0) == 25
    assert clamp_search_radius(-40) == 25
    assert clamp_search_radius(None, default=50) == 50


def test_fractional_radius_rounds_down_to_whole_miles():
    assert clamp_search_radius(25.9) == 25


def test_optional_ceiling_applies_when_configured(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_RADIUS_MAX_MILES", 200)
    assert clamp_search_radius(600) == 200
    assert clamp_search_radius(150) == 150


# ── the location hard gate measures against the recruiter's radius ────────

def test_candidate_135mi_out_is_vetoed_at_the_default_radius(scorer):
    crit = _criteria(
        location="Durham, NC 27701", within_miles=25,
        skill_criteria=[{"value": "Quadient", "match_type": "must"}],
    )
    cand = {
        "location": "Charlotte, NC 28202",
        "enhanced_info": {"skills": [{"name": "Quadient"}]},
    }
    assert scorer._location_hard_gate(cand, crit) is not None
    assert cand["location_veto_reason"] == "outside_radius_confirmed"


def test_widening_past_100mi_clears_the_veto(scorer):
    """The recourse the recruiter was missing: the same candidate, same job,
    a radius the old 100mi cap made unreachable."""
    crit = _criteria(
        location="Durham, NC 27701", within_miles=200,
        skill_criteria=[{"value": "Quadient", "match_type": "must"}],
    )
    cand = {
        "location": "Charlotte, NC 28202",
        "enhanced_info": {"skills": [{"name": "Quadient"}]},
    }
    assert scorer._location_hard_gate(cand, crit) is None
    assert not cand.get("location_veto_reason")
    assert not cand.get("location_out_of_radius")


def test_widened_radius_reaches_the_score(scorer):
    """End-to-end through _score_candidate: the 0% is gone, not just the veto."""
    skills = [{"value": "Quadient", "match_type": "must"}]
    cand = lambda: {  # noqa: E731 - fresh dict per call; the gate mutates it
        "location": "Charlotte, NC 28202",
        "enhanced_info": {"skills": [{"name": "Quadient"}]},
    }

    narrow = scorer._score_candidate(
        cand(), _criteria(location="Durham, NC 27701", within_miles=25,
                          skill_criteria=skills)
    )
    assert narrow["score"] == 0
    assert narrow["score_details"]["hard_veto"]["triggered"] is True

    wide = scorer._score_candidate(
        cand(), _criteria(location="Durham, NC 27701", within_miles=200,
                          skill_criteria=skills)
    )
    assert wide["score"] > 0
    assert wide["score_details"]["hard_veto"]["triggered"] is False
