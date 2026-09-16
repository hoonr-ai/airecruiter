"""Cross submissions must apply Step 5's role gate before Step 5's scorer.

Regression for the "QA Lead surfaced as a 77% Data Analyst match" email
(2026-09-16). Step 5 only scores candidates its sourcing query returned for
the job's title chips (JobDiva ``TITLES=`` / ``_candidate_title_match``); the
scoring matrix then ranks them, and title relevance is only 15 of 100
points. Cross submissions re-score a pool sourced for OTHER jobs, so the
same title gate has to run first — ``title_relevance_gate`` — and the same
scorer (``_compute_resume_matching``) and floor apply after it.

The service's __init__ touches external clients, so the scorer is the
module-level ``unified_search_service`` used through the router adapters,
exactly as production wires it.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest  # noqa: E402

import services.unified_candidate_search as ucs  # noqa: E402
from services import cross_submissions as cs  # noqa: E402
from services.unified_candidate_search import SearchCriteria, title_relevance_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _matrix_on(monkeypatch):
    monkeypatch.setattr(ucs, "SCORING_MATRIX_V2", True)


DATA_ANALYST = [{
    "value": "Data Analyst",
    "match_type": "must",
    "similar_terms": ["Business Intelligence Analyst", "Reporting Analyst"],
}]


def _criteria(**kw):
    base = dict(
        job_id="26-28121",
        title_criteria=DATA_ANALYST,
        skill_criteria=[
            {"value": "SQL", "match_type": "must"},
            {"value": "Excel", "match_type": "must"},
            {"value": "Tableau", "match_type": "must"},
            {"value": "Python", "match_type": "can"},
        ],
        location="Charlotte, NC",
        within_miles=25,
    )
    base.update(kw)
    return SearchCriteria(**base)


def _cand(title, **extra):
    cand = {"title": title, "headline": title, "enhanced_info": {"job_title": title}}
    cand.update(extra)
    return cand


# ---------------------------------------------------------------- title_relevance_gate
def test_off_role_title_is_rejected_even_with_matching_skills():
    ok, reason = title_relevance_gate(_cand("QA Lead"), DATA_ANALYST)
    assert ok is False and reason == "title_mismatch"


@pytest.mark.parametrize("title", [
    "Data Analyst",
    "Senior Data Analyst",
    "Sr. Data-Analyst",                       # punctuation / seniority prefix
    "Business Intelligence Analyst",          # recruiter-approved similar title
    "Reporting Analyst",
    "Business Data Analyst",                  # every significant token present
    "BI Developer / Data Analyst",            # multi-title string, split on separators
])
def test_on_role_titles_pass(title):
    ok, reason = title_relevance_gate(_cand(title), DATA_ANALYST)
    assert ok is True, (title, reason)
    assert "data analyst" in reason or "analyst" in reason


@pytest.mark.parametrize("title", ["Data Engineer", "Data Scientist", "Business Analyst", "Analyst", "QA Analyst"])
def test_adjacent_but_different_roles_are_rejected(title):
    assert title_relevance_gate(_cand(title), DATA_ANALYST) == (False, "title_mismatch")


def test_short_role_words_are_significant_but_connectives_are_not():
    qa_lead = [{"value": "QA Lead", "match_type": "must"}]
    assert title_relevance_gate(_cand("Tech Lead"), qa_lead) == (False, "title_mismatch")
    assert title_relevance_gate(_cand("Lead QA Engineer"), qa_lead)[0] is True
    director = [{"value": "Director of Engineering", "match_type": "must"}]
    # A comma is a multi-title separator in this codebase ("BI Developer, SQL
    # Developer"), so the connective test uses a hyphen.
    assert title_relevance_gate(_cand("Director - Engineering"), director)[0] is True
    assert title_relevance_gate(_cand("Director of Sales"), director) == (False, "title_mismatch")


def test_title_gate_never_reads_the_resume_body():
    # The résumé mentions the role; the title does not. TITLES= semantics: reject.
    cand = _cand("QA Lead", resume_text="Built Data Analyst dashboards for the QA org; SQL, Excel, Tableau.")
    assert title_relevance_gate(cand, DATA_ANALYST) == (False, "title_mismatch")


def test_no_title_criteria_means_nothing_to_gate_on():
    assert title_relevance_gate(_cand("QA Lead"), []) == (True, "no_title_criteria")
    excluded_only = [{"value": "Data Analyst", "match_type": "exclude"}]
    assert title_relevance_gate(_cand("QA Lead"), excluded_only) == (True, "no_title_criteria")


def test_title_criteria_but_no_title_on_profile_fails_like_titles_clause():
    cand = {"resume_text": "data analyst", "enhanced_info": {}}
    assert title_relevance_gate(cand, DATA_ANALYST) == (False, "no_title_on_profile")


def test_enhanced_info_job_title_counts_when_headline_is_stale():
    # JobDiva headline says one thing; the résumé parse (what Step 5 stamps
    # as `title` after enrichment) says Data Analyst.
    cand = {"headline": "Consultant", "enhanced_info": {"job_title": "Data Analyst"}}
    ok, reason = title_relevance_gate(cand, DATA_ANALYST)
    assert ok is True and reason == "data analyst ~ data analyst"


# ---------------------------------------------------------------- router adapter
def test_router_gate_uses_the_stored_step5_title():
    from routers.candidates import _passes_step5_role_gate

    payload = {
        "candidate_id": "c1", "headline": "Consultant", "resume_text": "x",
        "data": json.dumps({"title": "Senior Data Analyst", "enhanced_info": {}}),
    }
    ok, reason = _passes_step5_role_gate(payload, _criteria())
    assert ok is True and reason == "data analyst ~ senior data analyst"
    assert _passes_step5_role_gate({"candidate_id": "c2", "headline": "QA Lead", "data": "{}"}, _criteria()) == (False, "title_mismatch")
    assert _passes_step5_role_gate({"candidate_id": "c3", "headline": "QA Lead"}, None) == (True, "no_criteria")


# ---------------------------------------------------------------- end to end (the screenshot)
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _prior_row(candidate_id, title, *, location="Charlotte, NC"):
    blob = {
        "engage_status": "failed",
        "engage_updated_at": (NOW - timedelta(days=8)).isoformat(),
        "engage_score": 3, "engage_total_score": 10,
        "title": title,
        "skills": [{"skill": "SQL"}, {"skill": "Excel"}, {"skill": "Tableau"}, {"skill": "Python"}],
        "enhanced_info": {
            "job_title": title,
            "structured_skills": [
                {"skill": "SQL", "years_used": 8, "last_used_year": 2026},
                {"skill": "Excel", "years_used": 10, "last_used_year": 2026},
                {"skill": "Tableau", "years_used": 3, "last_used_year": 2025},
                {"skill": "Python", "years_used": 2, "last_used_year": 2026},
            ],
            "years_of_experience": 12,
            "current_location": location,
        },
    }
    return {
        "id": 1, "prior_key": "26-27683", "candidate_id": candidate_id, "source": "JobDiva-TalentSearch",
        "name": "N S", "email": f"{candidate_id}@example.com", "phone": "", "headline": title, "location": location,
        "resume_text": f"{title} with 12 years: SQL, Excel, Tableau dashboards, Python scripting.",
        "data": json.dumps(blob), "resume_match_percentage": 77, "updated_at": NOW - timedelta(days=8),
        "prior_job_id": "1001", "prior_jobdiva_id": "26-27683", "prior_title": "Data Analyst II",
        "prior_enhanced_title": None, "prior_customer_name": "Inspire Brands",
    }


def test_qa_lead_scores_high_but_is_no_longer_a_data_analyst_cross_submission():
    from routers.candidates import _compute_resume_matching, _passes_step5_role_gate

    crit = _criteria()
    qa_row = _prior_row("qa", "QA Lead")
    da_row = _prior_row("da", "Senior Data Analyst")

    # The scorer alone (title = 15/100) rates the QA Lead well above the floor —
    # this is the screenshot: 77% for a Data Analyst req.
    qa_payload = cs.build_scoring_payload(qa_row, json.loads(qa_row["data"]))
    assert _compute_resume_matching(qa_payload, crit)["score"] >= cs.CROSS_SUBMISSIONS_MIN_SCORE

    common = dict(new_job_base_refs={"26-28121"}, existing_person_keys=set(), cutoff=NOW - timedelta(days=60), criteria=crit)

    # Without the gate both would be listed …
    ungated = cs.select_candidates([qa_row, da_row], scorer=_compute_resume_matching, **common)
    assert {c["candidate_id"] for c in ungated} == {"qa", "da"}

    # … with Step 5's role gate only the Data Analyst is.
    stats = {}
    gated = cs.select_candidates(
        [qa_row, da_row], scorer=_compute_resume_matching, prescreen=_passes_step5_role_gate, stats=stats, **common,
    )
    assert [c["candidate_id"] for c in gated] == ["da"]
    assert gated[0]["match_score"] >= cs.CROSS_SUBMISSIONS_MIN_SCORE
    assert gated[0]["role_match"] == "data analyst ~ senior data analyst"
    assert stats == {"gated_out": 1, "gated_reasons": {"title_mismatch": 1}, "scored": 1}


def test_cross_submission_floor_is_the_step5_pool_floor():
    from core import sourcing_config

    assert cs.CROSS_SUBMISSIONS_MIN_SCORE == float(sourcing_config.JOBDIVA_TALENTSEARCH_MIN_SCORE)
