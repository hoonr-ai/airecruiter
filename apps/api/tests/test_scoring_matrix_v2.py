"""Scoring Matrix v2 (recruiter rubric, 2026-09-11).

    Recent Must-Have Skills 75% · Recent Title/Role 15% · Preferred 10%
    Hard filters (pass/fail → 0%): exclusion rules / current client, no
    must-have skill evidenced, required certification missing, confirmed
    outside the mandatory location, below the minimum-years floor.
    Bands: 85-100 Excellent · 75-84 Strong · 60-74 Good · <60 Low priority.

Also pins the sample-mode "best 2-5 per source" selection that rides on the
matrix scores, and that the legacy additive boosts (JobAgent rank floor,
source-tier bonus, title boost) are NOT applied under the matrix.

The service's __init__ touches external clients, so scoring tests build a
bare instance via object.__new__; the sample-selection tests drive
`search_candidates` with the JobDiva pool stubbed the same way
test_sample_search_mode.py does.
"""
import asyncio
from datetime import datetime, timezone

import pytest  # noqa: E402

from core import sourcing_config  # noqa: E402
from core.config import (  # noqa: E402
    SCORING_BAND_EXCELLENT,
    SCORING_BAND_GOOD,
    SCORING_BAND_STRONG,
    SCORING_MATRIX_WEIGHTS,
    score_band,
)
import services.unified_candidate_search as ucs  # noqa: E402
from services.unified_candidate_search import (  # noqa: E402
    SearchCriteria,
    UnifiedCandidateSearch,
)


@pytest.fixture(autouse=True)
def _matrix_on(monkeypatch):
    monkeypatch.setattr(ucs, "SCORING_MATRIX_V2", True)


@pytest.fixture
def scorer():
    s = object.__new__(UnifiedCandidateSearch)
    s._current_family = None
    return s


def _criteria(**kw):
    base = dict(job_id="J1", location="", within_miles=25)
    base.update(kw)
    return SearchCriteria(**base)


def _skills(*names, **meta):
    """enhanced_info.structured_skills entries; meta applies to every entry."""
    return [{"skill": n, **meta} for n in names]


MUST5 = [
    {"value": s, "match_type": "must"}
    for s in ("Python", "SQL", "Airflow", "Spark", "Kafka")
]


# ---------------------------------------------------------------- weights
def test_matrix_weights_sum_to_100():
    assert round(sum(SCORING_MATRIX_WEIGHTS.values()), 4) == 100.0
    assert SCORING_MATRIX_WEIGHTS["must_have_skills"] == 75.0
    assert SCORING_MATRIX_WEIGHTS["title_recent"] == 15.0
    assert SCORING_MATRIX_WEIGHTS["preferred"] == 10.0


def test_full_match_across_all_three_buckets_is_100(scorer):
    crit = _criteria(
        title_criteria=[{"value": "Data Engineer", "match_type": "must"}],
        skill_criteria=[
            {"value": "Python", "match_type": "must"},
            {"value": "Rust", "match_type": "can"},
        ],
    )
    cand = {
        "title": "Data Engineer",
        "enhanced_info": {"job_title": "Data Engineer", "structured_skills": _skills("Python", "Rust")},
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score"] == 100
    d = res["score_details"]
    assert d["matrix"] == "v2"
    assert d["Recent Must-Have Skills"]["weight"] == 75.0
    assert d["Recent Title Relevance"]["weight"] == 15.0
    assert d["Preferred"]["weight"] == 10.0
    assert d["band"]["tier"] == "excellent"


# ---------------------------------------------------------------- redistribution
def test_only_must_have_rubric_redistributes_to_skills(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = {"enhanced_info": {"structured_skills": _skills("Python")}}
    res = scorer._score_candidate(cand, crit)
    assert res["score"] == 100
    assert "Recent Title Relevance" not in res["score_details"]
    assert "Preferred" not in res["score_details"]


def test_partial_must_have_lands_in_expected_bands(scorer):
    crit = _criteria(skill_criteria=MUST5)

    def score_with(*have):
        cand = {"enhanced_info": {"structured_skills": _skills(*have)}}
        return scorer._score_candidate(cand, crit)

    r4 = score_with("Python", "SQL", "Airflow", "Spark")
    r3 = score_with("Python", "SQL", "Airflow")
    r2 = score_with("Python", "SQL")
    # (matched + 0.15 floor per miss) / 5 → 83 / 66 / 49
    assert r4["score"] == 83 and r4["score_details"]["band"]["tier"] == "strong"
    assert r3["score"] == 66 and r3["score_details"]["band"]["tier"] == "good"
    assert r2["score"] == 49 and r2["score_details"]["band"]["tier"] == "low"
    assert r4["score_details"]["Recent Must-Have Skills"]["required_matched"] == 4
    assert "kafka" in r4["missing_skills"]


def test_must_have_dominates_preferred(scorer):
    crit = _criteria(skill_criteria=[
        {"value": "Python", "match_type": "must"},
        {"value": "Rust", "match_type": "can"},
    ])
    cand = {"enhanced_info": {"structured_skills": _skills("Python")}}
    res = scorer._score_candidate(cand, crit)
    # 75 of 85 available points → 88
    assert res["score"] == 88
    assert res["score_details"]["Preferred"]["preferred_matched"] == 0


# ---------------------------------------------------------------- recency / years
def test_stale_skill_by_last_used_year_is_decayed(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    old = {"enhanced_info": {"structured_skills": _skills("Python", last_used_year=2015)}}
    fresh = {"enhanced_info": {"structured_skills": _skills(
        "Python", last_used_year=datetime.now(timezone.utc).year
    )}}
    assert scorer._score_candidate(fresh, crit)["score"] == 100
    stale = scorer._score_candidate(old, crit)
    assert stale["score"] == 70  # 0.70 recency decay on the only bucket
    assert any("Not in recent experience" in line for line in stale["explainability"])


def test_recency_proxy_uses_resume_text_only_when_present(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    # Résumé text present but Python only in the structured list, not the text
    # head → treated as not recent.
    cand = {
        "resume_text": "Ten years of Java backend work. " * 20,
        "enhanced_info": {"structured_skills": _skills("Python")},
    }
    assert scorer._score_candidate(cand, crit)["score"] == 70
    # No résumé text at all → nothing to judge recency from → no penalty.
    bare = {"enhanced_info": {"structured_skills": _skills("Python")}}
    assert scorer._score_candidate(bare, crit)["score"] == 100


def test_required_years_use_per_skill_years_then_total_yoe(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must", "years": 5}])
    enough = {"enhanced_info": {"structured_skills": _skills("Python", years_used=6)}}
    short = {"enhanced_info": {"structured_skills": _skills("Python", years_used=2)}}
    unknown = {"enhanced_info": {"structured_skills": _skills("Python")}}
    total_ok = {"enhanced_info": {"structured_skills": _skills("Python"), "years_of_experience": 9}}
    assert scorer._score_candidate(enough, crit)["score"] == 100
    assert scorer._score_candidate(short, crit)["score"] == 50   # max(0.5 floor, 2/5)
    assert scorer._score_candidate(unknown, crit)["score"] == 90  # years unknown mult
    assert scorer._score_candidate(total_ok, crit)["score"] == 100
    assert any("needs 5+ yrs" in m for m in scorer._score_candidate(short, crit)["missing_skills"])


# ---------------------------------------------------------------- title bucket
def test_title_bucket_exact_role_match_and_miss(scorer):
    crit = _criteria(
        title_criteria=[{"value": "Program Manager", "match_type": "must"}],
        skill_criteria=[{"value": "Python", "match_type": "must"}],
    )
    hit = {"title": "Program Manager", "enhanced_info": {"structured_skills": _skills("Python")}}
    miss = {"title": "Accountant", "enhanced_info": {"structured_skills": _skills("Python")}}
    assert scorer._score_candidate(hit, crit)["score"] == 100
    r = scorer._score_candidate(miss, crit)
    assert r["score"] == 83  # 75 of 90
    assert r["score_details"]["Recent Title Relevance"]["score"] == 0


# ---------------------------------------------------------------- hard filters
def test_no_must_have_evidenced_with_resume_is_hard_fail(scorer):
    crit = _criteria(skill_criteria=MUST5)
    cand = {"resume_text": "Ten years of accounting and payroll. " * 10}
    res = scorer._score_candidate(cand, crit)
    assert res["score"] == 0
    assert res["score_details"]["hard_veto"]["triggered"] is True
    assert res["score_details"]["hard_filters"]["must_have_skills"] == "fail"
    assert res["score_details"]["band"]["tier"] == "excluded"
    assert res["explainability"][0].startswith("Hard exclusion")


def test_no_evidence_at_all_is_unknown_not_fail(scorer):
    crit = _criteria(skill_criteria=MUST5)
    cand = {"name": "Blank Profile"}
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False
    assert res["score_details"]["hard_filters"]["must_have_skills"] == "unknown"


def test_required_certification_is_hard_filter(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        resume_match_filters=[
            {"category": "certifications", "value": "Must have: PMP", "active": True}
        ],
    )
    missing = {
        "resume_text": "Python developer, no certifications listed. " * 5,
        "enhanced_info": {"structured_skills": _skills("Python")},
    }
    has = {
        "resume_text": "Python developer. Certifications: PMP (2022). " * 5,
        "enhanced_info": {"structured_skills": _skills("Python")},
    }
    r_missing = scorer._score_candidate(missing, crit)
    assert r_missing["score"] == 0
    assert r_missing["score_details"]["hard_filters"]["certifications"] == "fail"
    r_has = scorer._score_candidate(has, crit)
    assert r_has["score_details"]["hard_filters"]["certifications"] == "pass"
    assert r_has["score"] == 100


def test_preferred_certification_is_not_hard_filter(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        resume_match_filters=[
            {"category": "certifications", "value": "Can have: PMP", "active": True}
        ],
    )
    cand = {
        "resume_text": "Python developer. " * 10,
        "enhanced_info": {"structured_skills": _skills("Python")},
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_veto"]["triggered"] is False
    assert res["score_details"]["hard_filters"]["certifications"] == "n/a"
    assert res["score"] == 88  # 75 / 85 — the preferred cert is unmatched


def test_location_confirmed_outside_state_is_hard_fail(scorer):
    crit = _criteria(
        location="CA", within_miles=25,
        skill_criteria=[{"value": "Python", "match_type": "must"}],
    )
    cand = {
        "enhanced_info": {"structured_skills": _skills("Python"), "current_location": "Austin, TX"},
        "city": "Austin", "state": "TX",
    }
    res = scorer._score_candidate(cand, crit)
    assert res["score"] == 0
    assert res["score_details"]["hard_filters"]["location"] == "fail"


def test_location_unknown_does_not_fail(scorer):
    crit = _criteria(
        location="Plano, TX", within_miles=25,
        skill_criteria=[{"value": "Python", "match_type": "must"}],
    )
    cand = {"enhanced_info": {"structured_skills": _skills("Python")}}
    res = scorer._score_candidate(cand, crit)
    assert res["score"] == 100
    assert res["score_details"]["hard_filters"]["location"] == "pass"


def test_minimum_years_floor_is_hard_fail_when_known(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        min_experience_years=10,
    )
    junior = {"enhanced_info": {"structured_skills": _skills("Python"), "years_of_experience": 5}}
    senior = {"enhanced_info": {"structured_skills": _skills("Python"), "years_of_experience": 12}}
    unknown = {"enhanced_info": {"structured_skills": _skills("Python")}}
    assert scorer._score_candidate(junior, crit)["score"] == 0
    assert scorer._score_candidate(senior, crit)["score"] == 100
    r_unknown = scorer._score_candidate(unknown, crit)
    assert r_unknown["score"] == 100
    assert r_unknown["score_details"]["hard_filters"]["min_years"] == "unknown"


def test_currently_employed_by_client_is_hard_fail(scorer):
    crit = _criteria(
        skill_criteria=[{"value": "Python", "match_type": "must"}],
        resume_match_filters=[
            {"category": "customer", "value": "Must not be employed by: Acme Corp", "active": True}
        ],
    )
    current = {"enhanced_info": {
        "structured_skills": _skills("Python"),
        "company_experience": [{"company": "Acme Corp", "start_date": "2022", "end_date": "Present"}],
    }}
    past = {"enhanced_info": {
        "structured_skills": _skills("Python"),
        "company_experience": [
            {"company": "Acme Corp", "start_date": "2015", "end_date": "2018"},
            {"company": "Globex", "start_date": "2018", "end_date": "Present"},
        ],
    }}
    r_current = scorer._score_candidate(current, crit)
    assert r_current["score"] == 0
    assert r_current["score_details"]["hard_filters"]["client_employee"] == "fail"
    r_past = scorer._score_candidate(past, crit)
    assert r_past["score"] == 100
    assert r_past["score_details"]["hard_filters"]["client_employee"] == "pass"


def test_work_authorization_reported_as_screening_filter(scorer):
    crit = _criteria(skill_criteria=[{"value": "Python", "match_type": "must"}])
    cand = {"enhanced_info": {"structured_skills": _skills("Python")}}
    res = scorer._score_candidate(cand, crit)
    assert res["score_details"]["hard_filters"]["work_authorization"] == "screening"


# ---------------------------------------------------------------- bands
def test_score_band_thresholds():
    assert (SCORING_BAND_EXCELLENT, SCORING_BAND_STRONG, SCORING_BAND_GOOD) == (85, 75, 60)
    assert score_band(100)["tier"] == "excellent"
    assert score_band(85)["tier"] == "excellent"
    assert score_band(84)["tier"] == "strong"
    assert score_band(75)["tier"] == "strong"
    assert score_band(74)["tier"] == "good"
    assert score_band(60)["tier"] == "good"
    assert score_band(59)["tier"] == "low"
    assert score_band(None)["tier"] == "unscored"


def test_band_line_leads_explainability(scorer):
    crit = _criteria(skill_criteria=MUST5)
    cand = {"enhanced_info": {"structured_skills": _skills("Python", "SQL", "Airflow", "Spark")}}
    res = scorer._score_candidate(cand, crit)
    assert res["explainability"][0].startswith("Strong match")


# ---------------------------------------------------------------- sample helpers
def test_sample_row_hard_failed_classification():
    hf = UnifiedCandidateSearch._sample_row_hard_failed
    assert hf({"match_score": 0, "match_score_details": {"hard_veto": {"triggered": True}}}) is True
    assert hf({"match_score": 0, "match_score_details": {"hard_veto": {"triggered": False}}}) is False
    assert hf({"no_contact": True}) is True
    assert hf({"client_conflict": True}) is True
    assert hf({"match_score": 91}) is False


# ---------------------------------------------------------------- sample best-N flow
def _row(i, skills, *, employer=None):
    text = "Senior engineer using " + ", ".join(skills) + " daily. " * 3 if skills else "Senior accountant. " * 3
    enhanced = {"structured_skills": _skills(*skills)}
    if employer:
        enhanced["company_experience"] = [{"company": employer, "start_date": "2021", "end_date": "Present"}]
    return {
        "candidate_id": str(i),
        "id": str(i),
        "name": f"Cand {i}",
        "source": "JobDiva-JobAgent",
        "api_rank": i,
        "resume_text": text,
        "enhanced_info": enhanced,
    }


def _patch_service(svc, rows):
    async def _fake_search(criteria, resume_count_override=None):
        return {
            "candidates": [dict(r) for r in rows],
            "source_type": "JobDiva-JobAgent",
            "jobdiva_criteria_unconfigured": False,
        }

    async def _fake_enrich(candidates, criteria, skip_llm=False):
        for c in candidates:
            yield {"type": "candidate_enriched", "candidate": c}

    async def _fake_hydrate(candidates, queue, sentinel):
        await queue.put(sentinel)

    svc._search_jobdiva_talent = _fake_search
    svc._enrich_filtered_jobdiva_progressive = _fake_enrich
    svc._hydrate_jobdiva_in_background = _fake_hydrate
    svc._attach_cached_enhanced_info = lambda pool: None


def _drive(svc, criteria):
    async def _run():
        out = []
        async for ev in svc.search_candidates(criteria):
            out.append(ev)
        return out

    return asyncio.run(_run())


def _emitted(events):
    return [ev["data"] for ev in events if ev.get("type") == "candidate"]


def _sample_criteria(**overrides):
    base = dict(
        job_id="12345",
        sources=["JobDiva-JobAgent"],
        search_mode="sample",
        sample_per_source=5,
        assess_all_sources=True,
        skill_criteria=[
            {"value": "Python", "match_type": "must"},
            {"value": "SQL", "match_type": "must"},
        ],
    )
    base.update(overrides)
    return SearchCriteria(**base)


def test_sample_emits_best_rows_by_score_not_arrival_order(monkeypatch):
    monkeypatch.setattr(sourcing_config, "SAMPLE_MIN_ROWS_PER_SOURCE", 2)
    monkeypatch.setattr(sourcing_config, "SAMPLE_QUALITY_FLOOR", 60)
    # Arrival order is weak-first; the best three (full matches) must win.
    rows = [
        _row(0, []),
        _row(1, ["Python"]),          # ~58: below the floor
        _row(2, ["Python", "SQL"]),   # 100
        _row(3, []),
        _row(4, ["Python", "SQL"]),   # 100
        _row(5, ["SQL"]),             # ~58
        _row(6, ["Python", "SQL"]),   # 100
        _row(7, []),
    ]
    svc = UnifiedCandidateSearch()
    _patch_service(svc, rows)
    got = _emitted(_drive(svc, _sample_criteria()))
    assert [c["candidate_id"] for c in got] == ["2", "4", "6"]
    assert all(c["match_score"] == 100 for c in got)


def test_sample_always_shows_minimum_rows_even_below_floor(monkeypatch):
    monkeypatch.setattr(sourcing_config, "SAMPLE_MIN_ROWS_PER_SOURCE", 2)
    monkeypatch.setattr(sourcing_config, "SAMPLE_QUALITY_FLOOR", 60)
    rows = [_row(0, []), _row(1, ["Python"]), _row(2, []), _row(3, ["SQL"]), _row(4, [])]
    svc = UnifiedCandidateSearch()
    _patch_service(svc, rows)
    got = _emitted(_drive(svc, _sample_criteria()))
    assert len(got) == 2
    assert sorted(c["candidate_id"] for c in got) == ["1", "3"]
    assert all(c["match_score"] < 60 for c in got)


def test_sample_respects_cap_of_five(monkeypatch):
    monkeypatch.setattr(sourcing_config, "SAMPLE_MIN_ROWS_PER_SOURCE", 2)
    monkeypatch.setattr(sourcing_config, "SAMPLE_QUALITY_FLOOR", 60)
    rows = [_row(i, ["Python", "SQL"]) for i in range(8)]
    svc = UnifiedCandidateSearch()
    _patch_service(svc, rows)
    got = _emitted(_drive(svc, _sample_criteria(sample_per_source=5)))
    assert len(got) == 5


def test_sample_skips_hard_filter_fails(monkeypatch):
    monkeypatch.setattr(sourcing_config, "SAMPLE_MIN_ROWS_PER_SOURCE", 2)
    monkeypatch.setattr(sourcing_config, "SAMPLE_QUALITY_FLOOR", 60)
    rows = [
        _row(0, ["Python", "SQL"], employer="Acme Corp"),  # current client → hard fail
        _row(1, ["Python", "SQL"]),
        _row(2, ["Python"]),
    ]
    svc = UnifiedCandidateSearch()
    _patch_service(svc, rows)
    crit = _sample_criteria(
        resume_match_filters=[
            {"category": "customer", "value": "Must not be employed by: Acme Corp", "active": True}
        ],
    )
    got = _emitted(_drive(svc, crit))
    ids = [c["candidate_id"] for c in got]
    assert "0" not in ids
    assert ids == ["1", "2"]  # 100, then the sub-floor row fills the minimum


def test_matrix_disables_jobagent_rank_floor_and_source_bonus():
    """Legacy finalize stacked rank floor (api_rank 0 → 85) + JobAgent bonus
    (+10) on top of the rubric score. Under the matrix the % IS the matrix."""
    rows = [_row(0, ["Python"])]  # 1 of 2 must-haves → (1 + 0.15) / 2 → 58
    svc = UnifiedCandidateSearch()
    _patch_service(svc, rows)
    got = _emitted(_drive(svc, _sample_criteria(sample_per_source=1)))
    assert len(got) == 1
    assert 50 <= got[0]["match_score"] < 60
    details = got[0]["match_score_details"]
    assert "jobagent_rank_floor" not in details
    assert "source_tier_bonus" not in details
    assert "title_boost" not in details
