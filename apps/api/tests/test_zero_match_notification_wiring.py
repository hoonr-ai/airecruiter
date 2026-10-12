"""Wiring tests for routers/candidates.py's _maybe_notify_zero_match — the
gate that decides, from a /candidates/search "summary" event, whether to
queue create_zero_match_notification. Covers the gating logic itself (full
vs sample search, zero vs nonzero count, malformed event) rather than the
full streaming endpoint, which has no existing test harness.
"""
from types import SimpleNamespace

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

    def fake_create_zero_match(**kwargs):
        notif_calls.append(kwargs)
        return "sentinel-coro"

    def fake_spawn(coro, name=None):
        spawn_calls.append((coro, name))

    monkeypatch.setattr(cand_module, "create_zero_match_notification", fake_create_zero_match)
    monkeypatch.setattr(cand_module, "_spawn_task", fake_spawn)
    return notif_calls, spawn_calls


def test_queues_notification_on_zero_candidates_in_a_full_search(monkeypatch):
    notif_calls, spawn_calls = _patch(monkeypatch)
    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)

    assert len(notif_calls) == 1
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
    def _boom(**kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(cand_module, "create_zero_match_notification", _boom)
    monkeypatch.setattr(cand_module, "_spawn_task", lambda *a, **k: None)

    request = SimpleNamespace(job_id="J1")
    criteria = SimpleNamespace(search_mode="full")

    cand_module._maybe_notify_zero_match(request, criteria, _summary_event(0), _user)  # no exception = pass
