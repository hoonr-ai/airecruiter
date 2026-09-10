"""Required-skill coverage cap on synthetic score lifts.

Reported on job 26-26189 (Quadient/Exstream Developer, Fidelity): a candidate
matching 2 of 7 required skills displayed 95% Fit. The 95 was never a rubric
verdict — a JobDiva-JobAgent row in the top 5 gets an 85 rank floor plus the
+10 JobAgent source-tier bonus, both pure provenance signals.

These pin the rule that fixes it: a lift may not carry a row past what its
required-skill coverage supports, and may never drag a row below the honest
rubric score it earned.
"""
import pytest  # noqa: E402

from core import config  # noqa: E402
from services.unified_candidate_search import UnifiedCandidateSearch  # noqa: E402


@pytest.fixture
def scorer():
    # __init__ builds external clients; the cap is pure, so bypass it.
    return object.__new__(UnifiedCandidateSearch)


def _cand(score, *, matched, total, extra_dims=None, **kw):
    details = {"Skills Match": {"required_matched": matched, "required_total": total}}
    details.update(extra_dims or {})
    out = {"match_score": score, "match_score_details": details, "explainability": []}
    out.update(kw)
    return out


# ── _required_coverage ────────────────────────────────────────────────────

def test_coverage_sums_required_groups_across_dimensions(scorer):
    details = {
        "Skills Match": {"required_matched": 2, "required_total": 7},
        "Education & Certifications": {"required_matched": 1, "required_total": 1},
        # Synthetic dimensions carry no required counts and must not dilute.
        "Profile Completeness": {"weight": 2.0, "value": 0.9},
        "hard_veto": {"triggered": False, "reasons": []},
    }
    assert scorer._required_coverage(details) == (3, 8)


def test_coverage_is_none_when_rubric_declares_nothing_required(scorer):
    details = {
        "Skills Match": {"required_matched": 0, "required_total": 0,
                         "preferred_matched": 3, "preferred_total": 4},
        "hard_veto": {"triggered": False},
    }
    assert scorer._required_coverage(details) is None


def test_coverage_never_reports_more_matches_than_groups(scorer):
    details = {"Skills Match": {"required_matched": 9, "required_total": 7}}
    assert scorer._required_coverage(details) == (7, 7)


# ── the cap ───────────────────────────────────────────────────────────────

def test_jobagent_95_with_two_of_seven_required_is_capped(scorer):
    """The reported bug: rank floor 85 + source bonus 10 = 95 on a 2/7 row."""
    cand = _cand(95, matched=2, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=52)

    # ceiling = 40 + (2/7) * 60 = 57
    assert cand["match_score"] == 57
    assert cand["match_score_details"]["required_coverage_cap"] == {
        "required_matched": 2,
        "required_total": 7,
        "ceiling": 57,
        "uncapped": 95,
        "rubric_score": 52,
    }
    assert "matched 2 of 7 required items" in cand["explainability"][0]


def test_cap_never_drops_below_the_earned_rubric_score(scorer):
    """A rubric that legitimately said 78 keeps 78 even at low coverage — the
    cap removes lift, it does not invent a penalty."""
    cand = _cand(88, matched=2, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=78)
    assert cand["match_score"] == 78
    assert cand["match_score_details"]["required_coverage_cap"]["uncapped"] == 88


def test_strong_coverage_keeps_its_lift(scorer):
    """6 of 7 required → ceiling 91, so a 90 stays exactly as scored."""
    cand = _cand(90, matched=6, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=80)
    assert cand["match_score"] == 90
    assert "required_coverage_cap" not in cand["match_score_details"]
    assert cand["explainability"] == []


def test_full_coverage_is_never_capped(scorer):
    cand = _cand(100, matched=7, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=88)
    assert cand["match_score"] == 100


def test_no_required_groups_leaves_the_score_alone(scorer):
    cand = {
        "match_score": 95,
        "match_score_details": {
            "Skills Match": {"required_matched": 0, "required_total": 0}
        },
        "explainability": [],
    }
    scorer._apply_required_coverage_cap(cand, rubric_score=60)
    assert cand["match_score"] == 95


def test_hard_vetoed_row_stays_at_zero(scorer):
    """0% is reserved for hard-filter fails; the cap must not disturb it."""
    cand = _cand(0, matched=6, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=0)
    assert cand["match_score"] == 0
    assert "required_coverage_cap" not in cand["match_score_details"]


def test_unscored_row_stays_unscored(scorer):
    cand = _cand(None, matched=1, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=40)
    assert cand["match_score"] is None


def test_kill_switch_restores_the_old_behavior(scorer, monkeypatch):
    monkeypatch.setattr(
        "services.unified_candidate_search.SCORING_COVERAGE_CAP_ENABLED", False
    )
    cand = _cand(95, matched=2, total=7)
    scorer._apply_required_coverage_cap(cand, rubric_score=52)
    assert cand["match_score"] == 95


def test_ceiling_curve_matches_the_documented_formula(scorer):
    base = config.SCORING_COVERAGE_CAP_BASE
    for matched, total in [(0, 7), (1, 7), (3, 7), (5, 7), (7, 7)]:
        cand = _cand(100, matched=matched, total=total)
        scorer._apply_required_coverage_cap(cand, rubric_score=1)
        expected = round(base + (matched / total) * (100.0 - base))
        assert cand["match_score"] == expected
