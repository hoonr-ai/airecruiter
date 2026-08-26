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
    assert "JobDiva melted" in status["reason"]
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
    # TalentSearch still ran and reported its own outcome.
    assert statuses["JobDiva-TalentSearch"]["status"] == "ok"
    assert statuses["JobDiva-TalentSearch"]["count"] == 3


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
    assert "boolean backend down" in statuses["JobDiva-TalentSearch"]["reason"]
    assert statuses["JobDiva-JobAgent"]["status"] == "ok"
