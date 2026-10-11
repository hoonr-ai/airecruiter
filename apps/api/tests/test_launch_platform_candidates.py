"""Unit D1 — routers/candidates.py launch-platform surface:

* build_search_criteria / iter_search_events — the /candidates/search
  internals a server-side launch runs, with the endpoint now built on them.
  Its NDJSON output is pinned against a capture from the code BEFORE the
  refactor (develop @9f5a71de); only the wall-clock gender_updated_at differs.
* enrich_contacts_for_launch — Launch PAIR's grouped contact lookup.
* save_candidates as a directly callable function, with the rescore merge fix
  (tests/test_launch_platform_sql_pg.py runs the merge on a real Postgres).
"""
import asyncio
import ast
import contextlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

import routers.candidates as rc
from core.auth import UserIdentity
from models import CandidateSaveRecord, CandidateSearchRequest, CandidatesSaveRequest
from services.gender_logic import normalize_gender_prediction


# Captured from routers/candidates.py at 9f5a71de with the fakes below.
GOLDEN = json.loads(r'''{
    "ok": {
        "chunks": [
            "{\"type\": \"stage\", \"data\": \"Searching JobDiva (JobAgent)...\"}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"0\", \"id\": \"0\", \"name\": \"Ann Lee\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 70, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"1\", \"id\": \"1\", \"name\": \"Bob Ray\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 71, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"2\", \"id\": \"2\", \"name\": \"Cy Doe\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 72, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"1\", \"stage\": \"scored\", \"patch\": {\"match_score\": 88}}\n",
            "{\"type\": \"source_status\", \"source\": \"JobDiva-JobAgent\", \"status\": \"ok\"}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"2\", \"stage\": \"dropped\", \"patch\": {}}\n",
            "{\"type\": \"summary\", \"data\": {\"summary\": {\"total\": 3}}}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"0\", \"stage\": \"gender\", \"patch\": {\"gender_label\": \"female\", \"gender_confidence\": 0.95, \"gender_source\": \"inferred_ai_name\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"2\", \"stage\": \"gender\", \"patch\": {\"gender_label\": \"male\", \"gender_confidence\": 0.95, \"gender_source\": \"inferred_ai_name\", \"gender_updated_at\": \"TS\"}}\n"
        ],
        "cross": [
            [
                "J1",
                "J1"
            ]
        ]
    },
    "boom": {
        "chunks": [
            "{\"type\": \"stage\", \"data\": \"Searching JobDiva (JobAgent)...\"}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"0\", \"id\": \"0\", \"name\": \"Ann Lee\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 70, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"1\", \"id\": \"1\", \"name\": \"Bob Ray\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 71, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate\", \"data\": {\"candidate_id\": \"2\", \"id\": \"2\", \"name\": \"Cy Doe\", \"source\": \"JobDiva-JobAgent\", \"match_score\": 72, \"gender_label\": \"default\", \"gender_confidence\": 0.0, \"gender_source\": \"unknown\", \"gender_updated_at\": \"TS\"}}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"1\", \"stage\": \"scored\", \"patch\": {\"match_score\": 88}}\n",
            "{\"type\": \"source_status\", \"source\": \"JobDiva-JobAgent\", \"status\": \"ok\"}\n",
            "{\"type\": \"candidate_detail\", \"candidate_id\": \"2\", \"stage\": \"dropped\", \"patch\": {}}\n",
            "{\"type\": \"error\", \"message\": \"producer exploded\"}\n"
        ],
        "cross": [
            [
                "J1",
                "J1"
            ]
        ]
    }
}''')

REQUESTS = {
    "ok": dict(job_id="J1", sources=["JobDiva-JobAgent"], location_type="Onsite", client_name="Acme",
               location="Austin, TX", within_miles=40, boolean_string="java AND spring"),
    "boom": dict(job_id="J1", sources=["JobDiva-JobAgent"], location_type="Onsite", client_name="Acme",
                 boolean_string="BOOM"),
}


def _install(monkeypatch) -> Dict[str, Any]:
    rec: Dict[str, Any] = {"cross": [], "criteria": []}

    async def fake_search(criteria):
        rec["criteria"].append(criteria)
        yield {"type": "stage", "data": "Searching JobDiva (JobAgent)..."}
        await asyncio.sleep(0)
        for i, name in enumerate(["Ann Lee", "Bob Ray", "Cy Doe"]):
            yield {"type": "candidate", "data": {"candidate_id": str(i), "id": str(i), "name": name,
                                                  "source": "JobDiva-JobAgent", "match_score": 70 + i}}
            await asyncio.sleep(0)
        yield {"type": "candidate_detail", "candidate_id": "1", "stage": "scored", "patch": {"match_score": 88}}
        await asyncio.sleep(0)
        yield {"type": "source_status", "source": "JobDiva-JobAgent", "status": "ok"}
        yield {"type": "candidate_detail", "candidate_id": "2", "stage": "dropped", "patch": {}}
        await asyncio.sleep(0)
        if criteria.boolean_string == "BOOM":
            raise RuntimeError("producer exploded")
        yield {"type": "summary", "data": {"summary": {"total": 3}}}

    async def fake_infer(name, threshold=0.6):
        await asyncio.sleep(0)
        label = "female" if name.startswith("Ann") else ("male" if name.startswith("Cy") else "default")
        return normalize_gender_prediction(predicted_label=label, confidence=0.95 if label != "default" else 0.0,
                                           source="inferred_ai_name", threshold=threshold)

    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda job_id, user: None)
    monkeypatch.setattr(rc.unified_search_service, "search_candidates", fake_search)
    monkeypatch.setattr(rc, "infer_gender_from_name_ai", fake_infer)
    monkeypatch.setattr(rc, "_schedule_cross_submissions",
                        lambda job, criteria: rec["cross"].append([job, criteria.job_id]))
    return rec


def _norm(line: str) -> str:
    return re.sub(r'"gender_updated_at": "[^"]+"', '"gender_updated_at": "TS"', line)


def _endpoint_lines(req) -> List[str]:
    async def go():
        resp = await rc.search_jobdiva_candidates(req, UserIdentity(email="q@x.com", role="admin"))
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return out
    return asyncio.run(go())


def _iter_events(req, **kw) -> List[Dict[str, Any]]:
    async def go():
        async with contextlib.aclosing(rc.iter_search_events(req, **kw)) as gen:
            return [e async for e in gen]
    return asyncio.run(go())


# ------------------------------------------------------------- search ----

@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_search_endpoint_output_is_identical_to_before_the_refactor(monkeypatch, name):
    rec = _install(monkeypatch)
    lines = _endpoint_lines(CandidateSearchRequest(**REQUESTS[name]))
    assert [_norm(line) for line in lines] == GOLDEN[name]["chunks"]
    assert rec["cross"] == GOLDEN[name]["cross"]


@pytest.mark.parametrize("name", sorted(REQUESTS))
def test_iter_search_events_yields_the_endpoints_lines_as_dicts(monkeypatch, name):
    rec = _install(monkeypatch)
    events = _iter_events(CandidateSearchRequest(**REQUESTS[name]))
    assert [_norm(json.dumps(e) + "\n") for e in events] == GOLDEN[name]["chunks"]
    assert rec["cross"] == [["J1", "J1"]]                     # scheduled once, like the endpoint
    if name == "ok":
        gender = [e for e in events if e.get("stage") == "gender"]
        assert {e["candidate_id"] for e in gender} == {"0", "2"}  # the router-added patches are there
    else:
        assert events[-1] == {"type": "error", "message": "producer exploded"}


def test_iter_search_events_can_skip_cross_submissions(monkeypatch):
    rec = _install(monkeypatch)
    _iter_events(CandidateSearchRequest(**REQUESTS["ok"]), schedule_cross_submissions=False)
    assert rec["cross"] == []


def test_iter_search_events_and_endpoint_search_with_identical_criteria(monkeypatch):
    rec = _install(monkeypatch)
    req = CandidateSearchRequest(**REQUESTS["ok"])
    _endpoint_lines(req)
    _iter_events(req)
    a, b = rec["criteria"]
    assert a.model_dump() == b.model_dump() if hasattr(a, "model_dump") else vars(a) == vars(b)


def test_iter_search_events_raises_when_criteria_cannot_be_built(monkeypatch):
    _install(monkeypatch)

    def boom(_req):
        raise ValueError("bad request")
    monkeypatch.setattr(rc, "build_search_criteria", boom)
    with pytest.raises(ValueError):
        _iter_events(CandidateSearchRequest(**REQUESTS["ok"]))


def test_closing_the_iterator_cancels_pending_gender_lookups(monkeypatch):
    _install(monkeypatch)
    started: List[str] = []
    cancelled: List[str] = []

    async def slow_infer(name, threshold=0.6):
        started.append(name)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise
    monkeypatch.setattr(rc, "infer_gender_from_name_ai", slow_infer)

    async def go():
        async with contextlib.aclosing(rc.iter_search_events(CandidateSearchRequest(**REQUESTS["ok"]))) as gen:
            async for e in gen:
                if e["type"] == "candidate" and e["data"]["candidate_id"] == "1":
                    break
    asyncio.run(go())
    assert started and sorted(cancelled) == sorted(started)


def test_closing_the_iterator_closes_the_search_before_aclose_returns(monkeypatch):
    """The search generator's own cleanup (it cancels the paid per-source
    work) must have run by the time the consumer's aclose() returns."""
    _install(monkeypatch)
    order: List[str] = []

    async def search(criteria):
        try:
            for i in range(5):
                yield {"type": "candidate", "data": {"candidate_id": str(i), "id": str(i), "name": "Pat Quick"}}
                await asyncio.sleep(0)
        finally:
            order.append("search-cleanup")
    monkeypatch.setattr(rc.unified_search_service, "search_candidates", search)

    async def go():
        async with contextlib.aclosing(rc.iter_search_events(CandidateSearchRequest(**REQUESTS["ok"]))) as gen:
            async for e in gen:
                if e["type"] == "candidate":
                    break
        order.append("after-aclose")
    asyncio.run(go())
    assert order == ["search-cleanup", "after-aclose"]


def test_endpoint_unserialisable_event_still_ends_with_an_error_line(monkeypatch):
    import datetime as _dt
    _install(monkeypatch)
    order: List[str] = []

    async def search(criteria):
        try:
            yield {"type": "stage", "data": "Searching"}
            yield {"type": "summary", "data": {"at": _dt.datetime(2026, 10, 11)}}
            yield {"type": "stage", "data": "never sent"}
        finally:
            order.append("search-cleanup")
    monkeypatch.setattr(rc.unified_search_service, "search_candidates", search)
    lines = _endpoint_lines(CandidateSearchRequest(**REQUESTS["ok"]))
    assert json.loads(lines[0]) == {"type": "stage", "data": "Searching"}
    assert json.loads(lines[-1]) == {"type": "error", "message": "Object of type datetime is not JSON serializable"}
    assert len(lines) == 2 and order == ["search-cleanup"]


class _JobConn:
    """monitored_jobs reads of build_search_criteria."""

    def __init__(self, location_row=None, filters_row=None):
        self.rows = {"location_type": location_row, "resume_match_filters": filters_row}
        self.sql: List[str] = []

    def __call__(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                conn.sql.append(sql)
                self._row = conn.rows["location_type" if "location_type, city" in sql else "resume_match_filters"]

            def fetchone(self):
                return self._row

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                pass
        return Conn()


def test_build_search_criteria_reads_the_same_job_fallbacks(monkeypatch):
    conn = _JobConn(location_row=("", "REMOTE"),
                    filters_row=([{"category": "certification", "value": "PMP", "active": True}], "Globex"))
    monkeypatch.setattr(rc, "get_db_connection", conn)
    resolved: Dict[str, Any] = {}
    c = rc.build_search_criteria(CandidateSearchRequest(
        job_id="J1", location="Austin, TX", within_miles=250, search_mode="sample", sample_per_source=50,
        locations=[{"value": "Austin, TX"}, {"value": "Dallas, TX", "radius": "500mi"}],
    ), _resolved=resolved)
    assert c.location_type == "Remote" and c.client_name == "Globex"
    assert c.resume_match_filters == [{"category": "certification", "value": "PMP", "active": True}]
    assert c.within_miles == 100
    assert [(a.value, a.within_miles) for a in c.additional_locations] == [("Dallas, TX", 100)]
    assert c.search_mode == "sample" and c.sample_per_source == 10 and c.page_size == 100
    assert resolved == {"location": "Austin, TX",
                        "additional_locations": [{"value": "Dallas, TX", "within_miles": 100}],
                        "location_type": "Remote", "effective_limit": 100, "require_resume": True,
                        "within_miles": 100}


def test_build_search_criteria_skips_placeholder_clients_and_db_when_given(monkeypatch):
    def no_db():
        raise AssertionError("no DB read expected")
    monkeypatch.setattr(rc, "get_db_connection", no_db)
    c = rc.build_search_criteria(CandidateSearchRequest(
        job_id="J1", location_type="Hybrid", client_name="External",
        resume_match_filters=[{"category": "certification", "value": "PMP", "active": True}]))
    assert c.location_type == "Hybrid" and c.client_name == ""


def test_search_fallback_still_uses_what_was_resolved(monkeypatch):
    """Criteria construction failing mid-way falls back exactly as before."""
    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda job_id, user: None)
    calls: List[Dict[str, Any]] = []

    class FakeJD:
        async def authenticate(self):
            return "tok"

        async def search_candidates(self, **kw):
            calls.append(kw)
            return [{"candidate_id": "x"}]
    import services.jobdiva as jd
    monkeypatch.setattr(jd, "JobDivaService", FakeJD)
    real = rc.SearchCriteria
    n = {"calls": 0}

    def flaky(**kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise TypeError("boom")
        return real(**kw)
    monkeypatch.setattr(rc, "SearchCriteria", flaky)
    import services.unified_candidate_search as ucs
    monkeypatch.setattr(ucs, "SearchCriteria", flaky)
    out = asyncio.run(rc.search_jobdiva_candidates(
        CandidateSearchRequest(job_id="J1", location="Austin, TX", location_type="Onsite", client_name="Acme",
                               limit=7, require_resume=False),
        UserIdentity(email="q@x.com", role="admin")))
    assert calls[0]["location"] == "Austin, TX" and calls[0]["limit"] == 7 and calls[0]["require_resume"] is False
    assert out["total"] == 1 and "fallback" in out["message"]


def test_search_and_enrich_endpoints_suppress_apdex(monkeypatch):
    import core.newrelic as nr
    calls: List[str] = []
    monkeypatch.setattr(nr, "suppress_apdex", lambda: calls.append("x"), raising=False)
    _install(monkeypatch)
    _endpoint_lines(CandidateSearchRequest(**REQUESTS["ok"]))
    assert calls == ["x"]

    async def impl(cid, item):
        return {"status": "success", "candidate_id": cid}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)
    asyncio.run(rc.enrich_candidate_contacts(
        rc.EnrichCandidateContactsRequest(candidates=[rc.EnrichCandidateContactRequest(candidate_id="a")]), user=None))
    assert calls == ["x", "x"]


# ------------------------------------------------------------ enrichment ----

def _items(n: int) -> List[Dict[str, Any]]:
    return [{"candidate_id": f"c{i}", "source": "LinkedIn-Exa", "match_score": 80,
             "linkedin_url": f"https://www.linkedin.com/in/c{i}", "name": "ignored", "email": "x@y.z"}
            for i in range(n)]


def _drain(gen_coro) -> List[Dict[str, Any]]:
    async def go():
        async with contextlib.aclosing(gen_coro) as gen:
            return [e async for e in gen]
    return asyncio.run(go())


def test_launch_enrichment_sends_exactly_the_launch_fields(monkeypatch):
    seen: List[Any] = []

    async def impl(cid, item):
        seen.append(item)
        return {"status": "success", "candidate_id": cid}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)
    items = _items(1) + [{"id": "d1", "source": None, "match_score": True, "linkedin_url": ""},
                         {"candidate_id": "d2", "match_score": "85"}]
    _drain(rc.enrich_contacts_for_launch(items, job_id="26-001"))
    by_id = {r.candidate_id: r.model_dump(exclude_none=True) for r in seen}
    assert by_id["c0"] == {"candidate_id": "c0", "jobdiva_id": "26-001", "source": "LinkedIn-Exa",
                           "linkedin_url": "https://www.linkedin.com/in/c0", "trigger": "launch", "match_score": 80.0}
    assert by_id["d1"] == {"candidate_id": "d1", "jobdiva_id": "26-001", "trigger": "launch"}
    assert by_id["d2"] == {"candidate_id": "d2", "jobdiva_id": "26-001", "trigger": "launch"}


def test_launch_enrichment_outcomes_read_like_the_browser(monkeypatch):
    answers = {
        "c0": {"status": "success", "phone": None, "mobilePhone": "+14155550100", "email": "a@x.com",
               "phone_provider": "kipplo", "email_provider": None, "lookup": {"kipplo": "phone"}},
        "c1": {"status": "skipped", "reason": "not eligible"},
        "c2": {"status": "success", "phone": None, "email": None, "lookup": {"kipplo": "out of credits"}},
        "c3": {"status": "error", "message": "x"},
    }

    async def impl(cid, item):
        if cid == "c4":
            raise RuntimeError("provider bug")
        return {"candidate_id": cid, **answers[cid]}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)
    out = {a["candidate_id"]: a for a in _drain(rc.enrich_contacts_for_launch(_items(5), job_id="J"))}
    assert out["c0"]["outcome"] == "found" and out["c0"]["phone"] == "+14155550100"
    assert out["c0"]["phone_provider"] == "kipplo" and out["c0"]["email_provider"] == "other"
    assert out["c0"]["looked_up"] is True and out["c0"]["kipplo"] == "phone"
    assert out["c1"]["outcome"] == "not_eligible" and out["c1"]["looked_up"] is False
    assert out["c2"]["outcome"] == "no_contact" and out["c2"]["kipplo"] == "out of credits"
    assert out["c3"]["outcome"] == "failed" and out["c4"]["outcome"] == "failed"
    assert out["c4"]["result"] == {"status": "error", "candidate_id": "c4", "message": "lookup failed: RuntimeError"}
    assert sorted(a["index"] for a in out.values()) == [0, 1, 2, 3, 4]


def test_launch_enrichment_matches_the_grouped_endpoint(monkeypatch):
    async def impl(cid, item):
        await asyncio.sleep(0.001 * int(cid[1:]))
        return {"status": "success", "candidate_id": cid, "phone": f"+1415555{int(cid[1:]):04d}",
                "trigger": item.trigger, "jobdiva_id": item.jobdiva_id}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)
    items = _items(6)
    body = rc.EnrichCandidateContactsRequest(jobdiva_id="J", trigger="launch", candidates=[
        rc.EnrichCandidateContactRequest(candidate_id=i["candidate_id"], source=i["source"],
                                         linkedin_url=i["linkedin_url"], match_score=i["match_score"])
        for i in items])
    endpoint = asyncio.run(rc.enrich_candidate_contacts(body, user=None))["results"]
    ours = sorted(_drain(rc.enrich_contacts_for_launch(items, job_id="J")), key=lambda a: a["index"])
    assert [a["result"] for a in ours] == endpoint


def test_launch_enrichment_bounds_lookups_in_flight(monkeypatch):
    state = {"now": 0, "max": 0}

    async def impl(cid, item):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return {"status": "success", "candidate_id": cid}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)
    out = _drain(rc.enrich_contacts_for_launch(_items(12), job_id="J", concurrency=3))
    assert len(out) == 12 and state["max"] == 3
    assert rc.LAUNCH_ENRICH_CONCURRENCY == 20


def test_stopping_early_lets_started_lookups_finish_and_never_starts_the_rest(monkeypatch):
    started: List[str] = []
    finished: List[str] = []

    async def impl(cid, item):
        started.append(cid)
        await asyncio.sleep(0.05 if cid != "c0" else 0)
        finished.append(cid)
        return {"status": "success", "candidate_id": cid}
    monkeypatch.setattr(rc, "_enrich_candidate_contact_impl", impl)

    async def go():
        async with contextlib.aclosing(rc.enrich_contacts_for_launch(_items(6), job_id="J", concurrency=2)) as gen:
            async for _a in gen:
                break                       # the consumer goes away after one answer
        await asyncio.sleep(0.2)            # paid lookups already running keep going
    asyncio.run(go())
    assert started[0] == "c0" and len(started) <= 3
    assert sorted(finished) == sorted(started)


def test_empty_launch_enrichment_yields_nothing(monkeypatch):
    assert _drain(rc.enrich_contacts_for_launch([], job_id="J")) == []


# --------------------------------------------------------- save ----------

def test_save_candidates_is_callable_directly_with_a_user_keyword(monkeypatch):
    """The runner's way in: no FastAPI, `user` passed by keyword."""
    executed: List[tuple] = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))
            self._row = ("26-001", "J1") if "SELECT jobdiva_id, job_id" in sql else None

        def fetchone(self):
            return self._row

        def close(self):
            pass

    class Conn:
        def cursor(self, **_k):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    async def no_gate(_job):
        return None
    import services.no_contact as nc
    access: List[tuple] = []
    monkeypatch.setattr(rc, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda job, user, **k: access.append((job, user.email, k)))
    monkeypatch.setattr(rc, "_enforce_recruiter_email_gate", no_gate)
    monkeypatch.setattr(rc, "stamp_job_launched_by", lambda *a, **k: None)
    monkeypatch.setattr(rc, "load_dnc_phone_set", lambda: set())
    monkeypatch.setattr(nc, "check_no_contact", lambda c: None)
    monkeypatch.setattr(rc, "_build_resume_matching_criteria", lambda job: None)
    monkeypatch.setattr(rc, "_compute_resume_matching", lambda p, c: {
        "score": 61.0, "status": "done", "score_details": {}, "matched_skills": [], "missing_skills": [],
        "explainability": [], "scored_at": "t"})
    res = asyncio.run(rc.save_candidates(
        CandidatesSaveRequest(jobdiva_id="J1", candidates=[CandidateSaveRecord(
            candidate_id="L1", source="LinkedIn-Exa", match_score=None, is_selected=True, phone="4155550100")]),
        user=UserIdentity(email="rec@x.com", role="recruiter")))
    assert res["saved_ids"] == ["L1"] and access == [("J1", "rec@x.com", {"allow_not_found": True})]
    rescore = [(s, p) for s, p in executed if "SET resume_match_percentage = %s" in s]
    assert len(rescore) == 1
    sql, params = rescore[0]
    assert "|| %s::jsonb" in sql and "jsonb_each(sourced_candidates.data)" in sql
    assert json.loads(params[1])["match_score"] == 61.0


def _save_source() -> str:
    src = Path(rc.__file__).read_text()
    tree = ast.parse(src)
    return next(ast.get_source_segment(src, n) for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "save_candidates")


def test_save_never_replaces_the_stored_blob_and_preserves_launch_keys():
    body = _save_source()
    assert not re.search(r"\bdata\s*=\s*%s\b", body)
    for key in ("engage_session_id", "engage_bulk_id", "engage_block_reason",
                "engage_status_before_claim", "jobdiva_provisioning_claimed_at", "engage_status",
                "engage_interview_id", "jobdiva_candidate_id"):
        # Both the upsert and the rescore carry every backend-owned key forward.
        assert body.count(f"'{key}'") >= 2, key
