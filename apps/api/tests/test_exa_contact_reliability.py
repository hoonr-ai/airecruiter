"""Exa-sourced candidates actually get contact details (2026-09-28).

Report: "we are not getting any contact details for anyone coming from exa".
Exa rows carry only a name + LinkedIn URL, so they depend on the chain
ZoomInfo (name) -> Apollo (URL) -> paid Exa Agent (URL). Live checks that day:
Apollo answered 422 "insufficient credits" on every call and ZoomInfo had no
unique name match, so the Exa Agent was the only step that could work -- and:

  - its 25s poll budget discarded phone lookups (measured: email 9s, phone 52s),
    while Exa still billed the abandoned runs;
  - sourcing lookups shared the deep-search agent's single concurrency slot, so
    a job's lookups ran one at a time;
  - Pass A ran the lookup BEFORE emitting, holding each row off screen and
    buying contacts for rows the score gate then dropped.
"""
import asyncio

import httpx

import core.config as core_config
from core import sourcing_config
from services import contact_enrichment as ce
from services import sourced_candidates_storage
from services.unified_candidate_search import SearchCriteria, UnifiedCandidateSearch

LINKEDIN = "https://www.linkedin.com/in/jane-doe"


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _client(responses, calls):
    """httpx.AsyncClient stand-in: serves `responses` to POSTs in order."""
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(url)
            return responses.pop(0)

    return _Client


# ---------------------------------------------------------------------------
# Timeout / concurrency defaults
# ---------------------------------------------------------------------------

def test_exa_poll_budget_fits_a_phone_lookup():
    assert core_config.EXA_CONTACT_ENRICH_TIMEOUT_S >= 60


def test_contact_lookups_do_not_borrow_the_deep_search_slot(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_CONCURRENCY", 1)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_CONCURRENCY", 3)
    monkeypatch.setattr(ce, "_EXA_SEMAPHORE", None)

    assert ce._exa_semaphore()._value == 3


# ---------------------------------------------------------------------------
# Apollo out of credits: said loudly, then skipped
# ---------------------------------------------------------------------------

def test_apollo_out_of_credits_trips_a_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "APOLLO_API_KEY", "k")
    monkeypatch.setattr(ce, "_apollo_no_credits_until", 0.0)
    monkeypatch.setattr(ce.httpx, "AsyncClient", _client(
        [_Resp(422, text='{"error":"You have insufficient credits! Upgrade your plan."}')], calls,
    ))

    first = asyncio.run(ce.apollo_enrich_by_linkedin("c1", LINKEDIN))
    second = asyncio.run(ce.apollo_enrich_by_linkedin("c2", LINKEDIN))

    assert first == {"ok": False, "message": "Apollo out of credits"}
    assert second == {"ok": False, "message": "Apollo out of credits"}
    assert len(calls) == 1  # the second lookup never went on the wire
    assert ce.apollo_out_of_credits()


def test_other_apollo_errors_do_not_trip_the_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "APOLLO_API_KEY", "k")
    monkeypatch.setattr(ce, "_apollo_no_credits_until", 0.0)
    monkeypatch.setattr(ce.httpx, "AsyncClient", _client(
        [_Resp(422, text='{"error":"invalid linkedin_url"}')], calls,
    ))

    res = asyncio.run(ce.apollo_enrich_by_linkedin("c1", LINKEDIN))

    assert res["ok"] is False and "422" in res["message"]
    assert not ce.apollo_out_of_credits()


def test_sourcing_chain_still_reaches_exa_when_apollo_is_dry(monkeypatch):
    monkeypatch.setattr(ce, "_apollo_no_credits_until", 0.0)
    monkeypatch.setattr(sourcing_config, "EXA_SOURCING_CONTACT_FALLBACK", True)
    monkeypatch.setattr(ce, "APOLLO_API_KEY", "k")
    monkeypatch.setattr(ce.httpx, "AsyncClient", _client(
        [_Resp(422, text="You have insufficient credits! Upgrade your plan.")], [],
    ))

    async def _zi(full_name, company=""):
        return {}

    exa_calls = []

    async def _exa(candidate_id, linkedin_url, full_name="", company="", fields=ce.EXA_CONTACT_FIELDS):
        exa_calls.append(tuple(fields))
        return {"ok": True, "fields": {"workEmail": "jane@acme.com", "mobilePhone": "",
                                       "workPhone": "", "personalEmail": ""}}

    monkeypatch.setattr(ce, "_zoominfo_enrich_for_sourcing", _zi)
    monkeypatch.setattr(ce, "exa_enrich_by_linkedin", _exa)
    ce.reset_job_counter("job-dry", include_lifetime=True)

    res = asyncio.run(ce.enrich_contact_for_sourcing(
        LINKEDIN, "job-dry", full_name="Jane Doe", include_exa=True, want_phone=False,
    ))

    assert res["provider_used"] == "exa"
    assert res["workEmail"] == "jane@acme.com"
    # Sourcing never buys the phone ($0.07 of the run) — email only.
    assert exa_calls == [("email",)]


# ---------------------------------------------------------------------------
# Exa create: a 429 burst is retried, not lost
# ---------------------------------------------------------------------------

def test_exa_create_429_is_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "_EXA_CREATE_429_BACKOFF_S", 0)
    monkeypatch.setattr(ce.httpx, "AsyncClient", _client([
        _Resp(429, text="rate limited"),
        _Resp(200, {"id": "run-1", "status": "completed",
                    "output": {"structured": {"contact": {"email": "jane@acme.com"}}}}),
    ], calls))

    res = asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe"))

    assert res["ok"] is True
    assert res["fields"]["workEmail"] == "jane@acme.com"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Pass A (LinkedIn-Exa): show the row first, then fill contact by patch
# ---------------------------------------------------------------------------

def _exa_row(name, slug):
    return {
        "id": f"exa-{slug}",
        "candidate_id": f"exa-{slug}",
        "name": name,
        "title": "Data Engineer",
        "profile_url": f"https://www.linkedin.com/in/{slug}",
        "source": "LinkedIn-Exa",
    }


def test_exa_rows_are_shown_before_their_contact_lookup(monkeypatch):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    monkeypatch.setattr(sourcing_config, "EXTERNAL_SOURCE_MIN_SCORE", 60)
    monkeypatch.setenv("EXA_DEEP_ANALYSIS_ENABLED", "false")

    async def _no_llm(cand):
        return cand

    monkeypatch.setattr(sourced_candidates_storage, "process_linkedin_candidate", _no_llm)

    lookups = []

    async def _chain(linkedin_url, jobdiva_id=None, **kwargs):
        lookups.append((linkedin_url, kwargs))
        return {"workEmail": "shown@acme.com", "mobilePhone": "", "provider_used": "exa"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _chain)

    svc = UnifiedCandidateSearch()

    async def _pass_a(criteria):
        return {"candidates": [_exa_row("Shown Person", "shown"), _exa_row("Weak Match", "weak")],
                "source_type": "LinkedIn-Exa"}

    def _policy(cand, criteria):
        cand["match_score"] = 10 if cand["name"] == "Weak Match" else 90
        return cand

    svc._search_exa = _pass_a
    svc.apply_scoring_policy = _policy
    svc._candidate_title_match = lambda cand, criteria: True
    svc._candidate_outside_years_range_pre_llm = lambda cand, criteria: False
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }

    async def _run():
        return [ev async for ev in svc.search_candidates(SearchCriteria(job_id="job-a", sources=["Exa"]))]

    events = asyncio.run(_run())

    shown = [ev["data"] for ev in events if ev.get("type") == "candidate"]
    assert [c["name"] for c in shown] == ["Shown Person"]
    # Only the shown row was looked up; the score-gated row cost nothing.
    assert [url for url, _ in lookups] == ["https://www.linkedin.com/in/shown"]
    assert lookups[0][1]["include_exa"] is True

    patches = [ev for ev in events
               if ev.get("type") == "candidate_detail" and ev.get("stage") == "contact_enrichment"]
    assert patches == [{
        "type": "candidate_detail",
        "candidate_id": "exa-shown",
        "stage": "contact_enrichment",
        "patch": {"email": "shown@acme.com"},
    }]
    row_idx = next(i for i, ev in enumerate(events) if ev.get("type") == "candidate")
    assert row_idx < events.index(patches[0])


# ---------------------------------------------------------------------------
# Spend policy: LinkedIn sources at/above 60% only
# ---------------------------------------------------------------------------

def test_block_reason_policy(monkeypatch):
    monkeypatch.setattr(sourcing_config, "CONTACT_ENRICH_SOURCE_PREFIXES", ("LinkedIn", "JobDiva"))
    monkeypatch.setattr(sourcing_config, "CONTACT_ENRICH_UNSCORED_OK_PREFIXES", ("JobDiva",))
    monkeypatch.setattr(sourcing_config, "CONTACT_ENRICH_MIN_SCORE", 60)
    ok = ce.contact_lookup_block_reason

    assert ok("LinkedIn-Exa", 60) == ""
    assert ok("LinkedIn-DeepSearch", 91.5) == ""
    assert ok("LinkedIn-Unipile", 75) == ""
    assert "below 60" in ok("LinkedIn-Exa", 59)
    assert "unscored" in ok("LinkedIn-Exa", None)
    # JobDiva candidates missing contact are eligible too (user, 2026-09-28);
    # the floor holds when they carry a score, unscored JobAgent rows pass.
    assert ok("JobDiva-JobAgent", 95) == ""
    assert ok("JobDiva-Applicants", None) == ""
    assert "below 60" in ok("JobDiva-TalentSearch", 40)
    assert "not an eligible source" in ok("Dice", 95)
    assert "not an eligible source" in ok("", 95)


def _run_pass_a(monkeypatch, rows, scores):
    monkeypatch.setattr(sourcing_config, "EXA_AGENT_ENABLED", False)
    monkeypatch.setattr(sourcing_config, "EXTERNAL_SOURCE_MIN_SCORE", 60)
    monkeypatch.setenv("EXA_DEEP_ANALYSIS_ENABLED", "false")

    async def _no_llm(cand):
        return cand

    monkeypatch.setattr(sourced_candidates_storage, "process_linkedin_candidate", _no_llm)
    lookups = []

    async def _chain(linkedin_url, jobdiva_id=None, **kwargs):
        lookups.append(linkedin_url)
        return {"workEmail": "x@acme.com", "provider_used": "apollo"}

    monkeypatch.setattr(ce, "enrich_contact_for_sourcing", _chain)
    svc = UnifiedCandidateSearch()

    async def _pass_a(criteria):
        return {"candidates": rows, "source_type": "LinkedIn-Exa"}

    def _policy(cand, criteria):
        cand["match_score"] = scores[cand["name"]]
        return cand

    svc._search_exa = _pass_a
    svc.apply_scoring_policy = _policy
    svc._candidate_title_match = lambda cand, criteria: True
    svc._candidate_outside_years_range_pre_llm = lambda cand, criteria: False
    svc._filter_assessment = lambda cand, criteria, enforce_years=False: {
        "passes": True, "matched": [], "missing": [], "excluded": [], "score": 0,
    }

    async def _run():
        return [ev async for ev in svc.search_candidates(SearchCriteria(job_id="job-p", sources=["Exa"]))]

    return asyncio.run(_run()), lookups


def test_unscored_exa_rows_are_shown_but_never_looked_up(monkeypatch):
    events, lookups = _run_pass_a(
        monkeypatch,
        [_exa_row("Unscored", "unscored"), _exa_row("Good", "good")],
        {"Unscored": None, "Good": 80},
    )
    shown = {ev["data"]["name"] for ev in events if ev.get("type") == "candidate"}
    assert shown == {"Unscored", "Good"}  # the display gate keeps unscored rows
    assert lookups == ["https://www.linkedin.com/in/good"]


# ---------------------------------------------------------------------------
# Launch PAIR's enrichment pass honours the same policy
# ---------------------------------------------------------------------------

def _launch_enrich(monkeypatch, **req):
    from routers import candidates as candidates_router

    calls = []

    async def _apollo(candidate_id, linkedin_url):
        calls.append("apollo")
        return {"ok": False}

    async def _zi(*a, **k):
        calls.append("zoominfo")
        return {"ok": False}

    monkeypatch.setattr(candidates_router, "_apollo_enrich_by_linkedin", _apollo)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_name", _zi)
    monkeypatch.setattr(ce, "zoominfo_enrich_by_email", _zi)

    def _no_db():
        raise RuntimeError("no db in tests")

    monkeypatch.setattr(candidates_router, "get_db_connection", _no_db)
    body = candidates_router.EnrichCandidateContactRequest(
        linkedin_url=LINKEDIN, full_name="Jane Doe", **req
    )
    res = asyncio.run(candidates_router._enrich_candidate_contact_impl("c1", body))
    return res, calls


def test_launch_skips_ineligible_sources_and_low_scores(monkeypatch):
    for req in (
        {"trigger": "launch", "source": "Dice", "match_score": 95},
        {"trigger": "launch", "source": "JobDiva-TalentSearch", "match_score": 30},
        {"trigger": "launch", "source": "LinkedIn-Exa", "match_score": 45},
        {"trigger": "launch", "source": "LinkedIn-Exa"},  # unscored
    ):
        res, calls = _launch_enrich(monkeypatch, **req)
        assert res["status"] == "skipped", req
        assert calls == [], req


def test_launch_looks_up_eligible_rows_and_buttons_stay_ungated(monkeypatch):
    res, calls = _launch_enrich(monkeypatch, trigger="launch", source="LinkedIn-Exa", match_score=72)
    assert res["status"] == "success" and "apollo" in calls
    res, calls = _launch_enrich(monkeypatch, trigger="launch", source="JobDiva-JobAgent")
    assert res["status"] == "success" and "apollo" in calls

    # No trigger = a deliberate click (phone button / Rankings): not gated.
    res, calls = _launch_enrich(monkeypatch, source="JobDiva-JobAgent", match_score=10)
    assert res["status"] == "success" and "apollo" in calls


# ---------------------------------------------------------------------------
# Exa is the last provider: a failed lookup gets one second chance
# ---------------------------------------------------------------------------

class _ExaServer:
    """httpx.AsyncClient stand-in for the Exa Agent API: POST creates a run,
    GET polls it. Queued answers; an Exception in a queue is raised."""

    def __init__(self, creates, polls=(), keep_polling=None):
        self.creates = list(creates)
        self.polls = list(polls)
        self.keep_polling = keep_polling
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(("create", ""))
        answer = self.creates.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get(self, url, headers=None):
        self.calls.append(("poll", url.rsplit("/", 1)[-1]))
        return self.polls.pop(0) if self.polls else self.keep_polling


DONE = {"status": "completed", "output": {"structured": {"contact": {"email": "jane@acme.com"}}}}


def _run(run_id, **fields):
    return _Resp(200, {"id": run_id, **fields})


def _exa(monkeypatch, creates, polls=(), keep_polling=None):
    server = _ExaServer(creates, polls, keep_polling)
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_ENABLED", True)
    monkeypatch.setattr(ce, "EXA_API_KEY", "test-key")
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_RETRY", True)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(ce, "_EXA_CREATE_429_BACKOFF_S", 0)
    monkeypatch.setattr(ce.httpx, "AsyncClient", server)
    return server


def _lookup():
    return asyncio.run(ce.exa_enrich_by_linkedin("c1", LINKEDIN, "Jane Doe"))


def test_exa_failed_run_gets_one_new_run(monkeypatch):
    server = _exa(monkeypatch, [_run("run-1", status="failed"), _run("run-2", **DONE)])

    res = _lookup()

    assert res["ok"] is True and res["fields"]["workEmail"] == "jane@acme.com"
    assert [kind for kind, _ in server.calls] == ["create", "create"]


def test_exa_network_error_on_create_gets_one_new_run(monkeypatch):
    server = _exa(monkeypatch, [httpx.ConnectError("boom"), _run("run-1", **DONE)])

    assert _lookup()["ok"] is True
    assert [kind for kind, _ in server.calls] == ["create", "create"]


def test_exa_failed_poll_keeps_watching_the_same_run(monkeypatch):
    # The run is still going (and billed): watch it again, don't pay for a new one.
    server = _exa(monkeypatch, [_run("run-1", status="running")], polls=[_Resp(503), _run("run-1", **DONE)])

    res = _lookup()

    assert res["ok"] is True and res["fields"]["workEmail"] == "jane@acme.com"
    assert server.calls == [("create", ""), ("poll", "run-1"), ("poll", "run-1")]


def test_exa_timed_out_run_is_watched_once_more_not_bought_again(monkeypatch):
    seen = []

    async def _attempt(client, headers, body, candidate_id, run_id="", timeout_s=None):
        seen.append(run_id)
        if len(seen) == 1:
            return {"message": "Exa run timed out", "retry": "wait", "run_id": "run-1"}
        return {"result": {"ok": True, "fields": {"workEmail": "jane@acme.com"}}}

    _exa(monkeypatch, [])
    monkeypatch.setattr(ce, "_exa_contact_attempt", _attempt)

    assert _lookup()["ok"] is True
    assert seen == ["", "run-1"]


def test_exa_attempt_reports_a_timeout_as_wait_on_the_same_run(monkeypatch):
    _exa(monkeypatch, [_run("run-1", status="running")], keep_polling=_run("run-1", status="running"))
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_TIMEOUT_S", 1)
    monkeypatch.setattr(ce, "_EXA_POLL_INTERVAL_S", 0.05)

    async def _go():
        async with ce.httpx.AsyncClient() as client:
            return await ce._exa_contact_attempt(client, {}, {}, "c1")

    outcome = asyncio.run(_go())

    assert outcome == {"message": "Exa run timed out", "retry": "wait", "run_id": "run-1"}


def test_exa_retries_only_once(monkeypatch):
    server = _exa(monkeypatch, [_run("run-1", status="failed"), _run("run-2", status="cancelled")])

    assert _lookup() == {"ok": False, "message": "Exa run cancelled"}
    assert len(server.calls) == 2


def test_exa_create_5xx_is_retried_but_4xx_is_not(monkeypatch):
    server = _exa(monkeypatch, [_Resp(502, text="bad gateway"), _run("run-1", **DONE)])
    assert _lookup()["ok"] is True and len(server.calls) == 2

    server = _exa(monkeypatch, [_Resp(402, text="NO_MORE_CREDITS")])
    # fatal: a deep search would hit the same 4xx, so the chain does not try one.
    assert _lookup() == {"ok": False, "message": "Exa create error (402)", "fatal": True}
    assert len(server.calls) == 1


def test_exa_clean_miss_is_an_answer_not_retried(monkeypatch):
    server = _exa(monkeypatch, [_run("run-1", status="completed", output={"structured": {"contact": {}}})])

    res = _lookup()

    assert res["ok"] is True and not ce._has_usable_field(res["fields"])
    assert len(server.calls) == 1


def test_exa_retry_can_be_switched_off(monkeypatch):
    server = _exa(monkeypatch, [_run("run-1", status="failed")])
    monkeypatch.setattr(ce, "EXA_CONTACT_ENRICH_RETRY", False)

    assert _lookup() == {"ok": False, "message": "Exa run failed"}
    assert len(server.calls) == 1
