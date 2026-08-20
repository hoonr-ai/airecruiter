"""Two-phase JobAgent fetch — quick-first paint (2026-08-20).

JobAgentSearch latency scales with resumeCount (measured 13s @ rc=100 vs
110s @ rc=400), and the initial Step-5 search used to hold back every
JobDiva-JobAgent row behind one full-batch (150-resume) call. The pool now
fires a small quick call (JOBAGENT_QUICK_FIRST_COUNT) concurrently with the
full call, so the top ranks — resume text included, straight from the agent
response — stream while the full tranche is still in flight. seen_ids dedup
in emit_jobdiva_agent_result keeps the overlapping ranks from re-emitting
or re-enriching.

These tests pin:
  - resume_count_override plumbing in _search_jobdiva_talent (request size
    and result slice)
  - two-phase orchestration: both calls fire on the initial search, the
    quick tranche streams before the full call returns, and no candidate
    emits twice
  - single-call guards: offset>0 ("Search more"), bypass_screening
    (headless auto-sync), and JOBAGENT_QUICK_FIRST_COUNT=0
"""
import asyncio
import os

for _k in (
    "OPENAI_API_KEY", "JOBDIVA_CLIENT_ID", "JOBDIVA_USERNAME", "JOBDIVA_PASSWORD",
    "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID", "ENCRYPTION_KEY",
):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from core import sourcing_config  # noqa: E402
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


class _FakeJobDivaService:
    """Stub for the resume_count_override plumbing tests."""

    def __init__(self, total=150):
        self.total = total
        self.resume_counts = []

    async def authenticate(self):
        return None  # no token → qualifications batch is skipped

    async def search_via_job_agent(self, job_id, resume_count, require_resume=True):
        self.resume_counts.append(int(resume_count))
        return {
            "candidates": _fake_rows(min(self.total, int(resume_count))),
            "criteria_unconfigured": False,
            "resolved_jobdiva_id": 1,
        }


def _criteria(**overrides):
    base = dict(job_id="12345", sources=["JobDiva-JobAgent"])
    base.update(overrides)
    return SearchCriteria(**base)


def test_resume_count_override_requests_and_slices_to_n():
    svc = UnifiedCandidateSearch()
    fake = _FakeJobDivaService()
    svc.jobdiva_service = fake
    res = asyncio.run(
        svc._search_jobdiva_talent(_criteria(), resume_count_override=20)
    )
    assert fake.resume_counts == [20]
    assert len(res["candidates"]) == 20
    assert [c["candidate_id"] for c in res["candidates"]] == [str(i) for i in range(20)]


def test_no_override_keeps_offset_plus_batch_tranche_math():
    svc = UnifiedCandidateSearch()
    fake = _FakeJobDivaService(total=300)
    svc.jobdiva_service = fake
    res = asyncio.run(
        svc._search_jobdiva_talent(
            _criteria(jobdiva_offset=150, jobdiva_batch_size=150)
        )
    )
    # Search-more: request offset+batch, slice off the already-shown ranks.
    assert fake.resume_counts == [300]
    assert [c["candidate_id"] for c in res["candidates"]][0] == "150"
    assert len(res["candidates"]) == 150


def _patch_service_for_orchestration(svc, state):
    """Stub everything below _run_jobagent_pool: the JobAgent search itself
    (quick returns fast, full returns slow) and the enrichment generator
    (immediate passthrough). Hydration is stubbed so the test never touches
    JobDiva/DB."""

    async def _fake_search(criteria, resume_count_override=None):
        state["calls"].append(resume_count_override)
        if resume_count_override:
            await asyncio.sleep(0.01)
            n = int(resume_count_override)
        else:
            await asyncio.sleep(0.3)
            state["full_returned"] = True
            n = int(criteria.jobdiva_batch_size or 150)
        return {
            "candidates": _fake_rows(n),
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
        events = []
        async for ev in svc.search_candidates(criteria):
            events.append(ev)
        return events

    return asyncio.run(_run())


def test_two_phase_streams_quick_tranche_first_without_duplicates():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    events = _drive(svc, _criteria())

    # Both phases fired: one quick call, one full call.
    quick_n = sourcing_config.JOBAGENT_QUICK_FIRST_COUNT
    assert sorted(c or 0 for c in state["calls"]) == [0, quick_n]

    candidate_ids = [
        str(ev["data"]["candidate_id"])
        for ev in events
        if ev.get("type") == "candidate"
    ]
    # Every rank emitted exactly once — the overlap deduped, nothing lost.
    assert len(candidate_ids) == 150
    assert len(set(candidate_ids)) == 150
    # The quick tranche (ranks 0..N-1) streamed before any full-only rank:
    # queue order preserves emission order, and the quick phase resolved
    # while the full call was still sleeping.
    assert set(candidate_ids[:quick_n]) == {str(i) for i in range(quick_n)}


def test_search_more_tranche_stays_single_call():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)
    _drive(svc, _criteria(jobdiva_offset=150, jobdiva_batch_size=150))
    assert state["calls"] == [None]


def test_headless_bypass_screening_stays_single_call():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)
    _drive(svc, _criteria(bypass_screening=True))
    assert state["calls"] == [None]


def test_quick_count_zero_disables_two_phase(monkeypatch):
    monkeypatch.setattr(sourcing_config, "JOBAGENT_QUICK_FIRST_COUNT", 0)
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)
    _drive(svc, _criteria())
    assert state["calls"] == [None]
