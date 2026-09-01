"""Sample-first search flow (2026-08-28).

Step 5's "Run Search" now probes each selected source (search_mode="sample")
and emits only `sample_per_source` fully-scored preview rows per source; the
recruiter approves the sample, and the follow-up full run (search_mode="full",
assess_all_sources=True) scores every source — JobDiva-JobAgent included — so
the frontend can auto-launch PAIR for every launchable candidate (no minimum
score; scores order the safety cap).

These tests pin:
  - sample mode fires ONE small JobAgent probe (resume_count_override =
    SAMPLE_MODE_POOL_SIZE) — never the two-phase quick+full pair
  - sample mode emits complete `candidate` rows (no shimmer skeletons, no
    candidate_detail patches) and at most `sample_per_source` of them
  - sample mode skips the background CandidatesDetail hydration sweep
  - assess_all_sources=True keeps a numeric match_score on JobDiva-JobAgent
    rows; the default (False) still stamps them to None
  - the wire model / SearchCriteria defaults keep legacy callers on the
    full-search path
"""
import asyncio

from core import sourcing_config  # noqa: E402
from models import CandidateSearchRequest  # noqa: E402
from services.unified_candidate_search import (  # noqa: E402
    SearchCriteria,
    UnifiedCandidateSearch,
)


def _fake_rows(n):
    return [
        {
            "candidate_id": str(i),
            "id": str(i),
            "name": f"Cand {i}",
            "source": "JobDiva-JobAgent",
            "api_rank": i,
            "resume_text": "Senior engineer with plenty of resume text " * 3,
        }
        for i in range(n)
    ]


def _criteria(**overrides):
    base = dict(job_id="12345", sources=["JobDiva-JobAgent"])
    base.update(overrides)
    return SearchCriteria(**base)


def _patch_service(svc, state):
    """Stub the JobAgent search, the enrichment generator (passthrough that
    also emits a details patch per row, so patch suppression is observable),
    and hydration (records invocation)."""

    async def _fake_search(criteria, resume_count_override=None):
        state["calls"].append(resume_count_override)
        n = int(resume_count_override or criteria.jobdiva_batch_size or 150)
        return {
            "candidates": _fake_rows(n),
            "source_type": "JobDiva-JobAgent",
            "jobdiva_criteria_unconfigured": False,
        }

    async def _fake_enrich(candidates, criteria, skip_llm=False):
        state["enriched"] = state.get("enriched", 0) + len(candidates)
        state["skip_llm"] = skip_llm
        for c in candidates:
            yield {
                "type": "candidate_detail",
                "candidate_id": str(c.get("candidate_id")),
                "stage": "jobdiva_details",
                "patch": {"_stage": "details_loaded"},
            }
            yield {"type": "candidate_enriched", "candidate": c}

    async def _fake_hydrate(candidates, queue, sentinel):
        state["hydrated"] = True
        await queue.put(sentinel)

    svc._search_jobdiva_talent = _fake_search
    svc._enrich_filtered_jobdiva_progressive = _fake_enrich
    svc._hydrate_jobdiva_in_background = _fake_hydrate
    svc._attach_cached_enhanced_info = lambda pool: None


def _drive(svc, criteria):
    async def _run():
        events = []
        async for ev in svc.search_candidates(criteria):
            events.append(ev)
        return events

    return asyncio.run(_run())


def _candidate_events(events):
    return [ev for ev in events if ev.get("type") == "candidate"]


def test_sample_mode_fires_single_small_probe():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    _drive(svc, _criteria(search_mode="sample", sample_per_source=2))
    assert state["calls"] == [sourcing_config.SAMPLE_MODE_POOL_SIZE]


def test_sample_mode_emits_at_most_cap_complete_rows():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    events = _drive(svc, _criteria(search_mode="sample", sample_per_source=2))

    cands = _candidate_events(events)
    assert len(cands) == 2
    for ev in cands:
        data = ev["data"]
        # Complete rows: scored (numeric or explicit None) — never a shimmer
        # skeleton stage.
        assert data.get("_stage") is None
        assert "match_score" in data
    # No detail patches ride along in sample mode — there are no skeleton
    # rows client-side for them to target.
    assert not [ev for ev in events if ev.get("type") == "candidate_detail"]


def test_sample_mode_skips_background_hydration():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    _drive(svc, _criteria(search_mode="sample", sample_per_source=2))
    assert "hydrated" not in state


def test_sample_mode_pool_is_capped_before_enrichment():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    _drive(svc, _criteria(search_mode="sample", sample_per_source=2))
    assert state["enriched"] <= sourcing_config.SAMPLE_MODE_POOL_SIZE


def test_assess_all_sources_keeps_numeric_agent_scores():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    events = _drive(
        svc,
        _criteria(search_mode="sample", sample_per_source=2, assess_all_sources=True),
    )
    for ev in _candidate_events(events):
        assert isinstance(ev["data"].get("match_score"), (int, float))
    # assess_all_sources also disables the JobAgent high-level (skip-LLM)
    # shortcut so the score is a real skills assessment.
    assert state["skip_llm"] is False


def test_default_flow_still_stamps_agent_scores_to_none():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    events = _drive(svc, _criteria(search_mode="sample", sample_per_source=2))
    for ev in _candidate_events(events):
        assert ev["data"].get("match_score") is None


def test_full_mode_still_runs_two_phase_and_hydration():
    svc = UnifiedCandidateSearch()
    state = {"calls": []}
    _patch_service(svc, state)
    events = _drive(svc, _criteria())
    # Two-phase quick+full pair, exactly as before the sample flow existed.
    quick_n = sourcing_config.JOBAGENT_QUICK_FIRST_COUNT
    assert sorted(c or 0 for c in state["calls"]) == [0, quick_n]
    assert state.get("hydrated") is True
    # Skeleton rows + detail patches stream in full mode.
    assert _candidate_events(events)
    assert [ev for ev in events if ev.get("type") == "candidate_detail"]


def test_wire_model_defaults_stay_on_full_path():
    req = CandidateSearchRequest(job_id="1")
    assert req.search_mode == "full"
    assert req.sample_per_source == 2
    assert req.assess_all_sources is False
    crit = SearchCriteria(job_id="1")
    assert crit.search_mode == "full"
    assert crit.assess_all_sources is False
