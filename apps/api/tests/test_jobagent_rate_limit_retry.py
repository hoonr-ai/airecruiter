"""JobAgentSearch retries a 429 instead of returning no candidates.

A 429 — JobDiva throttling the shared account, or the shared limiter's own
"no slot came up" answer (jobdiva_rate_limit.is_slot_timeout) — used to break
out of the retry loop like any other 4xx, so on a busy account Step 5 got zero
JobAgent rows for a job JobDiva had 150 matches for (PROD, 2026-10-28: average
JobAgent rows per job fell from 38 to 14 the day after the slot timeout became
a 429).
"""
import asyncio

import httpx

from services import jobdiva
from services import jobdiva_rate_limit

_ROWS = [
    {"CANDIDATEID": "10000000000001", "FIRSTNAME": "Jane", "LASTNAME": "Doe", "CITY": "Miami", "PROVINCE": "FL"},
    {"CANDIDATEID": "10000000000002", "FIRSTNAME": "John", "LASTNAME": "Roe", "CITY": "Doral", "PROVINCE": "FL"},
]


def _slot_timeout():
    return httpx.Response(
        jobdiva_rate_limit.SLOT_TIMEOUT_STATUS,
        headers={jobdiva_rate_limit.SLOT_TIMEOUT_HEADER: "1", "Retry-After": "3"},
    )


def _run(monkeypatch, responses):
    """Serve `responses` in order; return (candidates, criteria_unconfigured, calls, sleeps)."""
    calls = []
    sleeps = []
    queue = list(responses)

    def handler(request):
        calls.append(request.url.path)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(jobdiva, "_shared_jd_client", lambda: client)

    real_sleep = asyncio.sleep

    async def fake_sleep(secs, *a, **k):
        sleeps.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(jobdiva.asyncio, "sleep", fake_sleep)

    async def main():
        try:
            return await jobdiva.jobdiva_service._search_with_job_agent(
                33265405, 150, "token", require_resume=True
            )
        finally:
            await client.aclose()

    candidates, unconfigured = asyncio.run(main())
    return candidates, unconfigured, calls, sleeps


def test_slot_timeouts_are_retried_and_the_rows_come_back(monkeypatch):
    candidates, unconfigured, calls, sleeps = _run(
        monkeypatch, [_slot_timeout(), _slot_timeout(), httpx.Response(200, json=_ROWS)]
    )
    assert [c["candidate_id"] for c in candidates] == ["10000000000001", "10000000000002"]
    assert unconfigured is False
    assert len(calls) == 3
    assert sleeps == [5.0, 10.0]


def test_a_real_jobdiva_429_waits_its_retry_after(monkeypatch):
    candidates, _, calls, sleeps = _run(
        monkeypatch,
        [httpx.Response(429, headers={"Retry-After": "25"}), httpx.Response(200, json=_ROWS)],
    )
    assert len(candidates) == 2
    assert len(calls) == 2
    assert sleeps == [25.0]


def test_retry_after_is_capped(monkeypatch):
    _, _, _, sleeps = _run(
        monkeypatch,
        [httpx.Response(429, headers={"Retry-After": "600"}), httpx.Response(200, json=_ROWS)],
    )
    assert sleeps == [30.0]


def test_a_429_that_never_clears_still_ends(monkeypatch):
    candidates, unconfigured, calls, sleeps = _run(monkeypatch, [_slot_timeout()])
    assert candidates == []
    assert unconfigured is False
    # Four rate-limit retries, then the normal three-attempt schedule.
    assert len(calls) == 7
    assert sleeps == [5.0, 10.0, 20.0, 30.0, 5, 15]


def test_other_client_errors_are_still_not_retried(monkeypatch):
    candidates, _, calls, sleeps = _run(monkeypatch, [httpx.Response(404, text="not found")])
    assert candidates == []
    assert len(calls) == 1
    assert sleeps == []


def test_unconfigured_criteria_is_still_reported(monkeypatch):
    candidates, unconfigured, calls, _ = _run(
        monkeypatch, [httpx.Response(500, text="Criteria Not Assigned")]
    )
    assert candidates == []
    assert unconfigured is True
    assert len(calls) == 3
