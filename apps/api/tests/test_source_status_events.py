"""Per-source `source_status` stream events (2026-08-25).

Every JobDiva pool failure mode is fail-open: auth misses, unresolvable
job ids, 4xx/5xx, exhausted retries, and unconfigured Search-Agent criteria
all return an empty candidate list with only a server-side log line. In the
browser that made a dead pool indistinguishable from a legitimately empty
one — the Step-5 source pill just showed nothing (the 2026-08-25 "JobDiva
Agent filter empty in PROD" incident). The pools now emit one
`source_status` event each ({source, status: ok|empty|failed, count,
reason}) so the UI can say why a bucket is empty.

These tests pin:
  - ok/empty/failed statuses and reasons for the JobAgent pool, including
    the criteria-unconfigured wording
  - a JobAgent pool failure is contained: the stream still completes and
    the TalentSearch pool still runs (they share one gather)
  - the TalentSearch pool emits its own status
"""
import asyncio

from services.unified_candidate_search import (  # noqa: E402
    SearchCriteria,
    UnifiedCandidateSearch,
)

from tests.test_jobagent_quick_first import (  # noqa: E402
    _criteria,
    _drive,
    _fake_rows,
    _patch_service_for_orchestration,
)


def _statuses(events):
    return {
        ev["data"]["source"]: ev["data"]
        for ev in events
        if ev.get("type") == "source_status"
    }


def test_jobagent_ok_status_carries_matched_count():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    events = _drive(svc, _criteria())

    status = _statuses(events)["JobDiva-JobAgent"]
    assert status["status"] == "ok"
    assert status["count"] == 150
    assert status["criteria_unconfigured"] is False


def test_jobagent_zero_rows_reports_empty():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _empty_search(criteria, resume_count_override=None):
        return {"candidates": [], "jobdiva_criteria_unconfigured": False}

    svc._search_jobdiva_talent = _empty_search

    events = _drive(svc, _criteria())

    status = _statuses(events)["JobDiva-JobAgent"]
    assert status["status"] == "empty"
    assert status["count"] == 0
    assert "no Job Agent matches" in status["reason"]


def test_jobagent_unconfigured_criteria_reports_reason():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _unconfigured_search(criteria, resume_count_override=None):
        return {"candidates": [], "jobdiva_criteria_unconfigured": True}

    svc._search_jobdiva_talent = _unconfigured_search

    events = _drive(svc, _criteria())

    status = _statuses(events)["JobDiva-JobAgent"]
    assert status["status"] == "empty"
    assert status["criteria_unconfigured"] is True
    assert "Search Agent criteria" in status["reason"]


def test_jobagent_failure_reports_failed_and_stream_completes():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _broken_search(criteria, resume_count_override=None):
        raise RuntimeError("JobDiva melted")

    svc._search_jobdiva_talent = _broken_search

    events = _drive(svc, _criteria())

    status = _statuses(events)["JobDiva-JobAgent"]
    assert status["status"] == "failed"
    # Raw exception text must NOT reach the recruiter-facing reason — only
    # the sanitized class name does.
    assert "JobDiva melted" not in status["reason"]
    assert "RuntimeError" in status["reason"]
    # The stream ran to completion despite the pool failure.
    assert any(ev.get("type") == "summary" for ev in events)


def test_jobagent_failure_does_not_kill_talentsearch_pool():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _broken_search(criteria, resume_count_override=None):
        raise RuntimeError("JobDiva melted")

    async def _talent_search(criteria):
        rows = _fake_rows(3)
        for r in rows:
            r["source"] = "JobDiva-TalentSearch"
            r["match_score"] = 95
        return {"candidates": rows}

    svc._search_jobdiva_talent = _broken_search
    svc._search_jobdiva_talent_search = _talent_search

    events = _drive(
        svc,
        _criteria(sources=["JobDiva-JobAgent", "JobDiva-TalentSearch"]),
    )

    statuses = _statuses(events)
    assert statuses["JobDiva-JobAgent"]["status"] == "failed"
    # TalentSearch still ran and reported its own outcome. The stub rows
    # score below the TalentSearch quality bar, so the truthful status is
    # "empty" with the sub-threshold reason — NOT "ok" from the raw fetch.
    ts = statuses["JobDiva-TalentSearch"]
    assert ts["status"] == "empty"
    assert "3 boolean matches" in ts["reason"]
    assert "quality bar" in ts["reason"]


def test_talentsearch_failure_reports_failed():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _broken_talent_search(criteria):
        raise RuntimeError("boolean backend down")

    svc._search_jobdiva_talent_search = _broken_talent_search

    events = _drive(
        svc,
        _criteria(sources=["JobDiva-JobAgent", "JobDiva-TalentSearch"]),
    )

    statuses = _statuses(events)
    assert statuses["JobDiva-TalentSearch"]["status"] == "failed"
    assert "boolean backend down" not in statuses["JobDiva-TalentSearch"]["reason"]
    assert "RuntimeError" in statuses["JobDiva-TalentSearch"]["reason"]
    assert statuses["JobDiva-JobAgent"]["status"] == "ok"


def test_search_more_empty_tranche_emits_no_misleading_status():
    """An exhausted "Search more" tranche (offset>0, zero rows, no error)
    must stay quiet — an 'empty' event would overwrite the initial run's
    'ok' in the UI and claim JobDiva has no matches while rows are on
    screen."""
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _exhausted_search(criteria, resume_count_override=None):
        return {"candidates": [], "jobdiva_criteria_unconfigured": False}

    svc._search_jobdiva_talent = _exhausted_search

    events = _drive(svc, _criteria(jobdiva_offset=150, jobdiva_batch_size=150))

    assert "JobDiva-JobAgent" not in _statuses(events)


def test_jobagent_partial_failure_waits_for_quick_phase():
    """Full phase dies, quick phase succeeds: the status must be 'failed'
    but the count must reflect the quick tranche's rows (the gather waits
    for both phases) and the reason must acknowledge the partial results."""
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    import asyncio as _asyncio

    async def _half_broken_search(criteria, resume_count_override=None):
        if resume_count_override:
            await _asyncio.sleep(0.05)
            rows = _fake_rows(int(resume_count_override))
            return {"candidates": rows, "jobdiva_criteria_unconfigured": False}
        raise RuntimeError("full phase melted")

    svc._search_jobdiva_talent = _half_broken_search

    events = _drive(svc, _criteria())

    status = _statuses(events)["JobDiva-JobAgent"]
    assert status["status"] == "failed"
    assert status["count"] == 20
    assert "incomplete" in status["reason"]


def test_external_source_failure_reports_failed():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _broken_linkedin(criteria):
        raise RuntimeError("unipile down")

    svc._search_linkedin = _broken_linkedin

    events = _drive(svc, _criteria(sources=["LinkedIn"]))

    status = _statuses(events)["LinkedIn-Unipile"]
    assert status["status"] == "failed"
    assert "unipile down" not in status["reason"]
    assert "RuntimeError" in status["reason"]


def test_external_source_empty_reports_empty():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _empty_linkedin(criteria):
        return {"candidates": [], "source_type": "LinkedIn-Unipile"}

    svc._search_linkedin = _empty_linkedin

    events = _drive(svc, _criteria(sources=["LinkedIn"]))

    status = _statuses(events)["LinkedIn-Unipile"]
    assert status["status"] == "empty"
    assert status["count"] == 0


def test_applicants_failure_reports_failed():
    svc = UnifiedCandidateSearch()
    state = {"calls": [], "full_returned": False}
    _patch_service_for_orchestration(svc, state)

    async def _broken_applicants(criteria):
        raise RuntimeError("applicants endpoint down")

    svc._search_jobdiva_applicants = _broken_applicants

    events = _drive(svc, _criteria(sources=["JobDiva Applicants"]))

    status = _statuses(events)["JobDiva-Applicants"]
    assert status["status"] == "failed"
    assert "RuntimeError" in status["reason"]
