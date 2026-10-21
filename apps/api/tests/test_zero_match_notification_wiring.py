"""Wiring tests for routers/candidates.py's _maybe_notify_zero_match — the
gate that decides, from a /candidates/search "summary" event, whether to
queue the zero-match notification. Covers the gating logic itself (full
vs sample search, zero vs nonzero count, malformed event), plus (PR review
follow-up) the actual event loop inside search_jobdiva_candidates's
stream_candidates(), so a regression in the `event.get("type") ==
"summary"`/"source_status" hook itself would be caught, not just the helper
it calls.

The stream-level tests below use contextlib.aclosing (via
_search_event_stream), same as tests/test_launch_platform_candidates.py —
that requires Python 3.10+; this repo's CI runs 3.12 (see
.github/workflows/deploy.yml) but a 3.9 local venv will fail these with
AttributeError, same as the rest of that file.
"""
import asyncio
from types import SimpleNamespace

from core.auth import UserIdentity
from models import CandidateSearchRequest
from routers import candidates as cand_module

_user = SimpleNamespace(email="recruiter@x.com")


def _summary_event(total_candidates):
    return {
        "type": "summary",
        "data": {"summary": {"total_candidates": total_candidates}},
    }


def _patch(monkeypatch):
    notif_calls = []
    spawn_calls = []

    def fake_enqueue(task_name, job_id, searcher_email):
        notif_calls.append({"task": task_name, "job_id": job_id, "searcher_email": searcher_email})
        return "sentinel-coro"

    def fake_spawn(coro, name=None):
        spawn_calls.append((coro, name))

    monkeypatch.setattr(cand_module, "enqueue", fake_enqueue)
    monkeypatch.setattr(cand_module, "_spawn_task", fake_spawn)
    return notif_calls, spawn_calls


def test_queues_notification_on_zero_candidates_in_a_full_search(monkeypatch):
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)

    assert len(notif_calls) == 1
    assert notif_calls[0]["task"] == "zero_match_notification"
    assert notif_calls[0]["job_id"] == "J1"
    assert notif_calls[0]["searcher_email"] == "recruiter@x.com"
    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == "sentinel-coro"


def test_does_not_queue_for_nonzero_candidates(monkeypatch):
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(5), _user)

    assert notif_calls == []
    assert spawn_calls == []


def test_does_not_queue_for_a_sample_search_even_at_zero(monkeypatch):
    """The Step-5 UI runs a cheap "sample" probe before a recruiter approves
    a full search — a 0-result sample is routine, not a signal worth
    notifying on."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="sample")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)

    assert notif_calls == []
    assert spawn_calls == []


def test_defaults_to_full_when_search_mode_is_absent(monkeypatch):
    notif_calls, _ = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace()  # no search_mode attribute at all

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)

    assert len(notif_calls) == 1


def test_does_not_queue_when_summary_is_malformed_or_missing_count(monkeypatch):
    """A missing total_candidates key must never be treated as "zero" —
    skip rather than risk a false alarm from a malformed event."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, {"type": "summary", "data": {}}, _user)
    cand_module._maybe_notify_zero_match(request, criteria, {"type": "summary"}, _user)

    assert notif_calls == []
    assert spawn_calls == []


def test_swallows_errors_without_raising(monkeypatch):
    """Must never break the NDJSON stream still being delivered to the client."""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(cand_module, "enqueue", _boom)
    monkeypatch.setattr(cand_module, "_spawn_task", lambda *a, **k: None)

    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)  # no exception = pass


def test_does_not_queue_when_a_source_failed(monkeypatch):
    """PR review follow-up: a 0-candidate summary caused by a broken/timed-
    out source pool (see unified_candidate_search.py's emit_source_status)
    is not the same signal as a search that genuinely ran clean and found
    nobody — must not tell the recruiter to loosen filters when the real
    problem is a failed upstream call."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user, any_source_failed=True)

    assert notif_calls == []
    assert spawn_calls == []


def test_closes_the_coroutine_if_spawn_raises(monkeypatch):
    """PR review follow-up: enqueue(...) is a real
    coroutine object by the time _spawn_task sees it — if spawn itself
    raises (not the notification logic once running), the coroutine must
    be closed explicitly so it isn't left never-awaited."""
    closed = []

    class _FakeCoro:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(cand_module, "enqueue", lambda *args, **kwargs: _FakeCoro())

    def _boom_spawn(coro, name=None):
        raise RuntimeError("no running event loop")
    monkeypatch.setattr(cand_module, "_spawn_task", _boom_spawn)

    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)  # no exception = pass
    assert closed == [True]


def test_logs_a_warning_not_a_debug_line_on_failure(monkeypatch):
    """PR review follow-up: a persistently broken hook should be visible in
    production logs, not only at debug level."""
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(cand_module, "enqueue", _boom)
    monkeypatch.setattr(cand_module, "_spawn_task", lambda *a, **k: None)

    warning_calls = []
    monkeypatch.setattr(cand_module.logger, "warning", lambda msg: warning_calls.append(msg))

    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)

    assert len(warning_calls) == 1
    assert "zero_match_warning" in warning_calls[0]


# --- stream_candidates() itself (PR review follow-up) ------------------------

def _install_fake_search(monkeypatch, events):
    async def fake_search(criteria):
        for event in events:
            yield event
            await asyncio.sleep(0)

    monkeypatch.setattr(cand_module, "_verify_job_access_by_id", lambda job_id, user: None)
    monkeypatch.setattr(cand_module.unified_search_service, "search_candidates", fake_search)
    monkeypatch.setattr(cand_module, "_schedule_cross_submissions", lambda job, criteria: None)


def _request():
    return CandidateSearchRequest(job_id="J1", sources=["JobDiva-JobAgent"], location_type="Onsite")


def _consume(req, user):
    async def go():
        resp = await cand_module.search_jobdiva_candidates(req, user)
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
        return out
    return asyncio.run(go())


def test_stream_fires_zero_match_on_a_real_zero_summary_event(monkeypatch):
    """Exercises the actual event.get("type") == "summary" hook inside
    stream_candidates(), not just _maybe_notify_zero_match directly — a
    regression in that hook (wrong event-type string, wrong field access)
    would be caught here even if the helper's own tests still pass."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    _install_fake_search(monkeypatch, [
        {"type": "summary", "data": {"summary": {"total_candidates": 0}}},
    ])

    user = UserIdentity(email="recruiter@x.com", role="recruiter")
    _consume(_request(), user)

    assert len(notif_calls) == 1
    assert notif_calls[0]["task"] == "zero_match_notification"
    assert notif_calls[0]["job_id"] == "J1"
    assert notif_calls[0]["searcher_email"] == "recruiter@x.com"
    assert len(spawn_calls) == 1


def test_stream_suppresses_zero_match_after_a_failed_source_status(monkeypatch):
    """A source_status(status="failed") event ahead of the summary must
    suppress the notification — the real end-to-end path for the Medium
    review finding, not just _maybe_notify_zero_match called directly with
    any_source_failed=True."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    _install_fake_search(monkeypatch, [
        {"type": "source_status", "data": {"source": "JobDiva-JobAgent", "status": "failed", "count": 0, "reason": "boom"}},
        {"type": "summary", "data": {"summary": {"total_candidates": 0}}},
    ])

    user = UserIdentity(email="recruiter@x.com", role="recruiter")
    _consume(_request(), user)

    assert notif_calls == []
    assert spawn_calls == []


def test_stream_does_not_fire_when_a_different_source_ok_follows_a_failure(monkeypatch):
    """Once any source has failed during this search, the whole summary is
    suspect — a later "ok" status from a different source must not clear
    that, since the 0 total still reflects the failed source's missing
    contribution."""
    notif_calls, spawn_calls = _patch(monkeypatch)
    _install_fake_search(monkeypatch, [
        {"type": "source_status", "data": {"source": "Dice", "status": "failed", "count": 0, "reason": "boom"}},
        {"type": "source_status", "data": {"source": "LinkedIn", "status": "ok", "count": 0}},
        {"type": "summary", "data": {"summary": {"total_candidates": 0}}},
    ])

    user = UserIdentity(email="recruiter@x.com", role="recruiter")
    _consume(_request(), user)

    assert notif_calls == []
    assert spawn_calls == []
