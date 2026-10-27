"""services/launch_sessions/effects.Effects: every call the runner makes to
the D1 / D2 / C code is bound against the REAL function's signature (so the
fakes the runner tests use can never drift from the production seam), and
the values pass through unchanged."""

import asyncio
import datetime as dt
import inspect

import pytest

from routers import candidates as rc
from routers import engagement as eng
from services import launch_timing
from services.launch_sessions.effects import Effects


def _recorder(monkeypatch, module, name, result=None, *, is_async=False, is_gen=False):
    original = getattr(module, name)
    sig = inspect.signature(original)
    calls = []

    if is_gen:
        async def fake(*a, **k):
            sig.bind(*a, **k)
            calls.append((a, k))
            for item in result or []:
                yield item
    elif is_async:
        async def fake(*a, **k):
            sig.bind(*a, **k)
            calls.append((a, k))
            return result
    else:
        def fake(*a, **k):
            sig.bind(*a, **k)
            calls.append((a, k))
            return result

    monkeypatch.setattr(module, name, fake)
    return calls


async def _drain(agen):
    return [x async for x in agen]


def test_search_and_enrich(monkeypatch):
    fx = Effects()
    req = fx.build_search_request({"job_id": "26-001", "boolean_string": "java"})
    assert req.boolean_string == "java"
    calls = _recorder(monkeypatch, rc, "iter_search_events", [{"type": "candidate"}], is_gen=True)
    assert asyncio.run(_drain(fx.search_events(req, schedule_cross_submissions=False))) == [{"type": "candidate"}]
    assert calls[0][1] == {"schedule_cross_submissions": False, "defer_paid_contact": False}
    calls = _recorder(monkeypatch, rc, "enrich_contacts_for_launch", [{"index": 0}], is_gen=True)
    items = [{"candidate_id": "a", "source": "LinkedIn", "linkedin_url": "u", "match_score": 70}]
    assert asyncio.run(_drain(fx.enrich(items, job_id="26-001", concurrency=5))) == [{"index": 0}]
    assert calls[0][1] == {"job_id": "26-001", "concurrency": 5}


def test_save_builds_the_real_request(monkeypatch):
    calls = _recorder(monkeypatch, rc, "save_candidates", {"saved_ids": ["a"]}, is_async=True)
    from services.launch_sessions import rules

    payload = rules.build_save_candidate_payload({"candidate_id": "a", "name": "A", "match_score": 70})
    out = asyncio.run(Effects().save("26-001", [payload], user="U"))
    assert out == {"saved_ids": ["a"]}
    (req,), kw = calls[0]
    assert kw == {"user": "U"} and req.jobdiva_id == "26-001" and req.candidates[0].candidate_id == "a"
    assert req.candidates[0].match_score == 70 and req.candidates[0].is_selected is True
    gate = _recorder(monkeypatch, rc, "_enforce_recruiter_email_gate", None, is_async=True)
    asyncio.run(Effects().recruiter_email_gate("26-001"))
    assert gate[0][0] == ("26-001",)


def test_launch_seam(monkeypatch):
    fx = Effects()
    req = fx.launch_request(
        job_id="26-001", candidate_ids=["a"], is_initial_launch=False, notify_recruiters=False,
        send_job_posting_email=False, app_base_url="", batch_size=75, launch_session_id="s",
    )
    assert isinstance(req, eng.LaunchRequest) and req.launch_session_id == "s"
    calls = _recorder(monkeypatch, eng, "run_launch", [{"type": "done"}], is_gen=True)
    hook = lambda i, ids: "b"  # noqa: E731
    out = asyncio.run(_drain(fx.run_launch(
        req, launch_id="l", run_id="s", fire_side_effects=False, should_stop=lambda: False,
        provision_inline=False, claimed_session_id="s", bulk_id_for=hook,
    )))
    assert out == [{"type": "done"}] and calls[0][1]["bulk_id_for"] is hook
    reg = _recorder(monkeypatch, eng, "register_session_launch_run")
    fx.register_launch_run("l", "26-001", requested_by="r@x.com", session_id="s")
    assert reg[0] == (("l", "26-001"), {"requested_by": "r@x.com", "launch_session_id": "s"})
    claim = _recorder(monkeypatch, eng, "claim_candidates_for_session", ["a"])
    assert fx.claim_for_session(["1001", "26-001"], ["a"], "s") == ["a"]
    assert claim[0][0] == (("1001", "26-001"), ["a"], "s")
    rel = _recorder(monkeypatch, eng, "release_session_claims", 1)
    assert fx.release_claims("s", ["1001", "26-001"], ["a"]) == 1
    assert rel[0][0] == ("s", ("1001", "26-001"), ["a"])
    rec = _recorder(monkeypatch, eng, "reconcile_with_pairbot", {"sent": {}}, is_async=True)
    assert asyncio.run(fx.reconcile(["1001"], ["a"], session_id="s", own_bulk_ids=["b"])) == {"sent": {}}
    assert rec[0][1] == {"session_id": "s", "own_bulk_ids": ["b"]}


def test_provisioning_side_effects_and_timings(monkeypatch):
    fx = Effects()
    monkeypatch.delenv("JOBDIVA_PROVISIONING_ENABLED", raising=False)
    assert fx.provisioning_enabled() is True
    prov = _recorder(monkeypatch, eng, "provision_sent_candidates", {"success": 1}, is_async=True)
    assert asyncio.run(fx.provision("26-001", ["a"])) == {"success": 1}
    assert prov[0][0] == ("26-001", ["a"])
    fire = _recorder(monkeypatch, eng, "fire_launch_side_effects", True)
    assert fx.fire_side_effects("26-001", sent_count=2, notify_recruiters=True, send_job_posting_email=False,
                                is_initial_launch=True, app_base_url="https://x") is True
    assert fire[0][1]["sent_count"] == 2
    clicked = _recorder(monkeypatch, launch_timing, "stamp_launch_clicked", "1001")
    at = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)
    fx.stamp_launch_clicked("1001", at)
    assert clicked[0] == (("1001",), {"at": at})
    first = _recorder(monkeypatch, launch_timing, "stamp_first_launch", "1001")
    fx.stamp_first_launch("1001")
    assert first[0][0] == ("1001",)
    timing = _recorder(monkeypatch, launch_timing, "record_timing_event", {})
    fx.record_timing_event({"id": "s"}, job_first_launch_at=None)
    assert timing[0] == (({"id": "s"},), {"job_first_launch_at": None})


def test_real_launch_timing_rejects_the_old_call_shape():
    with pytest.raises(TypeError):
        launch_timing.stamp_launch_clicked("1001", at="not a datetime")
