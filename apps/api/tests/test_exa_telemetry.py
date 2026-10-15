"""Exa spend telemetry (services/exa_telemetry.py) and the contact path's
run metadata + its 400 guard (2026-10-15, docs/exa-cost-optimization.md).

Pins:
  - record_exa_call never raises, is a no-op when switched off, and every
    attribute name it uses survives core.newrelic.safe_attributes (which
    silently drops keys containing name/url/text/contact/content/resume ...)
  - cost parsing from SDK objects and raw run JSON, fractional ACU included
  - the request-scoped context: scope() resets, override=False keeps an outer
    caller's binding, bind_if_unbound() is a bare set; who-asked binding at
    the Step-5 search and the on-demand contact lookup
  - Agent run metadata: string values only, a fresh call_id per billed run
  - the guard: a 400 with metadata is re-sent once without it; accepted =>
    latched off for the process (ERROR once); the deep search keeps Fiber
  - ExaConfig once per worker per hour, only once New Relic took it
  - ExaCall per contact attempt / late watch, with cost from the run JSON
"""
import asyncio
import logging
import time
import types

import pytest

from core import config as core_config
from core import newrelic as nr
from core import pipeline_trace
from core import sourcing_config
from routers import candidates as candidates_router
from services import contact_enrichment as ce
from services import exa_telemetry as tele
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch
from tests.test_jobagent_quick_first import _patch_service_for_orchestration

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


@pytest.fixture
def events(monkeypatch):
    sent = []
    monkeypatch.setattr(tele, "_send", lambda event_type, attrs: sent.append((event_type, attrs)))
    return sent


def _calls(events, **match):
    return [a for t, a in events if t == "ExaCall" and all(a.get(k) == v for k, v in match.items())]


# ---------------------------------------------------------------------------
# record_exa_call
# ---------------------------------------------------------------------------

ALL_ATTRS = dict(
    cost_total=0.1, cost_search=0.01, cost_pages=0.02, cost_compute=0.03, cost_fiber=0.04,
    cost_fee_e=0.02, cost_fee_p=0.07, acu=0.4, num_results=50, n_results=12,
    fields_asked="email,phone", fields_found="phone", http_status=402, error_kind="NO_MORE_CREDITS",
    exa_run_id="run_123", call_id="abc", effort="low", tier="deep", path="ondemand", trigger="launch",
    arm="skip", defer=True, late=True, latency_ms=1234, launch_session_id="sess-1", attempt=2,
    rewatch=True, job_id="26-1",
)


def test_every_attribute_name_survives_safe_attributes(events):
    tele.record_exa_call("contact", "agent_run", "completed", **ALL_ATTRS)

    [attrs] = _calls(events)
    for key, value in ALL_ATTRS.items():
        assert attrs.get(key) == value, key
    assert attrs["surface"] == "contact" and attrs["endpoint"] == "agent_run" and attrs["outcome"] == "completed"


def test_record_drops_pii_and_none_and_never_raises(monkeypatch, events):
    tele.record_exa_call(
        "contact", "agent_run", "completed",
        candidate_name="Jane", linkedin_url=LINKEDIN, query_text="x", error_kind="a@b.com",
        http_status=None, fields_asked=("email", "phone"),
    )
    [attrs] = _calls(events)
    assert not {"candidate_name", "linkedin_url", "query_text", "error_kind", "http_status"} & set(attrs)
    assert attrs["fields_asked"] == "email,phone"

    def _boom(*_a, **_k):
        raise RuntimeError("nr down")

    monkeypatch.setattr(tele, "_send", _boom)
    tele.record_exa_call("pass_a", "search", "ok", latency_ms=1)  # no exception


def test_record_is_a_no_op_when_switched_off(monkeypatch, events):
    monkeypatch.setattr(core_config, "EXA_TELEMETRY_ENABLED", False)
    tele.record_exa_call("pass_a", "search", "ok")
    tele.record_config_once()
    assert events == []


def test_record_reads_the_bound_context_and_the_pipeline_run(events):
    async def go():
        pipeline_trace.new_run("26-7")
        with tele.scope(trigger="launch_s1", attempt=0, launch_session_id="s-1"):
            tele.record_exa_call("pass_a", "search", "ok")
        tele.record_exa_call("pass_a", "search", "ok")

    asyncio.run(go())
    inside, outside = _calls(events)
    assert inside["trigger"] == "launch_s1" and inside["attempt"] == 0 and inside["launch_session_id"] == "s-1"
    assert inside["job_id"] == "26-7" and inside["run_id"].startswith("26-7:")
    assert "trigger" not in outside and outside["job_id"] == "26-7"


# ---------------------------------------------------------------------------
# Cost parsing
# ---------------------------------------------------------------------------

def test_cost_from_search_response_sums_the_breakdowns():
    from exa_py.api import CostDollars

    resp = types.SimpleNamespace(cost_dollars=CostDollars(
        total=0.177, search={"neural": 0.047}, contents={"text": 0.05, "highlights": 0.08},
    ))
    assert tele.cost_from_search_response(resp) == {
        "cost_total": 0.177, "cost_search": 0.047, "cost_pages": 0.13,
    }
    assert tele.cost_from_search_response(types.SimpleNamespace(results=[])) == {}
    assert tele.cost_from_search_response(types.SimpleNamespace(cost_dollars={"total": "0.007"})) == {"cost_total": 0.007}


def test_cost_from_run_json_raw_and_sdk_with_fractional_acu():
    run = {
        "costDollars": {"total": 0.142, "agentCompute": 0.1, "search": 0.0, "emails": 0.02,
                        "phoneNumbers": 0.0, "dataSources": {"fiber": 0.022}},
        "usage": {"agentComputeUnits": 0.4},
    }
    assert tele.cost_from_run_json(run) == {
        "cost_total": 0.142, "cost_compute": 0.1, "cost_search": 0.0, "cost_fee_e": 0.02,
        "cost_fee_p": 0.0, "cost_fiber": 0.022, "acu": 0.4,
    }
    sdk = types.SimpleNamespace(
        cost_dollars=types.SimpleNamespace(total=0.1, agent_compute=0.1, data_sources=None),
        usage=types.SimpleNamespace(agent_compute_units=0.4),
    )
    assert tele.cost_from_run_json(sdk) == {"cost_total": 0.1, "cost_compute": 0.1, "acu": 0.4}
    assert tele.cost_from_run_json({"id": "run-1", "status": "completed"}) == {}
    assert tele.cost_from_run_json(None) == {}


def test_classify_exc():
    from services.exa_errors import ExaNoSlotError, ExaOutOfCreditsError

    assert tele.classify_exc(ExaOutOfCreditsError("NO_MORE_CREDITS")) == {
        "outcome": "out_of_credits", "http_status": 402, "error_kind": "NO_MORE_CREDITS"}
    assert tele.classify_exc(ExaOutOfCreditsError(skipped=True)) == {"outcome": "breaker_open"}
    assert tele.classify_exc(ExaNoSlotError("x")) == {"outcome": "no_slot"}
    assert tele.classify_exc(asyncio.TimeoutError()) == {"outcome": "timeout"}
    assert tele.classify_exc(ValueError("Request failed with status code 429: slow down"))["outcome"] == "rate_limited"
    assert tele.classify_exc(ValueError("Request failed with status code 500: oops")) == {
        "outcome": "error", "http_status": 500, "error_kind": "ValueError"}


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def test_scope_resets_and_override_false_keeps_the_outer_binding():
    with tele.scope(trigger="launch_s3", launch_session_id="s"):
        with tele.scope(override=False, trigger="launch", path="ondemand", job_id="J", bogus="x"):
            assert tele.current() == {"trigger": "launch_s3", "launch_session_id": "s", "path": "ondemand", "job_id": "J"}
        with tele.scope(trigger="click", tier=None):
            assert tele.current()["trigger"] == "click" and "tier" not in tele.current()
        assert tele.current() == {"trigger": "launch_s3", "launch_session_id": "s"}
    assert tele.current() == {}


def test_bind_if_unbound_is_a_bare_set_of_the_missing_keys():
    async def go():
        tele.bind_if_unbound(trigger="step5_full")
        tele.bind_if_unbound(trigger="autosync", path="x")
        return tele.current()

    assert asyncio.run(go()) == {"trigger": "step5_full", "path": "x"}
    assert tele.current() == {}  # asyncio.run's task had its own context


def test_step5_search_binds_its_trigger_unless_a_session_did(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    seen = []

    async def _exa(criteria):
        seen.append(tele.current().get("trigger"))
        return {"candidates": [], "source_type": "LinkedIn-Exa"}

    def drive(mode, outer=None):
        svc = UnifiedCandidateSearch()
        _patch_service_for_orchestration(svc, {"calls": [], "full_returned": False})
        svc._search_exa = _exa

        async def go():
            with tele.scope(trigger=outer):
                async for _ in svc.search_candidates(SearchCriteria(job_id="J1", sources=["Exa"], search_mode=mode)):
                    pass

        asyncio.run(go())

    drive("sample")
    drive("full")
    drive("full", outer="launch_s1")
    assert seen == ["step5_sample", "step5_full", "launch_s1"]


def test_ondemand_lookup_binds_trigger_path_and_job(monkeypatch):
    seen = []

    async def _chain(candidate_id, request):
        seen.append(tele.current())
        return {}

    monkeypatch.setattr(candidates_router, "_enrich_candidate_contact_chain", _chain)
    Req = candidates_router.EnrichCandidateContactRequest

    async def go():
        await candidates_router._enrich_candidate_contact_impl("c", Req(fields=["phone"], jobdiva_id="J1"))
        await candidates_router._enrich_candidate_contact_impl("c", Req(trigger="launch"))
        await candidates_router._enrich_candidate_contact_impl("c", Req())
        with tele.scope(trigger="launch_s3", launch_session_id="s"):
            await candidates_router._enrich_candidate_contact_impl("c", Req(trigger="launch", jobdiva_id="J2"))

    asyncio.run(go())
    assert seen[0] == {"trigger": "click_phone", "path": "ondemand", "job_id": "J1"}
    assert seen[1] == {"trigger": "launch", "path": "ondemand"}
    assert seen[2]["trigger"] == "click"
    assert seen[3] == {"trigger": "launch_s3", "launch_session_id": "s", "path": "ondemand", "job_id": "J2"}


# ---------------------------------------------------------------------------
# Agent run metadata
# ---------------------------------------------------------------------------

def test_agent_metadata_is_short_strings_with_a_fresh_call_id():
    with tele.scope(trigger="launch", tier="deep", attempt=3, job_id="x" * 100, defer=True):
        meta = tele.agent_metadata("contact")
        again = tele.agent_metadata("contact")
    assert all(isinstance(v, str) and 0 < len(v) <= 64 for v in meta.values())
    assert meta["surface"] == "contact" and meta["tier"] == "deep" and meta["attempt"] == "3"
    assert meta["trigger"] == "launch" and meta["env"] == nr.environment() and len(meta["job_id"]) == 64
    assert "defer" not in meta and "path" not in meta  # unbound / not a metadata key
    assert meta["call_id"] != again["call_id"] and len(meta["call_id"]) == 32

    body = {"query": "q", "metadata": meta}
    fresh = tele.with_fresh_call_id(body)
    assert fresh["metadata"]["call_id"] != meta["call_id"] and body["metadata"] is meta
    assert tele.with_fresh_call_id({"query": "q"}) == {"query": "q"}


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


def _exa_client(bodies, answers, polls=()):
    answers, polls = list(answers), list(polls)

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            bodies.append(json)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def get(self, url, headers=None, params=None):
            return polls.pop(0)

    return _Client


COST = {"total": 0.115, "agentCompute": 0.025, "emails": 0.02, "phoneNumbers": 0.07}
FOUND = _Resp(200, {"id": "run-1", "status": "completed", "costDollars": COST, "usage": {"agentComputeUnits": 0.4},
                    "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}})
REJECTED = _Resp(400, {"error": "metadata: unknown field"}, text='{"error":"metadata: unknown field"}')


@pytest.fixture
def exa_env(monkeypatch):
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_EFFORT", "low")
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_EFFORT", "medium")
    monkeypatch.setattr(ce, "EXA_CONTACT_DEEP_DATA_SOURCES", "fiber")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_RETRY", True)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0)
    from core.vendor_limiter import EXA

    monkeypatch.setattr(EXA, "min_interval_s", 0.0)

    def install(answers, polls=()):
        bodies = []
        monkeypatch.setattr(ce.httpx, "AsyncClient", _exa_client(bodies, answers, polls))
        return bodies

    return install


def _lookup(**kw):
    return asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", **kw))


def test_every_create_carries_string_metadata(exa_env, events):
    bodies = exa_env([FOUND])

    async def go():
        with tele.scope(trigger="launch", path="ondemand", job_id="26-1"):
            return await ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",), deep=True)

    assert asyncio.run(go())["ok"]
    meta = bodies[0]["metadata"]
    assert meta["surface"] == "contact" and meta["tier"] == "deep" and meta["trigger"] == "launch"
    assert meta["path"] == "ondemand" and meta["job_id"] == "26-1"
    assert all(isinstance(v, str) for v in meta.values())
    [call] = _calls(events, endpoint="agent_run")
    assert call["call_id"] == meta["call_id"]  # joins the run list to the event


def test_metadata_kill_switch(exa_env, monkeypatch):
    monkeypatch.setattr(core_config, "EXA_RUN_METADATA_ENABLED", False)
    bodies = exa_env([FOUND])
    assert _lookup(fields=("phone",))["ok"]
    assert "metadata" not in bodies[0]


def test_a_new_run_gets_a_new_call_id(exa_env):
    bodies = exa_env([_Resp(200, {"id": "run-1", "status": "failed"}), FOUND])
    assert _lookup(fields=("phone",))["ok"]
    assert bodies[0]["metadata"]["call_id"] != bodies[1]["metadata"]["call_id"]


def test_guard_normal_tier_400_retries_without_metadata_and_latches(exa_env, events, caplog):
    bodies = exa_env([REJECTED, FOUND, FOUND])

    with caplog.at_level(logging.ERROR, logger="services.exa_telemetry"):
        first = _lookup(fields=("phone",))
        second = _lookup(fields=("phone",))

    assert first["ok"] and second["ok"]
    assert "metadata" in bodies[0] and "metadata" not in bodies[1]
    assert {k: v for k, v in bodies[0].items() if k != "metadata"} == bodies[1]  # same body otherwise
    assert not tele.run_metadata_enabled()
    assert "metadata" not in bodies[2]  # latched for the process
    assert sum("rejected run metadata" in r.getMessage() for r in caplog.records) == 1
    assert len(_calls(events, endpoint="agent_guard", outcome="metadata_rejected")) == 1
    assert len(_calls(events, endpoint="agent_run", outcome="create_rejected")) == 1


def test_guard_deep_tier_keeps_fiber_when_only_the_metadata_was_refused(exa_env, events):
    bodies = exa_env([REJECTED, FOUND])

    res = _lookup(fields=("phone",), deep=True)

    assert res["ok"] and res["fields"]["mobilePhone"] == "+14155550100"
    assert len(bodies) == 2
    assert bodies[1]["dataSources"] == [{"provider": "fiber"}] and "metadata" not in bodies[1]
    assert "Fiber.ai people database" in bodies[1]["query"]
    assert not tele.run_metadata_enabled()
    assert not _calls(events, outcome="sources_refused")


def test_a_400_that_is_not_the_metadata_does_not_latch(exa_env, events):
    bodies = exa_env([REJECTED, REJECTED])

    res = _lookup(fields=("phone",))

    assert res == {"ok": False, "message": "Exa create error (400)", "fatal": True}
    assert len(bodies) == 2 and "metadata" not in bodies[1]
    assert tele.run_metadata_enabled()
    assert len(_calls(events, endpoint="agent_guard", outcome="rejected")) == 1


def test_guard_does_not_latch_on_an_ambiguous_retry(exa_env):
    exa_env([REJECTED, _Resp(503, text="busy"), FOUND])
    assert _lookup(fields=("phone",))["ok"]
    assert tele.run_metadata_enabled()


# ---------------------------------------------------------------------------
# ExaCall per contact attempt / late watch
# ---------------------------------------------------------------------------

def test_completed_contact_run_records_cost_and_fields(exa_env, events):
    exa_env([FOUND])

    async def go():
        with tele.scope(trigger="click_phone", path="ondemand"):
            return await ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe", fields=("phone",))

    assert asyncio.run(go())["ok"]
    [call] = _calls(events, endpoint="agent_run")
    assert call["outcome"] == "completed" and call["surface"] == "contact"
    assert call["cost_total"] == 0.115 and call["cost_fee_p"] == 0.07 and call["acu"] == 0.4
    assert call["fields_found"] == "phone" and call["fields_asked"] == "phone"
    assert call["tier"] == "normal" and call["effort"] == "low" and call["exa_run_id"] == "run-1"
    assert call["trigger"] == "click_phone" and call["path"] == "ondemand"
    assert isinstance(call["latency_ms"], int)


def test_timed_out_contact_run_records_no_cost_until_the_late_watch(exa_env, events, monkeypatch):
    running = _Resp(200, {"id": "run-2", "status": "running", "costDollars": {"total": 0.01}})
    exa_env([running], polls=[running] * 50)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 1)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0.05)

    res = _lookup(fields=("phone",), wait_retry=False)

    assert res == {"ok": False, "message": "Exa run timed out", "run_id": "run-2"}
    [call] = _calls(events, endpoint="agent_run")
    assert call["outcome"] == "timeout" and call["exa_run_id"] == "run-2" and "cost_total" not in call


def test_out_of_credits_create_is_recorded_as_such(exa_env, events):
    exa_env([_Resp(402, text='{"error":"No more credits","tag":"NO_MORE_CREDITS"}')])

    assert _lookup(fields=("phone",)) == {"ok": False, "message": "Exa create error (402)", "fatal": True}
    [call] = _calls(events, endpoint="agent_run")
    assert call["outcome"] == "out_of_credits" and call["http_status"] == 402 and call["error_kind"] == "NO_MORE_CREDITS"


def test_late_watch_records_its_final_cost(monkeypatch, events):
    polls = [_Resp(200, {"id": "run-9", "status": "completed", "costDollars": {"total": 0.142},
                         "output": {"structured": {"contact": {"phone": "+1 415 555 0100"}}}})]
    monkeypatch.setattr(ce.httpx, "AsyncClient", _exa_client([], [], polls))
    monkeypatch.setattr(ce, "_EXA_LATE_POLL_S", 0)
    monkeypatch.setattr(ce, "EXA_LATE_RESULT_WAIT_S", 5)

    async def _record(*a, **k):
        return None

    from services import contact_cache

    monkeypatch.setattr(contact_cache, "record", _record)

    found = asyncio.run(ce.keep_late_exa_result("run-9", LINKEDIN, fields=("phone",), provider="exa_deep"))

    assert found == {"email": "", "phone": "+14155550100"}
    [call] = _calls(events)
    assert call["late"] is True and call["outcome"] == "completed" and call["cost_total"] == 0.142
    assert call["tier"] == "deep" and call["fields_found"] == "phone" and call["exa_run_id"] == "run-9"


# ---------------------------------------------------------------------------
# ExaConfig
# ---------------------------------------------------------------------------

def test_config_is_sent_once_per_hour_and_only_once_new_relic_took_it(monkeypatch, events):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    monkeypatch.delenv("EXA_DEEP_ANALYSIS_ENABLED", raising=False)
    monkeypatch.setattr(core_config, "EXA_RUN_METADATA_ENABLED", True)
    monkeypatch.setattr(nr, "is_enabled", lambda: False)
    tele.record_config_once()
    assert events == []  # agent not connected: not counted as sent

    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    tele.record_config_once()
    tele.record_config_once()
    configs = [a for t, a in events if t == "ExaConfig"]
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg["pass_b"] is False and cfg["deep_analysis"] is False and cfg["metadata_on"] is True
    assert cfg["breaker_ttl_s"] == 300.0 and isinstance(cfg["worker_pid"], int) and cfg["sdk_version"]

    monkeypatch.setattr(tele, "_config_sent_at", time.monotonic() - tele.CONFIG_EVERY_S - 1)
    tele.record_config_once()
    assert len([a for t, a in events if t == "ExaConfig"]) == 2


def test_config_send_failure_is_retried_next_time(monkeypatch, events):
    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    real = tele._send

    def _fail(event_type, attrs):
        raise RuntimeError("down")

    monkeypatch.setattr(tele, "_send", _fail)
    tele.record_config_once()
    monkeypatch.setattr(tele, "_send", real)
    tele.record_config_once()
    assert len([a for t, a in events if t == "ExaConfig"]) == 1


# ---------------------------------------------------------------------------
# New Relic trace propagation into the executor
# ---------------------------------------------------------------------------

def test_wrap_for_thread_is_identity_when_new_relic_is_off():
    def fn():
        return 1

    assert nr.wrap_for_thread(fn) is fn
